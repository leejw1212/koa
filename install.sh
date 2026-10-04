#!/usr/bin/env bash
# 이 저장소의 설정을 Hermes 에 연결한다. 여러 번 실행해도 안전하다.
#   git clone git@github.com:leejw1212/koa.git ~/hermes-config && ~/hermes-config/install.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
mkdir -p "$HOME_DIR"

# config.yaml: 저장소 파일로 가는 심볼릭 링크. 기존 실파일은 백업.
if [ -e "$HOME_DIR/config.yaml" ] && [ ! -L "$HOME_DIR/config.yaml" ]; then
  mv "$HOME_DIR/config.yaml" "$HOME_DIR/config.yaml.bak.$(date +%Y%m%d%H%M%S)"
  echo "기존 config.yaml 을 .bak 으로 옮겼다"
fi
ln -sfn "$REPO/config.yaml" "$HOME_DIR/config.yaml"
echo "config.yaml -> $REPO/config.yaml"

# .env: 없으면 예시에서 만든다. 있으면 건드리지 않는다.
if [ ! -f "$HOME_DIR/.env" ]; then
  cp "$REPO/.env.example" "$HOME_DIR/.env"
  chmod 600 "$HOME_DIR/.env"
  echo "$HOME_DIR/.env 를 만들었다 — 값을 채워라"
fi

# kubernetes MCP 는 읽기 전용 kubeconfig 를 쓴다. 없으면 안내만 한다(클러스터 접근이 필요하므로 자동 실행 안 함).
if [ ! -f "$HOME/.kube/hermes-readonly.yaml" ]; then
  echo "주의: ~/.kube/hermes-readonly.yaml 없음 → $REPO/k8s/make-readonly-kubeconfig.sh [context] 실행"
fi

echo "완료. 실행 중인 Hermes 에서는 /reload-mcp (MCP 변경) 또는 재시작."
