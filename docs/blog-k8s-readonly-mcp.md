# AI 에이전트에게 쿠버네티스 "보기만" 허락하기 — MCP + 읽기 전용 ServiceAccount

AI 에이전트(Hermes)에 쿠버네티스 MCP 서버를 붙여 클러스터 상태를 물어보고 있었다.
MCP 쪽에서는 조회 도구 4개만 켜 두었지만, 질문 하나가 남았다.

> "조회 도구만 켰는데, 지금 삭제나 생성도 할 수 있어?"

결론부터 말하면 **도구 필터만으로는 부족하다.** 이 글에서는 API 서버(RBAC)가 직접 쓰기를 거부하도록
읽기 전용 ServiceAccount를 만들고, MCP 서버가 그 계정으로만 접속하게 바꾼 과정을 정리한다.

## 환경

| 항목 | 값 |
|---|---|
| 클러스터 | kind 3노드 (`kind-lab`, v1.37) |
| 에이전트 | Hermes Agent (데스크톱) |
| MCP 서버 | [`mcp-server-kubernetes`](https://www.npmjs.com/package/mcp-server-kubernetes) v4.1.9 (`npx`로 실행) |

## 1. 문제: 도구 필터는 "보이지 않게" 할 뿐이다

기존 MCP 설정은 다음과 같았다.

```yaml
mcp_servers:
  kubernetes:
    command: npx
    args: [-y, mcp-server-kubernetes]
    env:
      ALLOW_ONLY_NON_DESTRUCTIVE_TOOLS: "true"   # 서버가 delete 계열 도구를 등록하지 않음
    tools:
      include:                                   # 에이전트에 노출할 도구 화이트리스트
        - kubectl_get
        - kubectl_describe
        - kubectl_logs
        - explain_resource
```

이렇게 하면 에이전트에게 쓰기 도구가 **보이지 않는다.** 하지만 MCP 서버가 쓰는 kubeconfig는
여전히 `~/.kube/config`, 즉 kind 기본값인 **cluster-admin**이다.

- 설정 한 줄(include 제거, 비파괴 모드 해제)만 바뀌어도 바로 쓰기 권한이 생긴다.
- 서버 구현에 버그가 있거나 도구 인자를 악용하면 권한 전체가 노출된다.
- Secret 조회는 "읽기"라서 비파괴 모드에서도 허용된다.

그래서 **권한 자체를 API 서버에서 막기로** 했다.

## 2. 읽기 전용 ServiceAccount 만들기

`k8s/hermes-readonly.yaml`

```yaml
# Hermes kubernetes MCP 용 읽기 전용 계정.
# 권한: 내장 ClusterRole `view` (Secret 제외 네임스페이스 리소스 조회) + 노드 등 클러스터 리소스 조회.
# 쓰기 동사(create/update/patch/delete)와 exec/port-forward, Secret 조회는 API 서버에서 거부된다.
apiVersion: v1
kind: Namespace
metadata:
  name: hermes
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: hermes-readonly
  namespace: hermes
---
# 만료 없는 SA 토큰 (lab 용). 폐기하려면 이 Secret 을 지우면 토큰이 즉시 무효가 된다.
apiVersion: v1
kind: Secret
metadata:
  name: hermes-readonly-token
  namespace: hermes
  annotations:
    kubernetes.io/service-account.name: hermes-readonly
type: kubernetes.io/service-account-token
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: hermes-readonly-view
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: view
subjects:
  - kind: ServiceAccount
    name: hermes-readonly
    namespace: hermes
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: hermes-readonly-cluster
rules:
  - apiGroups: [""]
    resources: [nodes, persistentvolumes]
    verbs: [get, list, watch]
  - apiGroups: [storage.k8s.io]
    resources: [storageclasses]
    verbs: [get, list, watch]
  - apiGroups: [metrics.k8s.io]
    resources: [nodes, pods]
    verbs: [get, list]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: hermes-readonly-cluster
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: hermes-readonly-cluster
subjects:
  - kind: ServiceAccount
    name: hermes-readonly
    namespace: hermes
```

### 설계 포인트

- **내장 `view` ClusterRole 재사용.** 네임스페이스 리소스(Pod, Deployment, Service, Event, Ingress…)를
  조회할 수 있고, **Secret은 의도적으로 빠져 있다.** 직접 만든 규칙보다 실수할 여지가 적다.
- **`view`에 없는 클러스터 범위 리소스만 따로 추가.** 노드, PV, StorageClass, metrics를 넣었다.
  에이전트가 "노드 상태 봐줘"에 답할 수 있어야 하기 때문이다.
- **장기 토큰 Secret.** 1.24부터 SA 토큰이 자동으로 생기지 않는다. lab이라 편의상
  `service-account-token` 타입 Secret으로 만료 없는 토큰을 발급했다.
  운영 환경이라면 `kubectl create token --duration`으로 짧은 토큰을 쓰는 편이 낫다.
- **폐기가 쉽다.** Secret을 지우면 토큰이 즉시 무효가 된다.

## 3. 토큰으로 kubeconfig 만들기

매번 손으로 만들지 않도록 스크립트로 만들었다. `k8s/make-readonly-kubeconfig.sh`

```bash
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
```

실행:

```console
$ k8s/make-readonly-kubeconfig.sh kind-lab
namespace/hermes created
serviceaccount/hermes-readonly created
secret/hermes-readonly-token created
clusterrolebinding.rbac.authorization.k8s.io/hermes-readonly-view created
clusterrole.rbac.authorization.k8s.io/hermes-readonly-cluster created
clusterrolebinding.rbac.authorization.k8s.io/hermes-readonly-cluster created
작성: /Users/<user>/.kube/hermes-readonly.yaml (context kind-lab-readonly)
```

만들어진 kubeconfig (**토큰과 CA는 마스킹**):

```yaml
apiVersion: v1
kind: Config
clusters:
- name: kind-lab
  cluster:
    server: https://127.0.0.1:51023
    certificate-authority-data: <MASKED-CA-BASE64>
users:
- name: hermes-readonly
  user:
    token: <MASKED-SA-TOKEN>
contexts:
- name: kind-lab-readonly
  context:
    cluster: kind-lab
    user: hermes-readonly
current-context: kind-lab-readonly
```

> ⚠️ 이 파일에는 만료 없는 토큰이 들어 있다. 권한을 600으로 두고, git 저장소에는 **절대 넣지 않는다.**
> 저장소에는 RBAC 매니페스트와 생성 스크립트만 올리고, 장비마다 스크립트를 실행해 만든다.

생성된 리소스:

```console
$ kubectl -n hermes get sa,secret
NAME                             AGE
serviceaccount/default           5m
serviceaccount/hermes-readonly   5m

NAME                           TYPE                                  DATA   AGE
secret/hermes-readonly-token   kubernetes.io/service-account-token   3      5m

$ kubectl get clusterrolebinding hermes-readonly-view hermes-readonly-cluster -o wide
NAME                      ROLE                                  SERVICEACCOUNTS
hermes-readonly-view      ClusterRole/view                      hermes/hermes-readonly
hermes-readonly-cluster   ClusterRole/hermes-readonly-cluster   hermes/hermes-readonly
```

## 4. MCP 서버가 읽기 전용 kubeconfig를 쓰게 하기

`mcp-server-kubernetes`는 kubeconfig를 다음 우선순위로 찾는다(소스 `utils/kubernetes-manager.js` 기준).

1. `KUBECONFIG_YAML` (문자열)
2. `KUBECONFIG_JSON`
3. `K8S_SERVER` + `K8S_TOKEN`
4. in-cluster
5. **`KUBECONFIG_PATH` (파일 경로)** ← 이걸 사용
6. `KUBECONFIG`
7. 기본값 `~/.kube/config`

`KUBECONFIG_YAML`을 쓰면 설정 파일에 토큰이 박히니, 파일 경로를 넘기는 5번을 골랐다.
Hermes는 MCP 서브프로세스에 환경 변수를 거의 넘기지 않으므로 `env`에 직접 적는다.

```yaml
mcp_servers:
  kubernetes:
    command: npx
    args: [-y, mcp-server-kubernetes]
    env:
      ALLOW_ONLY_NON_DESTRUCTIVE_TOOLS: "true"
      KUBECONFIG_PATH: ${userHome}/.kube/hermes-readonly.yaml   # 추가
    tools:
      include: [kubectl_get, kubectl_describe, kubectl_logs, explain_resource]
```

`${userHome}`은 Hermes가 홈 디렉터리로 바꿔 준다. 그래서 장비마다 사용자 이름이 달라도 같은
설정 파일을 그대로 쓸 수 있다. 적용은 데스크톱 앱 재시작으로 한다(새 채팅만으로는 MCP 서버가 다시 뜨지 않는다). 메시징 게이트웨이를 쓰면 `hermes gateway restart`도 한다.

## 5. 검증

### RBAC

```console
$ export RO=~/.kube/hermes-readonly.yaml
$ for v in "create deployments" "delete pods" "patch deployments" \
           "create pods/exec" "get secrets" "list pods" "get nodes"; do
    printf "%-22s %s\n" "$v" "$(kubectl --kubeconfig $RO auth can-i $v -A)"
  done
create deployments     no
delete pods            no
patch deployments      no
create pods/exec       no
get secrets            no
list pods              yes
get nodes              yes
```

실제 삭제 요청도 API 서버에서 거부된다(`--dry-run=server`라서 성공하더라도 실제로는 지워지지 않는다).

```console
$ kubectl --kubeconfig $RO -n default delete pod echo-557947968f-kr2k9 --dry-run=server
Error from server (Forbidden): pods "echo-557947968f-kr2k9" is forbidden:
User "system:serviceaccount:hermes:hermes-readonly" cannot delete resource "pods"
in API group "" in the namespace "default"
```

### MCP 서버 경유

MCP 서버를 `KUBECONFIG_PATH`만 넣고 직접 띄워 JSON-RPC로 도구를 호출해 봤다.

| 호출 | 결과 |
|---|---|
| `kubectl_get pods -n default` | `pod/echo-...` 3개 정상 조회 |
| `kubectl_get secrets -n default` | `Forbidden: User "system:serviceaccount:hermes:hermes-readonly" cannot list resource "secrets"` |

에러 메시지의 주체가 `system:serviceaccount:hermes:hermes-readonly`로 찍히는 것으로,
MCP 서버가 admin이 아니라 새 SA로 접속한다는 것을 확인했다.

## 6. 정리: 방어선이 3겹이 됐다

| 층 | 장치 | 막는 것 |
|---|---|---|
| 에이전트 | `tools.include` 화이트리스트 | 쓰기 도구가 에이전트에 아예 안 보임 |
| MCP 서버 | `ALLOW_ONLY_NON_DESTRUCTIVE_TOOLS` | 서버가 delete 계열 도구를 등록하지 않음 |
| **API 서버** | **읽기 전용 SA + RBAC `view`** | **위 두 개가 뚫려도 쓰기·exec·Secret 조회 거부** |

앞의 두 개는 "설정"이고 마지막은 "권한"이다. 설정은 실수로 바뀔 수 있지만, 권한이 없으면 할 수 없다.

## 남은 구멍: 에이전트의 터미널

MCP는 막았지만, 에이전트에게 **터미널 도구**가 있으면 이야기가 다르다. 터미널은 내 계정으로 돌기 때문에
`~/.kube/config`(admin)를 그대로 읽을 수 있다. 막는 방법은 강도 순서로 다음과 같다.

1. **명령 거부 규칙** (`approvals.deny`에 `kubectl *delete*` 등 패턴 추가): 간단하지만 문자열 비교라서
   `curl`로 API를 직접 호출하면 우회된다. 실수 방지용이다.
2. **터미널을 Docker 백엔드로 실행**: 컨테이너에 읽기 전용 kubeconfig만 마운트한다. admin 인증서가
   아예 보이지 않으니 확실하다.
3. **해당 프로필에서 터미널 도구 끄기**: 가장 단순하고 확실하지만 다른 작업도 못 하게 된다.

## 되돌리기

```bash
kubectl -n hermes delete secret hermes-readonly-token   # 토큰만 즉시 폐기
kubectl delete -f k8s/hermes-readonly.yaml              # 전부 삭제
rm ~/.kube/hermes-readonly.yaml
```
