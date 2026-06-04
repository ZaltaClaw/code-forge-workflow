# CLAUDE.md — Session Router

> Loaded when `claude` runs in `containers/session-router/`.

## What this is

A Go service (single binary, `main.go`, ~155 lines) that:

1. Accepts `POST /sessions` with `{dev_id, project_id}`.
2. Looks up Redis for an existing warm session for that pair → returns it.
3. Otherwise, lists agent pods labeled `app=agent-pod,state=warm`, picks one,
   **patches the pod's labels** to `state=bound, session-id=…, dev-id=…`,
   binds the dev's workspace PVC, writes a session record to Redis (TTL =
   idle timeout) and Cosmos (audit log).
4. Runs an idle reaper goroutine that scans Redis for expired sessions and
   patches the corresponding pod to `state=cooldown` (which triggers the
   pod's preStop scrub and graceful exit).

## Why Go (not Python)

- Native `client-go` for tight Kubernetes API integration.
- Single static binary in a distroless image — small attack surface.
- Goroutines for the reaper without async/await overhead.
- ~10ms p99 on the claim path under load (measured in dev).

## Build & run

```bash
go build ./...                          # quick compile check
go test ./...                           # (TODO — write tests)
docker build -t code-forge/session-router:dev .
```

## Environment variables

| Var | Purpose |
|---|---|
| `REDIS_HOST`, `REDIS_PORT` | Session state cache |
| `COSMOS_ENDPOINT`, `COSMOS_DATABASE`, `COSMOS_CONTAINER` | Long-term session audit |
| `AGENT_POOL_NAMESPACE` | Where the warm pods live (`agent-pool`) |
| `MODEL_GATEWAY_URL` | Where to mint LiteLLM virtual keys |
| `IDLE_EVICT_SECONDS` | TTL on session keys → drives reaper cadence |
| `MAX_CONCURRENT_PER_DEV` | Quota cap |

Auth to Kubernetes is **in-cluster** (`rest.InClusterConfig()`); auth to Cosmos
and Service Bus is **Workload Identity** via `DefaultAzureCredential` (TODO:
swap the Redis client for the Azure SDK Cosmos client + add the Service Bus
publisher).

## Editing rules

1. **Don't block in HTTP handlers.** The claim path is on the user's hot loop. Anything > 50ms goes to a goroutine.
2. **Patch pods atomically with strategic-merge.** Concurrent claims are guarded by `state=warm` → `state=bound` being a single label patch — if two routers race, the second sees `state=bound` and picks the next pod.
3. **Idempotent claims.** Re-claiming an existing session must return the same handle. The Redis lookup at step 2 covers this; never bypass it.
4. **Reaper must be conservative.** When in doubt, leave the session alive — a stuck pod is cheaper than a confused developer.

## Open work (TODOs in `main.go`)

- [ ] `budgetOK()` — wire to Cosmos / LiteLLM `/spend/total` endpoint
- [ ] `enqueuePending()` — push to Service Bus `agent-pod-claims` so KEDA scales the pool
- [ ] `bindWorkspace()` — patch the pod spec or use a CSI inline volume to mount the dev's PVC
- [ ] OpenTelemetry traces tagged with `session_id`, `dev_id`
- [ ] Tests (`httptest` + a fake clientset)

## Pitfalls

- **Pod state drift** — if a router crashes after `state=bound` but before writing Redis, the pod is "lost". A janitor cron (`docs/OPERATIONS.md`) reconciles labels with Redis every 5 min.
- **Cosmos throttling under burst** — bulk-claim on session start. Use the Cosmos bulk executor or fan out writes via Service Bus.
- **`kubectl exec`-based I/O is fragile** — long-lived connections through the API server hit timeouts. The longer-term plan is a per-pod HTTP shim that the client connects to directly via Service Mesh.
