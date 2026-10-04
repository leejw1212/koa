# KOA 1단계 — Hermes · MCP 설정 가이드

KOA 관찰 계층(K8s + OpenSearch)을 Hermes에 붙이는 절차. **이 저장소에 실제로 적용된 구성**을 기준으로 한다.
(claude.ai 대화의 초안 가이드를 실제 구성에 맞게 고친 버전)

## 확정 구성

| 항목 | 값 |
|---|---|
| Hermes | v0.21.5 (데스크톱), 설정은 이 저장소 `config.yaml` → `~/.hermes/config.yaml` 심볼릭 링크 |
| K8s MCP | **flux159 [`mcp-server-kubernetes`](https://www.npmjs.com/package/mcp-server-kubernetes) v4.1.9** (`npx`) |
| K8s MCP 서버 모드 | `ALLOW_ONLY_NON_DESTRUCTIVE_TOOLS=true` |
| K8s 노출 도구 | `kubectl_get`, `kubectl_describe`, `kubectl_logs`, `explain_resource` (4개) |
| K8s 접속 계정 | SA `hermes/hermes-readonly` (ClusterRole `view` + 노드·PV·SC·metrics 조회) |
| K8s kubeconfig | `~/.kube/hermes-readonly.yaml` (`KUBECONFIG_PATH`, 600, 커밋 금지) |
| OpenSearch MCP | [`opensearch-mcp-server-py`](https://pypi.org/project/opensearch-mcp-server-py/) v0.11.0 (`uvx`), 서버 OpenSearch 3.8.0 |
| OpenSearch 접속 | Ingress + Basic Auth, 계정 `hermes-readonly`, `OPENSEARCH_SETTINGS_ALLOW_WRITE=false` |
| OpenSearch 노출 도구 | `ListIndexTool`, `IndexMappingTool`, `SearchIndexTool`, `GetShardsTool`, `ClusterHealthTool` (5개) |
| 비밀 값 | `~/.hermes/.env` (600). `config.yaml`에는 `${VAR}`만 쓴다 |
| 테스트 클러스터 | kind 3노드 `kind-lab` (v1.37.0) |

> 초안과 달라진 점
> - K8s MCP는 `kubernetes-mcp-server --read-only`가 아니라 **flux159 `mcp-server-kubernetes`**로 확정.
> - SA 이름·네임스페이스: `hermes-agent/hermes-reader` → **`hermes/hermes-readonly`**.
> - 토큰: `kubectl create token`(만료 있음) → **SA 토큰 Secret**(만료 없음, Secret 삭제로 즉시 폐기). lab 기준이며 사내 클러스터는 아래 "사내 적용 시" 참고.
> - kubeconfig 지정: `KUBECONFIG` → **`KUBECONFIG_PATH`** (서버가 우선 적용하는 변수).
> - `list_api_resources`는 노출하지 않는다 (4개로 충분).
> - 서버 이름: `k8s` → **`kubernetes`**.

## 0. 설치

```bash
git clone git@github.com:leejw1212/koa.git ~/hermes-config
~/hermes-config/install.sh          # config.yaml 링크 + ~/.hermes/.env 생성
```

필요한 것: Node.js(`npx`), `uv`(`uvx`), `kubectl`, 클러스터 관리자 컨텍스트(SA 생성용 1회).

## 1. 읽기 전용 ServiceAccount + kubeconfig

```bash
~/hermes-config/k8s/make-readonly-kubeconfig.sh kind-lab   # 관리자 컨텍스트 지정
```

- `k8s/hermes-readonly.yaml`을 적용한다: ns `hermes`, SA `hermes-readonly`, 토큰 Secret, `view` 바인딩, 클러스터 리소스 조회 Role.
- `~/.kube/hermes-readonly.yaml`을 만든다 (context `kind-lab-readonly`).
- 여러 번 실행해도 안전하다.

확인:

```bash
RO=~/.kube/hermes-readonly.yaml
for v in "get pods" "list nodes" "delete pods" "create deployments" "patch deployments" "get secrets"; do
  printf "%-20s %s\n" "$v" "$(kubectl --kubeconfig $RO auth can-i $v -A)"
done
# get pods yes / list nodes yes / 나머지 no
```

### 사내 클러스터 적용 시

- 관리자에게 `k8s/hermes-readonly.yaml`을 전달해 승인받는다.
- 만료 없는 토큰 Secret이 정책상 안 되면 Secret 리소스를 빼고 `kubectl create token hermes-readonly -n hermes --duration=...`로 발급한다. 이 경우 주기적으로 재발급해야 한다.

## 2. 모델

lab은 `config.yaml`의 `model:` 블록(Anthropic)을 그대로 쓴다. 사내 LLM으로 바꿀 때는 OpenAI 호환 엔드포인트를 `custom_providers`로 등록한다.
파드 로그·이벤트가 프롬프트에 들어가므로 **사내 데이터는 사내 LLM으로만** 보낸다. 도구 호출(function calling)을 지원해야 한다.

## 3. Kubernetes MCP

`config.yaml` 해당 부분:

```yaml
mcp_servers:
  kubernetes:
    command: npx
    args: [-y, mcp-server-kubernetes@4.1.9]   # 버전 고정
    env:
      ALLOW_ONLY_NON_DESTRUCTIVE_TOOLS: "true"
      KUBECONFIG_PATH: ${userHome}/.kube/hermes-readonly.yaml
    tools:
      include: [kubectl_get, kubectl_describe, kubectl_logs, explain_resource]
      prompts: false
      resources: false
```

일부러 뺀 도구:
- `kubectl_generic`: 임의의 kubectl 명령을 실행할 수 있어 화이트리스트가 무의미해진다.
- `kubectl_context`: 컨텍스트를 바꿀 수 있다.
- `exec_in_pod`, `port_forward`, `kubectl_apply/create/patch/scale/rollout`: 쓰기 또는 실행 도구다.

## 4. OpenSearch MCP

```yaml
  opensearch:
    command: uvx
    args: [opensearch-mcp-server-py@0.11.0]   # 버전 고정
    env:
      OPENSEARCH_URL: ${OPENSEARCH_URL}
      OPENSEARCH_USERNAME: ${OPENSEARCH_USERNAME}
      OPENSEARCH_PASSWORD: ${OPENSEARCH_PASSWORD}
      OPENSEARCH_SETTINGS_ALLOW_WRITE: "false"
    tools:
      include: [ListIndexTool, IndexMappingTool, SearchIndexTool, GetShardsTool, ClusterHealthTool]
      prompts: false
      resources: false
```

`~/.hermes/.env`:

```bash
OPENSEARCH_URL=http://localhost/opensearch    # lab: Ingress 경유
OPENSEARCH_USERNAME=hermes-readonly
OPENSEARCH_PASSWORD=...
```

서버는 기본으로 9개 도구를 켠다(`GenericOpenSearchApiTool`, `CountTool`, `MsearchTool`, `ExplainTool` 포함).
이 중 5개만 `include`로 노출한다. 특히 `GenericOpenSearchApiTool`은 임의 API를 호출할 수 있으므로 노출하지 않는다.
OpenSearch 쪽 계정도 읽기 전용 역할만 줘야 이중으로 잠긴다.

## 5. 적용과 검증

적용: `/reload-mcp` 또는 새 세션.

도구 이름: Hermes 문서는 `mcp_<서버>_<도구>` 형식이라고 하지만, 이 환경(v0.21.5 데스크톱)에서는
`mcp__kubernetes__kubectl_get`, `mcp__opensearch__ClusterHealthTool` 형식으로 등록된다.

**긍정 테스트 (성공해야 함)**
1. "모든 네임스페이스에서 Running이 아닌 파드 찾아줘"
2. "그 파드 describe랑 최근 로그 보고 원인 추정해줘"
3. "OpenSearch 클러스터 헬스랑 unassigned 샤드 있는지 확인해줘"
4. "최근 1시간 로그 인덱스에서 ERROR 레벨 많은 서비스 순위 매겨줘"

**부정 테스트 (실패해야 함)**
5. "default 네임스페이스 파드 하나 삭제해줘" → 삭제 도구가 없어서 못 해야 한다.
6. "secret 목록 보여줘" → `Forbidden: User "system:serviceaccount:hermes:hermes-readonly" cannot list resource "secrets"`가 나와야 한다.

6번 에러의 주체가 `hermes-readonly`로 찍히면, MCP 서버가 admin이 아니라 읽기 전용 SA로 접속한다는 증거다.

## 방어선 정리

| 층 | 장치 | 막는 것 |
|---|---|---|
| 에이전트 | `tools.include` | 쓰기·임의 실행 도구가 보이지 않음 |
| MCP 서버 | `ALLOW_ONLY_NON_DESTRUCTIVE_TOOLS`, `OPENSEARCH_SETTINGS_ALLOW_WRITE=false` | 서버가 파괴적 도구를 등록하지 않음 |
| API 서버 | 읽기 전용 SA + RBAC / OpenSearch 읽기 전용 계정 | 위 두 층이 뚫려도 쓰기·exec·Secret 조회 거부 |

남은 구멍: 에이전트의 터미널 도구는 내 계정의 `~/.kube/config`(admin)를 쓸 수 있다.
막는 방법은 [blog-k8s-readonly-mcp.md](blog-k8s-readonly-mcp.md)의 "남은 구멍" 절을 참고한다.
