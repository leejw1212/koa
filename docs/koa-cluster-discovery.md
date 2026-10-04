# KOA 첫 동작 — 클러스터 탐색과 MCP 설치

> 작성 2026-10-04 · 커밋 `2e36ffd` · 대상 lab: `kind-lab` (kind 3노드, k8s v1.37.0)

## 1. 왜 만들었나

운영 클러스터는 저마다 다르다. 지표 스택, 로그 저장소와 필드 이름, 이벤트 보존 기간, GitOps 도구는
각 클러스터 운영팀이 정하고 우리가 통제할 수 없다. 우리가 정할 수 있는 것은 **에이전트가 그 클러스터를 어떻게 파악하고
어떤 도구를 붙이느냐**다.

그래서 KOA 가 클러스터에 연결되면 가장 먼저 하는 일을 다음으로 정했다.

1. **탐색** — 클러스터를 훑어 무엇이 있고, 무엇에 접근할 수 있고, 무엇이 비어 있는지 기록한다.
2. **계획** — 그 기록을 보고 붙일 MCP 서버를 정한다.
3. **설치** — 준비가 끝난 MCP 서버만 Hermes 에 등록한다.

탐색 결과는 장애 분석 때 에이전트가 가장 먼저 읽는 자료가 된다. 매번 configmap 을 읽어 구조를 다시 파악하지 않아도 된다.

## 2. 구성

```
hermes-config/
  koa/
    discover.py    # 1) 탐색 → clusters/<이름>.yaml
    catalog.yaml   # 무엇을 찾을지 + 찾으면 어떤 MCP 를 붙일지
    plan.py        # 2) 계획, 3) --apply 로 설치
    README.md      # 사용법 요약
  clusters/
    kind-lab.yaml  # 클러스터 프로필 (자동 생성)
```

| 파일 | 줄 수 | 역할 |
|---|---|---|
| `koa/discover.py` | 371 | 읽기 전용 kubeconfig 로 클러스터를 조회해 프로필을 쓴다 |
| `koa/catalog.yaml` | 237 | 구성요소 감지 규칙 23종, MCP 서버 정의 6개, MCP 를 붙이지 않는 구성요소 안내 |
| `koa/plan.py` | 147 | 프로필 + 카탈로그 → 설치 계획. `--apply` 로 등록 |

필요한 것: `kubectl`, `python3` + PyYAML, 읽기 전용 kubeconfig(`~/.kube/hermes-readonly.yaml`, 만드는 법은 [setup-guide](setup-guide.md) 1절).

## 3. 사용법

```bash
cd ~/hermes-config
python3 koa/discover.py --probe        # 1) 탐색 → clusters/kind-lab.yaml (약 1.4초)
python3 koa/plan.py kind-lab           # 2) 계획만 보여준다. 아무것도 바꾸지 않는다
python3 koa/plan.py kind-lab --apply   # 3) verified 서버 등록
python3 koa/plan.py kind-lab --apply --with argocd   # candidate 는 이름을 명시해야 등록
```

등록 후: 데스크톱 앱 재시작(게이트웨이를 쓰면 `~/.local/bin/hermes gateway restart` 도) → `git diff config.yaml` → 커밋.

