# Convenience targets for the Temporal + MAF POC.
# Local-without-docker workflow uses a venv; docker workflow uses compose.

IMAGE ?= temporal-maf-agents-poc:dev
export PYTHONPATH := src

.PHONY: help install up down logs run test build workers \
        worker-orchestrator worker-planner worker-github worker-aks worker-approval

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

install: ## Install Python deps into the active environment
	pip install -r requirements.txt -e .

up: ## Start Temporal + all 5 workers via docker-compose
	docker compose up --build

down: ## Stop the docker-compose stack and remove volumes
	docker compose down -v

logs: ## Tail worker logs
	docker compose logs -f orchestrator-worker planner-agent-worker github-agent-worker aks-agent-worker approval-agent-worker

run: ## Start one orchestration from sample-input.json
	python -m starter

test: ## Run the unit tests (no Temporal server needed)
	pytest -q

build: ## Build the shared worker image
	docker build -t $(IMAGE) .

# --- Run individual workers locally (needs a reachable Temporal at $TEMPORAL_ADDRESS) ---
worker-orchestrator: ## Run the orchestrator worker locally
	python -m orchestrator_worker.worker
worker-planner: ## Run the planner agent worker locally
	python -m planner_agent_worker.worker
worker-github: ## Run the github agent worker locally
	python -m github_agent_worker.worker
worker-aks: ## Run the aks agent worker locally
	python -m aks_agent_worker.worker
worker-approval: ## Run the approval agent worker locally
	python -m approval_agent_worker.worker
