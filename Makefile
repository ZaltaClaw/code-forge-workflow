# =============================================================================
# Code Forge top-level Makefile — image build/push + chart install/template.
# =============================================================================
TAG ?= 1.0.0
ACR ?= codeforgedemo.azurecr.io
RELEASE ?= code-forge
NAMESPACE ?= session-control
# AKS nodes are linux/amd64; build for that platform even on Apple Silicon.
PLATFORM ?= linux/amd64
# agent-sandbox controller + CRDs (core Sandbox + extensions: SandboxTemplate/
# SandboxWarmPool/SandboxClaim). https://github.com/kubernetes-sigs/agent-sandbox
AGENT_SANDBOX_VERSION ?= v0.4.6
AGENT_SANDBOX_BASE ?= https://github.com/kubernetes-sigs/agent-sandbox/releases/download/$(AGENT_SANDBOX_VERSION)

# Placeholder values used only for `helm lint` / `helm template` previews.
FOUNDRY_RESOURCE ?= demo
AZURE_TENANT_ID ?= 00000000-0000-0000-0000-000000000000
ORCHESTRATOR_CLIENT_ID ?= 11111111-1111-1111-1111-111111111111
GATEWAY_CLIENT_ID ?= 33333333-3333-3333-3333-333333333333
# Shared --set flags for the lint/template demo render.
DEMO_SET = \
	  --set global.foundry.resource=$(FOUNDRY_RESOURCE) \
	  --set global.azureTenantId=$(AZURE_TENANT_ID) \
	  --set workloadIdentity.sandboxOrchestrator.clientId=$(ORCHESTRATOR_CLIENT_ID) \
	  --set workloadIdentity.modelGateway.clientId=$(GATEWAY_CLIENT_ID)

.PHONY: help
help:
	@echo "Targets:"
	@echo "  build-images     # docker build agent-pod + sandbox-orchestrator + model-gateway"
	@echo "  push-images      # docker push to \$$ACR"
	@echo "  chart-lint       # helm lint"
	@echo "  chart-template   # helm template (preview rendered yaml)"
	@echo "  install-crds     # kubectl apply agent-sandbox CRDs + controller"
	@echo "  chart-install    # install-crds then helm upgrade --install"
	@echo "  chart-uninstall  # helm uninstall"

.PHONY: build-images
build-images:
	docker buildx build --platform $(PLATFORM) --load -t $(ACR)/code-forge/agent-pod:$(TAG)            containers/agent-pod
	docker buildx build --platform $(PLATFORM) --load -t $(ACR)/code-forge/model-gateway:$(TAG)         containers/model-gateway
	docker buildx build --platform $(PLATFORM) --load -t $(ACR)/code-forge/sandbox-orchestrator:$(TAG)  containers/sandbox-orchestrator

.PHONY: push-images
push-images:
	az acr login -n $(firstword $(subst ., ,$(ACR)))
	docker push $(ACR)/code-forge/agent-pod:$(TAG)
	docker push $(ACR)/code-forge/model-gateway:$(TAG)
	docker push $(ACR)/code-forge/sandbox-orchestrator:$(TAG)

.PHONY: chart-lint
chart-lint:
	helm lint charts/code-forge $(DEMO_SET)

.PHONY: chart-template
chart-template:
	@helm template $(RELEASE) charts/code-forge $(DEMO_SET)

.PHONY: install-crds
install-crds:
	kubectl apply --server-side -f $(AGENT_SANDBOX_BASE)/manifest.yaml
	kubectl apply --server-side -f $(AGENT_SANDBOX_BASE)/extensions.yaml
	kubectl -n agent-sandbox-system rollout status deploy/agent-sandbox-controller --timeout=120s

.PHONY: chart-install
chart-install: install-crds
	helm upgrade --install $(RELEASE) charts/code-forge \
	  --create-namespace --namespace $(NAMESPACE) \
	  -f charts/code-forge/values-prod.yaml

.PHONY: chart-uninstall
chart-uninstall:
	helm uninstall $(RELEASE) --namespace $(NAMESPACE)