`discover.py` 옵션

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--kubeconfig` | `~/.kube/hermes-readonly.yaml` (`KOA_KUBECONFIG`) | `~/.kube/config` 를 주면 실행을 거부한다 |
| `--context` | 현재 컨텍스트 | |
| `--name` | 컨텍스트 이름에서 `-readonly` 를 뺀 것 | 프로필 파일 이름 |
| `--probe` | 끔 | 찾아낸 접근 주소의 health 경로에 GET 한 번 |
| `--out` | `clusters/<이름>.yaml` | |

## 4. 탐색이 확인하는 것

### 4.1 클러스터 기본 정보
버전, 배포판(노드 `providerID`), 컨테이너 런타임, 노드 수·Ready 수·역할, 네임스페이스·파드 수, 접속 계정.

### 4.2 권한
`kubectl auth can-i` 로 두 묶음을 검사한다.

| 묶음 | 항목 |
|---|---|
| 분석에 필요한 권한 | list pods, get pods/log, list events, list nodes, list services, list ingresses, list deployments, list configmaps, get nodes.metrics.k8s.io |
| 막혀 있어야 하는 권한 | list secrets, create pods/exec, create pods/portforward, get services/proxy, create pods, delete pods, patch deployments |

두 번째 묶음 중 하나라도 허용되면 `read_only: false` 로 기록하고 경고한다. `plan.py` 는 이 프로필로는 진행하지 않는다.

### 4.3 구성요소
파드의 **컨테이너 이미지 이름**과 클러스터의 **CRD API 그룹**으로 찾는다.

| 종류 | 감지 대상 |
|---|---|
| 지표 | prometheus, thanos, victoriametrics, kube-state-metrics, node-exporter, metrics-server |
| 경보·대시보드 | alertmanager, grafana |
| 로그 저장소 | opensearch, elasticsearch, loki |
| 로그·텔레메트리 수집기 | fluentd, fluent-bit, vector, otel-collector |
| 트레이스 | tempo, jaeger |
| GitOps | argocd, flux |
| MQ·캐시 | rabbitmq, kafka, redis |
| 기타 | ingress-nginx |

구성요소마다 다음을 기록한다: 네임스페이스와 워크로드, 이미지와 태그, 파드 수와 Ready 수, 그 파드를 선택하는 서비스와 포트,
**클러스터 밖 접근 주소**(그 서비스를 가리키는 Ingress), `--probe` 결과(HTTP 상태, 인증 필요 여부).

### 4.4 이벤트 보존
남아 있는 k8s 이벤트 수와 가장 오래된 이벤트의 나이. 사실상의 보존 기간이다.

### 4.5 빈 곳(gaps)
원인 분석에 필요한데 없는 신호를 자동으로 적는다.

- 지표 저장소 없음 / 로그 저장소 없음 / 로그 저장소는 있는데 수집기 없음
- 로그 수집기 자체 상태를 볼 신호 확인 필요 (로그가 없는 것과 수집이 끊긴 것을 구분하기 위해)
- 이벤트 보존 6시간 미만
- GitOps 없음, metrics API 없음, 트레이스 없음, 경보 시스템 없음
- 관측 구성요소인데 클러스터 밖 접근 주소가 없음

## 5. 클러스터 프로필 형식 (`koa.cluster-profile/v1`)

우리가 정한 형식이다. Hermes 의 "프로필"(`~/.hermes/profiles/`)이나 kubeconfig context 와는 관계없다.
자동 생성 파일이므로 직접 고치지 않고 `discover.py` 를 다시 실행한다.

```yaml
cluster: kind-lab
schema: koa.cluster-profile/v1
discovered_at: '2026-10-04T15:18:47Z'
kubeconfig: {context: kind-lab-readonly, identity: system:serviceaccount:hermes:hermes-readonly}
warnings: null
kubernetes: {version: v1.37.0, provider: [kind], runtime: [containerd], nodes: {...}, namespaces: 10, pods: 57}
access:
  can: [list pods, get pods/log, ...]
  cannot: [list secrets, create pods/exec, ...]
  read_only: true
