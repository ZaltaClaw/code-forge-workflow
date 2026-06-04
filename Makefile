# =============================================================================
# Code Forge top-level Makefile — image build/push + chart install/template.
# =============================================================================
TAG ?= 1.0.0
ACR ?= acrtheclouds.azurecr.io
RELEASE ?= code-forge
NAMESPACE ?= session-control

.PHONY: help
help:
	@echo "Targets:"
	@echo "  build-images     # docker build agent-pod + session-router + model-gateway"
	@echo "  push-images      # docker push to \$$ACR"
	@echo "  chart-lint       # helm lint"
	@echo "  chart-template   # helm template (preview rendered yaml)"
	@echo "  chart-install    # helm upgrade --install"
	@echo "  chart-uninstall  # helm uninstall"

.PHONY: build-images
build-images:
	docker build -t $(ACR)/code-forge/agent-pod:$(TAG)      containers/agent-pod
	docker build -t $(ACR)/code-forge/session-router:$(TAG) containers/session-router
	docker build -t $(ACR)/code-forge/model-gateway:$(TAG)  containers/model-gateway

.PHONY: push-images
push-images:
	az acr login -n $(firstword $(subst ., ,$(ACR)))
	docker push $(ACR)/code-forge/agent-pod:$(TAG)
	docker push $(ACR)/code-forge/session-router:$(TAG)
	docker push $(ACR)/code-forge/model-gateway:$(TAG)

.PHONY: chart-lint
chart-lint:
	helm lint charts/code-forge \
	  --set global.foundry.resource=demo \
	  --set global.azureTenantId=00000000-0000-0000-0000-000000000000 \
	  --set workloadIdentity.agentPod.clientId=11111111-1111-1111-1111-111111111111 \
	  --set workloadIdentity.sessionRouter.clientId=22222222-2222-2222-2222-222222222222 \
	  --set workloadIdentity.modelGateway.clientId=33333333-3333-3333-3333-333333333333 \
	  --set agentPod.keda.serviceBus.namespace=demo.servicebus.windows.net

.PHONY: chart-template
chart-template:
	@helm template $(RELEASE) charts/code-forge \
	  --set global.foundry.resource=demo \
	  --set global.azureTenantId=00000000-0000-0000-0000-000000000000 \
	  --set workloadIdentity.agentPod.clientId=11111111-1111-1111-1111-111111111111 \
	  --set workloadIdentity.sessionRouter.clientId=22222222-2222-2222-2222-222222222222 \
	  --set workloadIdentity.modelGateway.clientId=33333333-3333-3333-3333-333333333333 \
	  --set agentPod.keda.serviceBus.namespace=demo.servicebus.windows.net

.PHONY: chart-install
chart-install:
	helm upgrade --install $(RELEASE) charts/code-forge \
	  --create-namespace --namespace $(NAMESPACE) \
	  -f charts/code-forge/values-prod.yaml

.PHONY: chart-uninstall
chart-uninstall:
	helm uninstall $(RELEASE) --namespace $(NAMESPACE)
