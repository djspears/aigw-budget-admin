#!/usr/bin/env bash
# Deploy, preview or remove AI Gateway Budget Admin on Kubernetes.
#   ./deploy/deploy.sh [config.env]            render + apply + wait for rollout
#   ./deploy/deploy.sh [config.env] --dry-run  print the manifests with secrets redacted (nothing is applied)
#   ./deploy/deploy.sh [config.env] --uninstall delete the namespace (and everything in it)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CONFIG="${1:-$HERE/config.env}"; MODE="${2:-}"
case "$CONFIG" in --*) MODE="$CONFIG"; CONFIG="$HERE/config.env";; esac
[ -f "$CONFIG" ] || { echo "config not found: $CONFIG (copy deploy/config.env.example to deploy/config.env)"; exit 1; }
for t in python3 kubectl; do command -v "$t" >/dev/null || { echo "missing required tool: $t"; exit 1; }; done

val() { python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import render; print(render.load_env(sys.argv[2]).get(sys.argv[3], ""))' "$HERE" "$CONFIG" "$1"; }
CTX="$(val KUBE_CONTEXT)"; NS="$(val NAMESPACE)"; NS="${NS:-aigw-budget-admin}"
K=(kubectl); [ -n "$CTX" ] && K+=(--context "$CTX")

case "$MODE" in
  --dry-run)
    python3 "$HERE/render.py" "$CONFIG" --redact ;;
  --uninstall)
    read -r -p "Delete namespace '$NS' and everything in it? [y/N] " a
    [ "$a" = "y" ] && "${K[@]}" delete namespace "$NS" --wait=true ;;
  "")
    MANIFEST="$(python3 "$HERE/render.py" "$CONFIG")"          # validates config; secrets stay in memory
    printf '%s\n' "$MANIFEST" | "${K[@]}" apply -f -
    "${K[@]}" -n "$NS" rollout status deploy/aigw-budget-admin --timeout=180s
    echo
    "${K[@]}" -n "$NS" get pods,svc,ingress -l app=aigw-budget-admin 2>/dev/null || true
    echo
    echo "Open $(val PUBLIC_URL) and sign in with one of: $(val ADMIN_EMAILS)" ;;
  *) echo "unknown option: $MODE"; exit 1 ;;
esac
