# Workbench slim variant of the agentregistry server image.
#
# Based on docker/server.Dockerfile from
# https://github.com/agentregistry-dev/agentregistry, stripped of the
# Docker CLI + Docker Compose plugin installs that upstream bundles for
# arctl-server's orchestration use cases.  In the workbench we use the
# server as a pure catalog / registry — no host Docker orchestration —
# so the runtime only needs ca-certificates + curl.
#
# Final image: ~132 MB (vs ~600 MB upstream).
# Builds from the upstream clone populated by scripts/fetch_agentregistry.py.

ARG BUILDPLATFORM

# ── UI build ──────────────────────────────────────────────────────────────
FROM --platform=$BUILDPLATFORM node:22-alpine AS ui-builder
RUN apk add --no-cache make
WORKDIR /app
COPY Makefile ./
COPY ui/package.json ui/package-lock.json ./
COPY ui ui
RUN mkdir -p internal/registry/api/ui/dist
RUN make build-ui

# ── Go binary build ───────────────────────────────────────────────────────
FROM --platform=$BUILDPLATFORM golang:1.25-alpine AS builder
RUN apk add --no-cache make
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download && go mod verify
COPY cmd cmd
COPY internal internal
COPY pkg pkg
COPY --from=ui-builder /app/internal/registry/api/ui/dist /app/internal/registry/api/ui/dist
ARG TARGETARCH
ARG TARGETPLATFORM
ARG LDFLAGS
RUN CGO_ENABLED=0 GOOS=${TARGETOS:-linux} GOARCH=${TARGETARCH} \
    go build -a -ldflags "$LDFLAGS" -o bin/arctl-server cmd/server/main.go

# ── Runtime ───────────────────────────────────────────────────────────────
FROM alpine:3.21.7 AS runtime
RUN apk add --no-cache ca-certificates curl
COPY --from=builder /app/bin/arctl-server /app/bin/arctl-server

LABEL org.opencontainers.image.source=https://github.com/agentregistry-dev/agentregistry
LABEL org.opencontainers.image.description="Agent Registry Server (workbench slim variant)"
LABEL org.opencontainers.image.authors="Agent Registry Creators 🤖"

EXPOSE 8080
CMD ["/app/bin/arctl-server"]
