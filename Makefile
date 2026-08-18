.PHONY: bootstrap validate config up up-core up-notebook up-registry up-onyx up-guardrails up-full logs ps health stats down clean-generated clean-phoenix open open-litellm open-phoenix open-notebook open-catalog restart rebuild pin-images build-registry fetch-registry help _wait-litellm

.DEFAULT_GOAL := help

# All profiles — used for down/config to ensure nothing is missed
ALL_PROFILES=openwebui,onyx,tools,notebook,registry,guardrails,debug

help: ## Show this help
	@printf '\033[1mUsage:\033[0m make \033[36m<target>\033[0m\n\n'
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*##/ {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo ""

bootstrap: ## Generate .env + all config from templates
	python3 scripts/bootstrap.py

validate: ## Pre-flight .env validation
	python3 scripts/validate_env.py

config: ## Print merged compose config (all profiles)
	COMPOSE_PROFILES=$(ALL_PROFILES) docker compose config

up: bootstrap validate ## Core + Open WebUI + tools (daily driver)
	@$(MAKE) --no-print-directory _wait-litellm
	COMPOSE_PROFILES=openwebui,tools docker compose up -d

up-core: bootstrap validate ## Core only (no frontend, no tools)
	@$(MAKE) --no-print-directory _wait-litellm
	docker compose up -d

up-notebook: bootstrap validate ## Open WebUI + tools + Open Notebook
	@$(MAKE) --no-print-directory _wait-litellm
	COMPOSE_PROFILES=openwebui,tools,notebook docker compose up -d

fetch-registry: ## Clone agentregistry upstream source
	python3 scripts/fetch_agentregistry.py

build-registry: fetch-registry ## Build agentregistry server image
	COMPOSE_PROFILES=registry docker compose build agentregistry

up-registry: build-registry bootstrap validate ## Open WebUI + tools + Agentregistry
	@$(MAKE) --no-print-directory _wait-litellm
	COMPOSE_PROFILES=openwebui,tools,registry docker compose up -d

up-onyx: bootstrap validate ## Onyx frontend + tools (rich RAG)
	@$(MAKE) --no-print-directory _wait-litellm
	COMPOSE_PROFILES=onyx,tools docker compose up -d

up-guardrails: bootstrap validate ## Core + Open WebUI + tools + guardrails (Presidio DLP + injection heuristic)
	@$(MAKE) --no-print-directory _wait-litellm
	COMPOSE_PROFILES=openwebui,tools,guardrails docker compose up -d

up-full: bootstrap validate ## Everything including debug
	@$(MAKE) --no-print-directory _wait-litellm
	COMPOSE_PROFILES=$(ALL_PROFILES) docker compose up -d

logs: ## Tail logs (last 200 lines)
	COMPOSE_PROFILES=$(ALL_PROFILES) docker compose logs -f --tail=200

ps: ## List running containers
	COMPOSE_PROFILES=$(ALL_PROFILES) docker compose ps

health: ## Run healthcheck against all services
	python3 scripts/healthcheck.py

stats: ## Tool-usage stats from Phoenix traces
	python3 scripts/tool_stats.py

down: ## Stop all containers (preserves volumes)
	COMPOSE_PROFILES=$(ALL_PROFILES) docker compose down

restart: down bootstrap validate ## Stop, regenerate config, rebuild, start
	@$(MAKE) --no-print-directory _wait-litellm
	COMPOSE_PROFILES=openwebui,tools docker compose up -d --build

rebuild: down clean-generated bootstrap ## Full rebuild (nukes build cache)
	COMPOSE_PROFILES=$(ALL_PROFILES) docker compose build --no-cache
	@$(MAKE) --no-print-directory _wait-litellm
	COMPOSE_PROFILES=openwebui,tools docker compose up -d

clean-generated: ## Delete generated config files
	rm -f config/litellm/config.yaml config/caddy/Caddyfile
	rm -f config/openwebui/compose.tool-connections.yaml
	rm -rf config/catalog

pin-images: ## Freeze running image digests into .env
	python3 scripts/pin_images.py

clean-phoenix: ## Wipe Phoenix traces and restart
	COMPOSE_PROFILES=openwebui,tools docker compose rm -sf phoenix
	docker volume rm -f ai-workbench_phoenix-data
	COMPOSE_PROFILES=openwebui,tools docker compose up -d phoenix

open: open-catalog ## Open status dashboard (alias)

open-catalog: ## Open status dashboard in browser
	open http://ai-workbench.localhost

open-litellm: ## Open LiteLLM UI in browser
	open http://litellm.localhost

open-phoenix: ## Open Phoenix tracing UI in browser
	open http://phoenix.localhost

open-notebook: ## Open Notebook UI in browser
	open http://notebook.localhost

catalog: ## Open catalog page in browser
	open http://localhost:8090/catalog/

query: ## Run a chat query (usage: make query Q="your question")
	@python3 scripts/query.py $(Q)

# ── Internal helpers (not shown in help) ──────────────────────────────────────

_wait-litellm:
	@docker compose up -d litellm
	@printf 'Waiting for litellm...'
	@until [ "$$(docker inspect --format='{{.State.Health.Status}}' ai-workbench-litellm-1 2>/dev/null)" = "healthy" ]; do \
		sleep 2; printf '.'; \
	done
	@echo ' ready'