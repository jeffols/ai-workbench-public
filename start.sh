#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Traefik shared ingress (creates proxy network)
docker compose -f traefik/docker-compose.yml up -d

# Workbench with tools + registry profiles
COMPOSE_PROFILES=tools,registry docker compose up -d
