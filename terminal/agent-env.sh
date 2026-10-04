# Hermes 에이전트 터미널 전용 환경. config.yaml 의 terminal.shell_init_files 가 세션 시작 시 source 한다.
# 사람의 셸(~/.zshrc 등)에는 영향 없다.

# kubectl 기본 대상을 읽기 전용 SA kubeconfig 로 고정한다 (~/.kube/config 의 admin 대신).
# 주의: 기본값을 바꾸는 것이지 보안 경계는 아니다. --kubeconfig ~/.kube/config 를 명시하면 우회된다.
export KUBECONFIG="$HOME/.kube/hermes-readonly.yaml"
