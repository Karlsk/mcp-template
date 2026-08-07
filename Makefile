# Makefile for the SDN MCP server.
# Docker targets wrap deploy/docker-compose.yml. Secrets/connection are read
# from the gitignored .env (SDN_CONTROLLER_*). Compose looks for .env next to
# the compose file (deploy/) by default, so point it at the project-root .env.

ENV_FILE := $(wildcard .env)
COMPOSE := docker compose -f deploy/docker-compose.yml $(if $(ENV_FILE),--env-file .env)

.PHONY: help docker-build docker-build-frozen docker-up docker-stop docker-restart docker-ps docker-logs

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN { FS = ":.*?## " } { printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2 }'

docker-build: ## Build the sdn-mcp image (deploy/Dockerfile, mirrors, no --frozen)
	$(COMPOSE) build

docker-build-frozen: ## Build the image with --frozen (deploy/Dockerfile.frozen, locked deps)
	docker build -f deploy/Dockerfile.frozen -t sdn-mcp:latest .

docker-up: ## Build (if needed) and start the container in the background
	$(COMPOSE) up -d --build

docker-stop: ## Stop and remove the container (keeps the image)
	$(COMPOSE) down

docker-restart: ## Restart the container (no rebuild)
	$(COMPOSE) restart

docker-ps: ## Show container status
	$(COMPOSE) ps

docker-logs: ## Tail container logs (Ctrl-C to exit)
	$(COMPOSE) logs -f
