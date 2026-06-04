// session-router — stateless allocator. Sketch of the core claim/release loop.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/redis/go-redis/v9"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
)

type Session struct {
	ID         string    `json:"id"`
	DevID      string    `json:"dev_id"`
	ProjectID  string    `json:"project_id"`
	PodName    string    `json:"pod_name"`
	NodeName   string    `json:"node_name"`
	BoundAt    time.Time `json:"bound_at"`
	LastSeenAt time.Time `json:"last_seen_at"`
}

type Router struct {
	rdb     *redis.Client
	k8s     *kubernetes.Clientset
	idleSec int
	maxConc int
}

// POST /sessions  body: {dev_id, project_id}
// Returns existing session if warm, else allocates a free pod.
func (r *Router) handleClaim(w http.ResponseWriter, req *http.Request) {
	var body struct{ DevID, ProjectID string }
	if err := json.NewDecoder(req.Body).Decode(&body); err != nil {
		http.Error(w, err.Error(), 400); return
	}
	ctx := req.Context()
	key := fmt.Sprintf("sess:%s:%s", body.DevID, body.ProjectID)

	// 1. existing session?
	if raw, err := r.rdb.Get(ctx, key).Result(); err == nil {
		var s Session
		_ = json.Unmarshal([]byte(raw), &s)
		s.LastSeenAt = time.Now()
		r.rdb.Set(ctx, key, mustJSON(s), time.Duration(r.idleSec)*time.Second)
		writeJSON(w, s); return
	}

	// 2. quota check (concurrent + budget)
	if cnt, _ := r.rdb.SCard(ctx, "dev:"+body.DevID+":sessions").Result(); int(cnt) >= r.maxConc {
		http.Error(w, "concurrent session limit", 429); return
	}
	if !r.budgetOK(ctx, body.DevID) {
		http.Error(w, "daily budget exhausted", 402); return
	}

	// 3. find a warm pod and bind it
	pods, err := r.k8s.CoreV1().Pods("agent-pool").List(ctx, metav1.ListOptions{
		LabelSelector: "app=agent-pod,state=warm",
		Limit:         10,
	})
	if err != nil || len(pods.Items) == 0 {
		// pool empty → enqueue and return 202
		r.enqueuePending(ctx, body.DevID, body.ProjectID)
		http.Error(w, "pool empty, queued", 202); return
	}
	pod := pods.Items[0]

	// 4. patch labels: state=bound, dev-id, session-id, project-id
	sessID := newID()
	patch := fmt.Sprintf(
		`{"metadata":{"labels":{"state":"bound","session-id":%q,"dev-id":%q,"project-id":%q}}}`,
		sessID, body.DevID, body.ProjectID,
	)
	if _, err := r.k8s.CoreV1().Pods("agent-pool").
		Patch(ctx, pod.Name, "application/strategic-merge-patch+json", []byte(patch), metav1.PatchOptions{}); err != nil {
		http.Error(w, err.Error(), 500); return
	}

	// 5. mount the dev's workspace PVC (separate call: bind PVC to pod via projected volume update)
	r.bindWorkspace(ctx, pod.Name, body.DevID, body.ProjectID)

	s := Session{
		ID: sessID, DevID: body.DevID, ProjectID: body.ProjectID,
		PodName: pod.Name, NodeName: pod.Spec.NodeName,
		BoundAt: time.Now(), LastSeenAt: time.Now(),
	}
	r.rdb.Set(ctx, key, mustJSON(s), time.Duration(r.idleSec)*time.Second)
	r.rdb.SAdd(ctx, "dev:"+body.DevID+":sessions", sessID)
	writeJSON(w, s)
}

// Idle reaper — runs as a goroutine, polls Redis for expired keys
// and patches pods state=bound -> state=cooldown -> deletes (replaced by replicaset)
func (r *Router) reapIdle(ctx context.Context) {
	t := time.NewTicker(30 * time.Second)
	for range t.C {
		iter := r.rdb.Scan(ctx, 0, "sess:*", 100).Iterator()
		for iter.Next(ctx) {
			key := iter.Val()
			ttl, _ := r.rdb.TTL(ctx, key).Result()
			if ttl < 30*time.Second && ttl > 0 {
				continue
			}
			var s Session
			raw, _ := r.rdb.Get(ctx, key).Result()
			_ = json.Unmarshal([]byte(raw), &s)
			r.releasePod(ctx, s)
			r.rdb.Del(ctx, key)
			r.rdb.SRem(ctx, "dev:"+s.DevID+":sessions", s.ID)
		}
	}
}

func (r *Router) releasePod(ctx context.Context, s Session) {
	// Mark state=cooldown — the kubelet preStop hook scrubs /workspace, then exits.
	// ReplicaSet replaces the pod with a fresh state=warm one.
	patch := `{"metadata":{"labels":{"state":"cooldown"}}}`
	_, _ = r.k8s.CoreV1().Pods("agent-pool").
		Patch(ctx, s.PodName, "application/strategic-merge-patch+json", []byte(patch), metav1.PatchOptions{})
	_ = r.k8s.CoreV1().Pods("agent-pool").Delete(ctx, s.PodName, metav1.DeleteOptions{})
}

// --- helpers (stubs) ---
func (r *Router) budgetOK(ctx context.Context, devID string) bool { return true }
func (r *Router) enqueuePending(ctx context.Context, dev, proj string) {}
func (r *Router) bindWorkspace(ctx context.Context, pod, dev, proj string) {}
func newID() string { return strconv.FormatInt(time.Now().UnixNano(), 36) }
func mustJSON(v interface{}) string { b, _ := json.Marshal(v); return string(b) }
func writeJSON(w http.ResponseWriter, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

func main() {
	idle, _ := strconv.Atoi(os.Getenv("IDLE_EVICT_SECONDS"))
	maxc, _ := strconv.Atoi(os.Getenv("MAX_CONCURRENT_PER_DEV"))
	cfg, _ := rest.InClusterConfig()
	cs, _ := kubernetes.NewForConfig(cfg)
	r := &Router{
		rdb: redis.NewClient(&redis.Options{Addr: os.Getenv("REDIS_URL")}),
		k8s: cs, idleSec: idle, maxConc: maxc,
	}
	go r.reapIdle(context.Background())
	http.HandleFunc("/sessions", r.handleClaim)
	http.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) { w.Write([]byte("ok")) })
	fmt.Println("session-router listening on :8080")
	http.ListenAndServe(":8080", nil)
}
