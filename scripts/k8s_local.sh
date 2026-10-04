#!/usr/bin/env bash
# One-command local Kubernetes deployment (docker-desktop / kind / minikube).
#
#   scripts/k8s_local.sh up        build image, create secrets, deploy, wait until healthy
#   scripts/k8s_local.sh status    show pods / services
#   scripts/k8s_local.sh forward   port-forward the API to http://localhost:8000 (Ctrl+C to stop)
#   scripts/k8s_local.sh logs      tail API logs
#   scripts/k8s_local.sh down      delete everything (namespace + volumes)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NS=sentinel-rag
OVERLAY="$ROOT/k8s/overlays/dev"
SECRETS="$OVERLAY/secrets.env"
LOCAL_CONTEXTS="docker-desktop kind-* minikube rancher-desktop orbstack k3d-*"

say()  { printf "\033[1;34m==>\033[0m %s\n" "$*"; }
fail() { printf "\033[1;31mERROR:\033[0m %s\n" "$*" >&2; exit 1; }

preflight() {
  command -v docker  >/dev/null || fail "docker not installed"
  command -v kubectl >/dev/null || fail "kubectl not installed"
  docker info >/dev/null 2>&1   || fail "Docker is not running - start Docker Desktop"
  local ctx; ctx="$(kubectl config current-context 2>/dev/null)" || fail "no kubectl context. Enable Kubernetes in Docker Desktop (Settings > Kubernetes)"
  local ok=0
  for pattern in $LOCAL_CONTEXTS; do [[ "$ctx" == $pattern ]] && ok=1; done
  # Safety: never deploy dev secrets to a shared/remote cluster by accident
  [[ $ok == 1 || "${ALLOW_ANY_CONTEXT:-}" == 1 ]] || fail "context '$ctx' is not a local cluster. Run: kubectl config use-context docker-desktop"
  kubectl get nodes >/dev/null 2>&1 || fail "cluster '$ctx' is not reachable"
  say "Using cluster context: $ctx"
}

ensure_api_key() {
  if ! grep -qE '^API_KEYS_SHA256=\{".+' "$ROOT/.env" 2>/dev/null; then
    say "No API key yet - generating one (the key is printed ONCE below; save it)"
    (cd "$ROOT" && "${PY:-$ROOT/.venv/bin/python}" scripts/gen_api_key.py --tenant orbitly)
  fi
}

ensure_secrets() {
  local keys; keys="$(grep -E '^API_KEYS_SHA256=' "$ROOT/.env" | cut -d= -f2-)"
  if [[ ! -f "$SECRETS" ]]; then
    say "Creating $SECRETS with random passwords (git-ignored)"
    local pw qk
    pw="$(openssl rand -hex 24)"
    qk="$(openssl rand -hex 24)"
    umask 077
    cat >"$SECRETS" <<EOF
POSTGRES_PASSWORD=$pw
POSTGRES_DSN=postgresql://sentinel:$pw@postgres:5432/sentinel
REDIS_URL=redis://valkey:6379/0
QDRANT_API_KEY=$qk
API_KEYS_SHA256=$keys
EOF
  else
    # keep DB passwords stable (they're baked into the Postgres volume); refresh only the API key hashes
    local tmp; tmp="$(mktemp)"
    grep -v '^API_KEYS_SHA256=' "$SECRETS" >"$tmp"; echo "API_KEYS_SHA256=$keys" >>"$tmp"; mv "$tmp" "$SECRETS"
    chmod 600 "$SECRETS"
  fi
}

up() {
  preflight
  [[ -x "$ROOT/.venv/bin/python" ]] || fail "run 'make install' first"
  ensure_api_key
  ensure_secrets
  say "Building image"
  docker build -q -t sentinel-rag:local "$ROOT" >/dev/null
  # Unique tag per build: nodes cache images by tag, so re-using ":local" would keep running stale code.
  local tag; tag="local-$(docker image inspect -f '{{.Id}}' sentinel-rag:local | cut -c8-19)"
  docker tag sentinel-rag:local "sentinel-rag:$tag"
  say "Image: sentinel-rag:$tag"
  say "Applying manifests (k8s/overlays/dev) with image sentinel-rag:$tag"
  # Render + pin the image in one apply, so each deploy triggers exactly ONE rolling update.
  kubectl kustomize "$OVERLAY" | sed "s|image: sentinel-rag:local\$|image: sentinel-rag:$tag|" | kubectl apply -f -
  say "Waiting for datastores..."
  kubectl -n $NS rollout status statefulset/postgres --timeout=180s
  kubectl -n $NS rollout status statefulset/qdrant   --timeout=180s
  kubectl -n $NS rollout status deployment/valkey    --timeout=120s
  say "Waiting for the rolling update of the API"
  kubectl -n $NS rollout status deployment/sentinel-rag --timeout=180s
  status
  cat <<EOF

$(say "Deployed.")  Next:
  1) scripts/k8s_local.sh forward          # API on http://localhost:8000  (keep it running)
  2) In another terminal:
       curl localhost:8000/readyz
       API_KEY=<your key> make demo-remote  # ingest sample docs + run sample queries
  Lost your key? run 'make dev-key' then 'scripts/k8s_local.sh up' again.
EOF
}

status()  { kubectl -n $NS get pods,svc,pvc,hpa -o wide; }
forward() { say "Forwarding http://localhost:${PORT:-8000} -> svc/sentinel-rag (Ctrl+C to stop)"; kubectl -n $NS port-forward svc/sentinel-rag "${PORT:-8000}:80"; }
logs()    { kubectl -n $NS logs -f deployment/sentinel-rag; }
down()    { preflight; say "Deleting namespace $NS (includes data volumes)"; kubectl delete namespace $NS --ignore-not-found; }

case "${1:-}" in
  up|status|forward|logs|down) "$1" ;;
  *) sed -n '2,9p' "$0"; exit 1 ;;
esac
