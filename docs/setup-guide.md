# KOA 1단계 — Hermes · MCP 설정 가이드

KOA 관찰 계층(K8s + OpenSearch)을 Hermes에 붙이는 절차. **이 저장소에 실제로 적용된 구성**을 기준으로 한다.
(claude.ai 대화의 초안 가이드를 실제 구성에 맞게 고친 버전)

## 확정 구성

| 항목 | 값 |
|---|---|
| Hermes | v0.21.5 (데스크톱). KOA 는 profile distribution 으로 설치 → `~/.hermes/profiles/koa/` (2026-10-05 전에는 `install.sh` + 심볼릭 링크) |
| K8s MCP | **flux159 [`mcp-server-kubernetes`](https://www.npmjs.com/package/mcp-server-kubernetes) v4.1.9** (`npx`) |
| K8s MCP 서버 모드 | `ALLOW_ONLY_NON_DESTRUCTIVE_TOOLS=true` |
| K8s 노출 도구 | `kubectl_get`, `kubectl_describe`, `kubectl_logs`, `explain_resource` (4개) |
| K8s 접속 계정 | SA `hermes/hermes-readonly` (ClusterRole `view` + 노드·PV·SC·metrics 조회) |
| K8s kubeconfig | `~/.kube/hermes-readonly.yaml` (`KUBECONFIG_PATH`, 600, 커밋 금지) |
| OpenSearch MCP | [`opensearch-mcp-server-py`](https://pypi.org/project/opensearch-mcp-server-py/) v0.11.0 (`uvx`), 서버 OpenSearch 3.8.0 |
| OpenSearch 접속 | Ingress + Basic Auth, 계정 `hermes-readonly`, `OPENSEARCH_SETTINGS_ALLOW_WRITE=false` |
| OpenSearch 노출 도구 | `ListIndexTool`, `IndexMappingTool`, `SearchIndexTool`, `GetShardsTool`, `ClusterHealthTool` (5개) |
| 비밀 값 | 프로필의 `.env` (`~/.hermes/profiles/koa/.env`, 600). `config.yaml`에는 `${VAR}`만 쓴다 |
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
hermes profile install github.com/leejw1212/koa --alias   # → ~/.hermes/profiles/koa, 명령 `koa`
```

MCP 는 이 가이드처럼 손으로 넣지 않고, KOA 프로필에서 "클러스터 확인하고 MCP 세팅해줘" 로 온보딩한다
(스킬 `koa-cluster-discovery`, 정의는 `koa/catalog.yaml`). 아래 절들은 그 정의가 어떻게 정해졌는지의 기록이다.

필요한 것: Node.js(`npx`), `uv`(`uvx`), `kubectl`, 클러스터 관리자 컨텍스트(SA 생성용 1회).

## 1. 읽기 전용 ServiceAccount + kubeconfig

```bash
~/.hermes/profiles/koa/k8s/make-readonly-kubeconfig.sh kind-lab   # 관리자 컨텍스트 지정
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

적용: **데스크톱 앱 재시작.** 새 채팅만으로는 MCP 서버가 다시 뜨지 않는다(확인함: 새 채팅에서는 `prompts/resources: false` 가 반영되지 않았고, 앱 재시작 후 반영됨).
메시징 게이트웨이(`hermes gateway`)를 쓰면 그쪽도 `hermes gateway restart` — 게이트웨이는 앱과 별도로 MCP 서버를 띄운다.

확인: 도구 목록에 `mcp__kubernetes__*` 가 4개(`kubectl_get/describe/logs`, `explain_resource`)만 있어야 한다.
`list_prompts`, `get_prompt`, `list_resources`, `read_resource` 가 보이면 아직 재시작 전이다.
프로세스로 확인: `ps -axo lstart,command | grep -E 'mcp-server-kubernetes|opensearch-mcp-server'` → 고정 버전(`@4.1.9`, `@0.11.0`)과 재시작 시각.

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

## 터미널 도구의 kubectl

MCP 경로가 잠겨 있어도 에이전트의 터미널 도구는 별개다. 그대로 두면 `~/.kube/config`(admin)를 쓴다.
(실측: 터미널에서 `kubectl auth can-i delete pods` → `yes`, `list secrets` → `yes`)

조치: 에이전트 터미널의 기본 `KUBECONFIG`를 읽기 전용 kubeconfig로 고정한다.

```yaml
# config.yaml (hermes config set terminal.shell_init_files '[...]' 로 넣음)
terminal:
  shell_init_files:
    - ~/.profile          # 목록을 명시하면 기본 자동 source 가 꺼지므로 기본 3개를 함께 적는다
    - ~/.bash_profile
    - ~/.bashrc
    - ~/.hermes/profiles/koa/terminal/agent-env.sh   # export KUBECONFIG=$HOME/.kube/hermes-readonly.yaml
```

- 세션 시작 시 터미널 환경 스냅샷을 만들 때 source 된다 → **새 채팅부터** 적용, 진행 중인 채팅은 그대로.
- 사람의 셸(`~/.zshrc`)에는 영향 없다.

확인 (새 채팅의 터미널에서):

```bash
kubectl config current-context        # kind-lab-readonly
kubectl auth can-i delete pods -A     # no
kubectl auth can-i list secrets -A    # no
```

**한계:** 기본값을 바꿀 뿐 보안 경계가 아니다. 에이전트가 `--kubeconfig ~/.kube/config`나 `KUBECONFIG=...`를 직접 지정하면 admin으로 접근할 수 있다.
admin kubeconfig 파일이 같은 계정에 있는 한 완전히 막을 수는 없다. 완전히 막으려면 이 프로필에서 터미널 도구를 끄거나,
터미널 백엔드를 docker로 바꿔 `~/.kube/config`를 마운트하지 않아야 한다. 상세: [blog-k8s-readonly-mcp.md](blog-k8s-readonly-mcp.md)의 "남은 구멍" 절.
