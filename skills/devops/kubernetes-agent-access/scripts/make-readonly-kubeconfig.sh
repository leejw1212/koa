#!/usr/bin/env bash
# Apply the read-only SA (hermes-readonly.yaml next to this script) and write a token kubeconfig.
#   make-readonly-kubeconfig.sh [admin-context] [out]   (default out: ~/.kube/hermes-readonly.yaml, mode 600)
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
CTX="${1:-$(kubectl config current-context)}"
OUT="${2:-$HOME/.kube/hermes-readonly.yaml}"
K="kubectl --context $CTX"
$K apply -f "$DIR/hermes-readonly.yaml"
for _ in $(seq 1 30); do
  TOKEN="$($K -n hermes get secret hermes-readonly-token -o jsonpath='{.data.token}' 2>/dev/null | base64 -d || true)"
  [ -n "$TOKEN" ] && break; sleep 1
done
[ -n "${TOKEN:-}" ] || { echo "token not issued" >&2; exit 1; }
CLUSTER="$(kubectl config view -o jsonpath="{.contexts[?(@.name==\"$CTX\")].context.cluster}")"
SERVER="$(kubectl config view -o jsonpath="{.clusters[?(@.name==\"$CLUSTER\")].cluster.server}")"
CA="$($K -n hermes get secret hermes-readonly-token -o jsonpath='{.data.ca\.crt}')"
mkdir -p "$(dirname "$OUT")"; umask 077
cat > "$OUT" <<EOF
apiVersion: v1
kind: Config
clusters:
- name: $CLUSTER
  cluster: {server: $SERVER, certificate-authority-data: $CA}
users:
- name: hermes-readonly
  user: {token: $TOKEN}
contexts:
- name: $CTX-readonly
  context: {cluster: $CLUSTER, user: hermes-readonly}
current-context: $CTX-readonly
EOF
chmod 600 "$OUT"; echo "wrote $OUT (context $CTX-readonly)"