apis: {metrics_api: false}
components:
  opensearch:
    kind: logs
    instances:
    - namespace: logging
      workload: StatefulSet/opensearch
      image: opensearchproject/opensearch:3.8.0
      pods: 1
      ready: 1
      services: [opensearch (http:9200)]
      access: [http://localhost/opensearch]
      probe: [{url: http://localhost/opensearch/, reachable: true, http_status: 401, auth_required: true}]
events: {count: 3, oldest_age: 19m}
gaps: [...]
```

## 6. MCP 카탈로그와 설치 규칙

### 6.1 서버 목록

| MCP | 패키지(고정 버전) | 붙는 조건 | 상태 | 읽기 전용 장치 |
|---|---|---|---|---|
| kubernetes | `mcp-server-kubernetes@4.1.9` | k8s 조회 권한 | **verified** | 읽기 전용 SA kubeconfig, `ALLOW_ONLY_NON_DESTRUCTIVE_TOOLS`, 조회 도구 4개만 |
| opensearch | `opensearch-mcp-server-py@0.11.0` | opensearch 감지 | **verified** | `OPENSEARCH_SETTINGS_ALLOW_WRITE=false`, 조회 도구 5개만, OpenSearch 읽기 전용 역할 |
| prometheus | `prometheus-mcp-server@1.6.2` | prometheus 감지 | candidate | PromQL 은 쓰기를 표현할 수 없음, 조회 도구만 |
| grafana | `mcp-grafana@2.0.0` | grafana 감지 | candidate | `--disable-write`, Viewer 서비스 계정 |
| argocd | `argocd-mcp@0.9.0` | argocd 감지 | candidate | `MCP_READ_ONLY=true`, 조회 도구 5개만, get/list role 토큰 |
| rabbitmq | `amq-mcp-server-rabbitmq@4.0.0` | rabbitmq 감지 | candidate | `--allow-mutative-tools` 없음, read/observability/health 그룹만, monitoring 태그 계정 |

모든 서버에 `prompts: false`, `resources: false` 를 둔다.

### 6.2 상태의 뜻
- **verified** — lab 에서 직접 붙여 도구 목록과 쓰기 차단을 확인했다. `--apply` 로 등록된다.
- **candidate** — 각 프로젝트 README 로 읽기 전용 설정만 확인했다. `--with <이름>` 을 줘야 등록된다.

### 6.3 plan.py 판단 순서
1. 프로필이 `read_only: true` 가 아니면 중단한다.
2. 카탈로그의 `when` 조건(권한 또는 구성요소)이 맞는 서버만 후보로 올린다.
3. `requires_env` 변수가 `~/.hermes/.env` 나 환경에 있는지 본다. **값은 읽어 출력하지 않고 있는지만 확인한다.**
   없으면 할 일로 안내하고, 프로필에서 찾은 접근 주소를 제안값으로 보여준다.
4. 현재 `config.yaml` 과 비교해 `유지` / `등록` / `보류(준비 필요)` / `보류(candidate)` 를 정한다.
5. `--apply` 면 `~/.local/bin/hermes config set mcp_servers.<이름> '<json>'` 으로 등록한다.
   다른 설치본의 `hermes` 를 쓰면 게이트웨이 서비스 정의가 깨진 적이 있어 경로를 고정했다.

### 6.4 MCP 를 붙이지 않는 구성요소
| 구성요소 | 이유와 대안 |
|---|---|
| fluentd / fluent-bit | 전용 MCP 없음. 자체 지표 → Prometheus 로 보는 것이 정석 |
| loki | Grafana MCP 의 Loki 데이터소스로 조회 |
| thanos / victoriametrics | Prometheus 호환 API → prometheus MCP 의 `PROMETHEUS_URL` 로 지정 |
| elasticsearch, kafka, redis | 카탈로그에 아직 없음 (검증 필요) |

## 7. kind-lab 실행 결과

```
클러스터 kind-lab-readonly  (v1.37.0, 노드 3/3 Ready, 파드 57)
계정 system:serviceaccount:hermes:hermes-readonly  읽기전용=True
```

| 구성요소 | 위치 | 접근 주소 (probe) | MCP 계획 |
|---|---|---|---|
| (k8s 조회 권한) | – | – | kubernetes — 등록됨, 유지 |
| opensearch | logging/opensearch | `http://localhost/opensearch` (401, 인증 필요) | opensearch — 등록됨, 유지 |
| argocd | argocd/* (5개 워크로드) | `http://localhost/argocd` (200) | 보류 — `ARGOCD_BASE_URL`, `ARGOCD_API_TOKEN` 필요 |
| rabbitmq | linkcard/rabbitmq | 없음 | 보류 — 접근 주소와 `RABBITMQ_MANAGEMENT_ENDPOINT` 필요 |
| fluentd | logging/fluentd-aggregator, fluentd-forwarder | – | 붙이지 않음 |
| redis | argocd/argocd-redis, linkcard/redis | – | 붙이지 않음 |
| ingress-nginx | ingress-nginx/controller | – | 해당 없음 |

찾은 빈 곳

- 지표 저장소 없음 → 추세, 큐 적체, 처리량을 볼 수 없다
- 로그 수집기 자체 상태를 볼 신호 확인 필요
- k8s 이벤트 보존 약 20분
- metrics API 없음
- 트레이스 없음, 경보 시스템 없음
- rabbitmq 접근 주소 없음 (읽기 전용 SA 는 port-forward 불가)

## 8. 검증한 것

| 항목 | 결과 |
|---|---|
| 읽기 전용 kubeconfig 로 탐색 | 정상, 약 1.4초 |
| 권한 판정 | `read_only: true`, 쓰기·exec·port-forward·Secret·services/proxy 모두 거부 확인 |
| `--kubeconfig ~/.kube/config` 로 실행 | 실행 거부 (exit 1) |
| `--probe` | argocd 200, opensearch 401(인증 필요)로 기록 |
| `plan.py` 계획 | verified 2개 유지, candidate 2개 보류(할 일 안내), 붙이지 않는 2개 안내 |
| 등록 경로 | `hermes config set` 을 같은 값으로 실행 → `config.yaml` 변화 없음, 심볼릭 링크 유지 |
| 프로필 비밀 정보 | 토큰·비밀번호 없음 |

만드는 중 잡은 버그

1. **권한 오판** — `kubectl auth can-i get services/proxy` 는 서브리소스가 아니라 "이름이 proxy 인 services" 로 해석돼 `yes` 가 나왔다.
   그래서 읽기 전용 계정을 쓰기 가능으로 잘못 판정했다. `pods/log` 같은 서브리소스는 `--subresource` 로 분리해 검사하도록 고쳤다.
2. **접근 주소 오판** — argocd 처럼 워크로드가 여러 개면, 그중 하나라도 주소가 없을 때 "접근 주소 없음" 으로 표시했다.
   구성요소의 모든 인스턴스에 주소가 없을 때만 표시하도록 고쳤다.

## 9. 안전 장치 요약

- 클러스터에는 get/list 와 `auth can-i` 만 보낸다.
- 관리자 kubeconfig 로는 실행하지 않는다.
- `--probe` 는 관측 시스템의 health 경로에만 GET 한 번. 앱 경로로는 보내지 않는다.
- 쓰기 권한이 하나라도 보이면 계획 단계에서 중단한다.
- candidate 는 명시하지 않으면 등록하지 않는다.
- 비밀 값은 `.env` 에만 두고 스크립트는 있는지만 확인한다.

## 10. 한계

- **candidate 4개는 아직 실제로 붙여 보지 않았다.** 도구 이름과 읽기 전용 플래그는 README 기준이다.
- **rabbitmq MCP 는 kind 에서 쓸 수 없다.** SSRF 방지로 localhost·사설 IP 접속을 막는다.
- **클러스터 밖 관측 시스템**(Datadog, Grafana Cloud, Elastic Cloud 등)은 클러스터만 봐서는 주소를 알 수 없다.
- **접근 주소는 Ingress 만 본다.** LoadBalancer, NodePort, VPN 너머 주소는 직접 지정해야 한다.
- **이미지 이름 기반 감지**라 사내 미러로 이미지 이름이 바뀌면 놓칠 수 있다. `catalog.yaml` 의 `image` 정규식을 늘린다.
- 로그 인덱스 패턴과 필드 이름(서비스·request_id·오류 필드)은 아직 수집하지 않는다.
- 회사 클러스터 프로필에는 내부 호스트 이름이 들어간다. `clusters/` 를 git 에 커밋할지 정해야 한다.

## 11. 다음 단계

1. candidate 를 하나씩 verified 로 올린다 (argocd 부터).
   읽기 전용 계정 발급 → `.env` → `--apply --with` → 도구 목록과 쓰기 거부 확인 → `status: verified`.
2. 프로필에 로그 스키마(인덱스 패턴, 주요 필드 이름)를 추가한다. opensearch MCP 로 매핑을 읽어 채운다.
3. 분석 런북 skill 이 "분석 전에 `clusters/<이름>.yaml` 을 먼저 읽는다" 를 따르게 한다.
4. 장애 재현 시나리오로 탐색·분석 성능을 측정한다.
