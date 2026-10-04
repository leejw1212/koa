#!/usr/bin/env bash
# 읽기 전용 SA 를 적용하고 그 토큰으로 kubeconfig 를 만든다. 여러 번 실행해도 안전하다.
#   k8s/make-readonly-kubeconfig.sh [admin-context] [출력경로]
# 기본: 현재 컨텍스트 → ~/.kube/hermes-readonly.yaml (토큰이 들어 있으므로 커밋 금지, 600 권한)
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
CTX="${1:-$(kubectl config current-context)}"
OUT="${2:-$HOME/.kube/hermes-readonly.yaml}"
K="kubectl --context $CTX"

$K apply -f "$DIR/hermes-readonly.yaml"

# 토큰 컨트롤러가 Secret 을 채울 때까지 대기
for _ in $(seq 1 30); do
  TOKEN="$($K -n hermes get secret hermes-readonly-token -o jsonpath='{.data.token}' 2>/dev/null | base64 -d || true)"
  [ -n "$TOKEN" ] && break
  sleep 1
done
[ -n "${TOKEN:-}" ] || { echo "토큰 발급 실패" >&2; exit 1; }

CLUSTER="$(kubectl config view -o jsonpath="{.contexts[?(@.name==\"$CTX\")].context.cluster}")"
SERVER="$(kubectl config view -o jsonpath="{.clusters[?(@.name==\"$CLUSTER\")].cluster.server}")"
CA="$($K -n hermes get secret hermes-readonly-token -o jsonpath='{.data.ca\.crt}')"

mkdir -p "$(dirname "$OUT")"
umask 077
cat > "$OUT" <<EOF
apiVersion: v1
kind: Config
clusters:
- name: $CLUSTER
  cluster:
    server: $SERVER
    certificate-authority-data: $CA
users:
- name: hermes-readonly
  user:
    token: $TOKEN
contexts:
- name: $CTX-readonly
  context:
    cluster: $CLUSTER
    user: hermes-readonly
current-context: $CTX-readonly
EOF
chmod 600 "$OUT"
echo "작성: $OUT (context $CTX-readonly)"
