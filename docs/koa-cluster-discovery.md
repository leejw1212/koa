# KOA 첫 동작 — 클러스터 탐색과 MCP 설치

> 작성 2026-10-04 · 커밋 `2e36ffd` · 대상 lab: `kind-lab` (kind 3노드, k8s v1.37.0)

## 1. 왜 만들었나

운영 클러스터는 저마다 다르다. 지표 스택, 로그 저장소와 필드 이름, 이벤트 보존 기간, GitOps 도구는
각 클러스터 운영팀이 정하고 우리가 통제할 수 없다. 우리가 정할 수 있는 것은 **에이전트가 그 클러스터를 어떻게 파악하고
어떤 도구를 붙이느냐**다.

> **원칙: KOA 는 클러스터를 바꾸지 않는다.** 탐색 후 지금 쓸 수 있는 범위 안에서만 수집·분석한다.
> KOA 가 하는 변경은 우리 쪽 MCP 설치(등록)뿐이다. 지표 켜기, Ingress 열기, 구성요소 설치 같은 클러스터 설정 변경은
> 제안하지 않는다. 탐색에서 찾은 빈 곳은 "고칠 것"이 아니라 "분석할 때 감안할 한계"로 다룬다.

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

보통은 KOA 프로필에서 "클러스터 확인하고 MCP 세팅해줘" 라고 하면 에이전트가 아래를 순서대로 돌린다 (14절).

```bash
cd "$HERMES_HOME"                       # 설치본: ~/.hermes/profiles/koa, 개발: 저장소 루트
python3 koa/discover.py --probe        # 1) 탐색 → local/clusters/kind-lab.yaml (개발 체크아웃은 clusters/)
python3 koa/plan.py kind-lab           # 2) 계획만 보여준다. 아무것도 바꾸지 않는다
python3 koa/plan.py kind-lab --apply   # 3) verified 서버 등록
python3 koa/plan.py kind-lab --apply --with argocd   # candidate 는 이름을 명시해야 등록
```

등록 후: 데스크톱 앱 재시작(게이트웨이를 쓰면 `~/.local/bin/hermes gateway restart` 도).

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
보존 기간은 kube-apiserver 의 `--event-ttl` 플래그(없으면 기본 1h)로 판단한다. API 서버 파드가 보이지 않는 관리형(EKS/GKE/AKS)은
"미확인"으로 적고, 참고용으로 남아 있는 이벤트 수와 가장 오래된 이벤트의 나이를 함께 기록한다.
남은 이벤트 나이만으로 보존 기간을 추정하면 틀린다(8절 버그 3).

### 4.5 빈 곳(gaps) = 분석 한계
원인 분석에 필요한데 없는 신호를 자동으로 적는다. 고칠 대상이 아니라 분석할 때 감안할 한계이고,
보고서에는 한계마다 지금 쓸 수 있는 도구로 메우는 방법(KOA 대응)을 함께 적는다.

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
events: {count: 3, oldest_age: 11.1h, ttl_source: kube-apiserver 기본값 (--event-ttl 없음), ttl: 1h}
gaps: [...]
```

## 6. MCP 카탈로그와 설치 규칙

### 6.1 서버 목록

| MCP | 패키지(고정 버전) | 붙는 조건 | 상태 | 읽기 전용 장치 |
|---|---|---|---|---|
| kubernetes | `mcp-server-kubernetes@4.1.9` | k8s 조회 권한 | **verified** | 읽기 전용 SA kubeconfig, `ALLOW_ONLY_NON_DESTRUCTIVE_TOOLS`, 조회 도구 4개만 |
| opensearch | `opensearch-mcp-server-py@0.11.0` | opensearch 감지 | **verified** | `OPENSEARCH_SETTINGS_ALLOW_WRITE=false`, 조회 도구 5개만, OpenSearch 읽기 전용 역할 |
| prometheus | `prometheus-mcp-server@1.6.2` | prometheus 감지 | **verified** | PromQL 은 쓰기를 표현할 수 없음, 조회 도구만 |
| grafana | `mcp-grafana@2.0.0` | grafana 감지 | **verified** | `--disable-write`, Viewer 서비스 계정, 조회 도구 22개만 |
| argocd | `peopleforrester/mcp-k8s-observability-argocd-server` @`6ba6d1b` (git) | argocd 감지 | **verified** | `MCP_READ_ONLY=true`, 조회 도구 5개만, get/list role 토큰 |
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
| fluentd / fluent-bit | 전용 MCP 없음 → kubernetes MCP 로 파드 로그를 본다 |
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
- k8s 이벤트 보존 1h (kube-apiserver 기본값)
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
3. **이벤트 보존 오판** (2026-10-05 재실행에서 발견) — 처음에는 남은 이벤트 중 가장 오래된 것의 나이를 보존 기간으로 봤다.
   하룻밤 뒤 다시 돌리자 11.1시간 된 이벤트가 남아 있어 "보존 짧음" 항목이 빈 곳에서 사라졌다.
   실제 설정은 `--event-ttl` 없음, 즉 기본값 1h 다. 1h 가 지난 이벤트가 왜 남았는지는 확인하지 못했다
   (노트북 절전이나 docker 일시정지로 etcd lease 시간이 멈췄을 가능성).
   API 서버 플래그를 읽도록 고쳤다.

### 재실행 결과 (2026-10-05)

| 단계 | 결과 |
|---|---|
| `discover.py --probe` | 1.2초. 구성요소·접근 주소·probe 결과가 전날과 같음 (클러스터 변화 없음) |
| `plan.py` | kubernetes·opensearch 유지, argocd·rabbitmq 보류(할 일 안내) |
| `plan.py --apply` | "등록할 것이 없다", `config.yaml` 변화 없음 |
| `plan.py --apply --with argocd` | `.env` 값이 없어 등록 안 함 (의도대로) |

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

## 11. 보고서 (2026-10-05 추가)

`discover.py` 는 프로필과 함께 `clusters/<이름>.report.md` 를 만들고 화면에도 출력한다 (`koa/report.py`).

1. **찾은 구성요소** — 종류, 위치, Ready, 클러스터 밖 주소와 probe 결과, Prometheus 가 수집하는지
2. **붙일 수 있는 MCP** — 검증 상태, 근거, 현재 등록 여부, 판단, 필요한 것
3. **분석 한계와 KOA 대응** — 영향(🔴 결론을 확정 못 할 수 있음 / 🟡 느려지거나 추정에 기댐 / ⚪ 작음),
   분석에 미치는 영향, 지금 쓸 수 있는 도구로 메우는 방법(`catalog.yaml` 의 `gap_advice.workaround`, 구성요소의 `fallback`)
4. **다음 단계 (MCP)** — 바로 등록할 MCP, 접속 정보만 받으면 붙일 수 있는 MCP, 접근 주소가 없어 붙일 수 없는 MCP.
   클러스터 설정 변경은 넣지 않는다

Prometheus 수집 여부는 `--probe` 이고 Prometheus 가 인증 없이 열려 있을 때 `/api/v1/targets` 를 읽어 판단한다.
수집 대상의 namespace + service(또는 파드 이름 접두어)가 구성요소와 맞으면 수집 중으로 본다.

## 12. MCP 붙이기 결과 (2026-10-05)

kind-lab 테스트 클러스터라 토큰을 직접 발급했다(실제 클러스터에서는 사용자에게 받는다).
발급 스크립트 `lab/kind-lab-tokens.py`, 권한 확인 `lab/kind-lab-verify-tokens.py`, 도구 목록 점검 `koa/check_mcp.py`.

| MCP | 계정 / 토큰 | 권한 확인 | 실제 도구 → 노출 | 결과 |
|---|---|---|---|---|
| prometheus | 인증 없음 (`PROMETHEUS_URL`) | 조회 200, admin API 는 서버에서 꺼짐 | 6 → 5 (`health_check` 제외) | **verified**, 등록 |
| grafana | 서비스 계정 `koa-readonly` (Viewer) | 조회 200, 대시보드·폴더 생성 403 | 62 → 22 조회 도구 | **verified**, 등록 |
| argocd | 로컬 계정 `koa-readonly` (`role:readonly`, apiKey) | 앱 3개 조회, can-i sync·delete·create·update 모두 no | 11 → 5 | 붙일 수 없음 → **MCP 교체 후 verified** (아래) |

발견한 것

1. **grafana 는 `--disable-write` 로도 도구가 62개다.** oncall·incident·pyroscope·provisioning 같은 범위 밖 도구와
   `alerting_manage_routing`(이름상 관리 도구), `grafana_api_request`(GET 전용이지만 API 전체에 열림)가 들어 있다.
   분석용 조회 도구 22개만 `include` 로 남겼다. README 만 보고 붙였다면 62개가 그대로 노출됐다.
2. **argocd-mcp 0.9.0 은 base URL 의 경로를 버린다.** `ARGOCD_BASE_URL=http://localhost/argocd` 인데
   `http://localhost/api/v1/applications` 로 요청해 같은 호스트의 다른 앱(echo)이 응답했다.
   Argo CD 를 하위 경로로 연 클러스터에서는 쓸 수 없어 등록을 되돌렸다. 카탈로그에 `url_must_be_root: true` 를 두고,
   `plan.py` 가 접근 주소가 하위 경로면 "붙일 수 없음" 으로 판정하게 했다.
3. 토큰 권한 확인은 쓰기를 실제로 시도하지 않는 방법을 우선했다 (Argo CD `can-i` API). Grafana 는 생성 요청을 보내
   403 을 확인했고, 만약 성공했다면 바로 지우도록 했다.

클러스터 쪽 변경 (lab 이라 우리가 했다): Argo CD `argocd-cm` 에 `accounts.koa-readonly: apiKey`,
`argocd-rbac-cm` 에 `g, koa-readonly, role:readonly`. Grafana 서비스 계정 `koa-readonly`(Viewer).
argocd 토큰은 `.env` 에 남아 있지만 쓰는 MCP 가 없다.

적용: `~/.local/bin/hermes gateway restart` → prometheus·grafana MCP 프로세스가 새로 뜬 것 확인. 데스크톱 앱은 재시작해야 반영된다.

### argocd MCP 교체

argoproj-labs `argocd-mcp` 대신 쓸 서버를 찾았다. 조건: 하위 경로 URL 지원, 토큰 인증(쿠키·브라우저 의존 없음), 읽기 전용 모드, 유지보수 중.

| 후보 | 하위 경로 | 인증 | 읽기 전용 | 판단 |
|---|---|---|---|---|
| **peopleforrester/mcp-k8s-observability-argocd-server** (Python, 2026-09) | ✅ `base_url=f"{url}/api/v1"` | API 토큰 | `MCP_READ_ONLY` 기본 true, 파괴 작업 별도 차단 | **채택** |
| lukleh/mcp-read-only-argocd (PyPI 0.4.1) | ✅ | 브라우저 쿠키(`argocd.token`), 401 때 Chrome 쿠키를 읽어 갱신 | 읽기만 | 제외 — 사람 브라우저 세션에 의존 |
| jz-wilson/argocd-mcp-lite (npm 0.1.0) | ❌ `new URL(url, baseUrl)` 로 같은 문제 | 토큰 | – | 제외 |
| denysvitali/argocd-mcp (Go) | – | – | 쓰기 도구 다수, 읽기 전용 모드 미확인 | 제외 |

채택한 서버 검증 결과

- PyPI 에 없어 `uvx --from git+https://...@6ba6d1b` 로 **커밋 고정**.
- 도구 15개(읽기 9, 쓰기 4, 파괴 2) → 읽기 9개만 `include`. 쓰기 도구는 목록에 남지만 `MCP_READ_ONLY=true` 면 실행 시 거부된다.
- `list_applications` → 앱 3개, `get_application_status`·`diagnose_sync_failure` 정상.
- `sync_application`(dry_run) 을 일부러 호출 → `OPERATION BLOCKED: ... read-only mode`. Argo CD 서버 로그에 쓰기 요청 없음.
- 방어선 3겹: `include`(도구 숨김) → `MCP_READ_ONLY`(서버가 거부) → `role:readonly` 토큰(Argo CD 가 거부).

## 14. 배포: Hermes profile distribution (2026-10-05)

목표: 어느 장비에서든 저장소를 받아 "클러스터 확인하고, 리스트 정리해서, 사용자와 MCP 세팅해" 라고만 하면 되게.
그동안 이 동작은 기본 프로필의 **메모리 + 로컬 스킬** 에 기대고 있었다. 둘 다 저장소에 없어서 다른 장비에서는 재현되지 않았다.

| 지식 | 전 | 후 |
|---|---|---|
| 항상 지키는 규칙 (클러스터 무변경, 읽기 전용, 토큰은 사용자에게) | 기본 프로필 메모리 | `SOUL.md` — 모든 대화의 시스템 프롬프트 |
| 작업 절차 (온보딩, 장애 분석, MCP 연결, 접근 권한) | `~/.hermes/skills/devops/` (저장소 밖) | 저장소 `skills/devops/` 4개 |
| 설정 | `config.yaml` 심볼릭 링크 + `install.sh` | `config.yaml` (MCP 는 비움 — 클러스터마다 온보딩) |
| 경로 | `~/hermes-config`, `~/.hermes/.env` 하드코딩 | `koa/paths.py` 가 설치본/개발 체크아웃을 판별 |

- 설치: `hermes profile install github.com/leejw1212/koa --alias` → 프로필 `koa`. 메모리·세션·`.env`·`local/` 은 업데이트해도 유지된다.
- 메모리는 Hermes 설계상 배포에 포함되지 않는다. 그래서 메모에 있던 KOA 규칙을 `SOUL.md` 로 옮겼다.
- 탐색 결과는 설치본에서 `local/clusters/` 에 쓴다 (`hermes profile update` 가 건드리지 않는 사용자 영역).
- `lab/`(kind-lab 관리자 스크립트)와 `clusters/`(개발용 결과)는 `distribution_owned` 에서 빼서 설치본에 들어가지 않는다.
- 이 장비의 기본 프로필은 개발용: `skills.external_dirs: [~/hermes-config/skills]` 로 저장소 스킬을 직접 읽는다 (사본 없음).

설치 검증 (로컬 디렉터리로 `koa-test` 설치)

| 항목 | 결과 |
|---|---|
| 복사된 것 | `SOUL.md`, `config.yaml`, `skills/`(4), `koa/`, `k8s/`, `terminal/`, `docs/`, `README.md`, `.env.EXAMPLE` |
| 복사 안 된 것 | `.env`, `clusters/`, `lab/` |
| 스킬 | `hermes -p koa-test skills list` → 4개 local, enabled |
| 경로 판별 | 설치본 인식, 결과 경로 `profiles/koa-test/local/clusters`, `.env` 는 프로필 것 |

## 15. 자동 등록 · 읽기 전용 확인 · 조회 (2026-10-05, v0.1.1)

사용자 결정: MCP 는 묻지 않고 자동으로 붙인다. 대신 등록 전에 계정이 읽기 전용인지 백엔드에 물어보고, 등록 후에는 실제로 한 번 조회한다.

`plan.py --apply` 순서 (서버마다)

| 단계 | 도구 | 방법 (쓰기 시도 없음) |
|---|---|---|
| 1. 읽기 전용 확인 | `koa/readonly.py` | 백엔드의 권한 질의 API |
| 2. 등록 | `hermes config set` | `fail` 이면 등록 안 함 / 이미 있으면 해제 |
| 3. 조회 확인 | `koa/query.py --probe` | 서버를 stdio 로 띄워 카탈로그 `probe` 조회 1개 |

결과는 `<이름>.mcp.yaml` 과 보고서 5절 "붙인 MCP 확인".

백엔드별 읽기 전용 확인

| MCP | 질의 | kind-lab 결과 |
|---|---|---|
| kubernetes | 관리자급 SSAR 5개 + 네임스페이스마다 SelfSubjectRulesReview | ok. Calico `networkpolicies` 쓰기 규칙이 보였지만 `calico-tiered-policy-passthrough`(모든 인증 사용자) 이고 `tier.networkpolicies` 권한이 없어 실제로는 거부 |
| grafana | `/api/user`, `/api/access-control/user/permissions` | ok. 권한 28개 모두 read/list/get/query |
| argocd | `/api/v1/account/can-i/…` 13개 | ok. 앱 조회 yes, 쓰기·실행 12종 no |
| prometheus | `/api/v1/status/flags` | ok. admin·remote-write·OTLP 꺼짐 (lifecycle 은 켜져 있지만 MCP 에 부르는 도구 없음) |
| opensearch | `_cat/plugins` → `_plugins/_security/authinfo` | **warn**. `DISABLE_SECURITY_PLUGIN=true` 라 서버에 계정·권한이 없다 (Ingress basic auth 뿐). 쓰기 차단은 MCP 계층에만 의존 |

`fail` 경로는 모의로 확인: argocd 판정을 fail 로 바꾸자 등록이 해제되고 조회도 하지 않았다.

조회 (`koa/query.py`) — 카탈로그 `queries` 에 MCP 별 이름 붙인 조회를 둔다.

| MCP | 조회 |
|---|---|
| kubernetes | `nodes`, `pods ns=`, `events ns=`, `describe kind= name= ns=`, `logs name= ns= tail=` |
| prometheus | `up`, `down`, `firing`, `restarts range=`, `promql q=` |
| opensearch | `health`, `indices`, `search index= q= size=` |
| grafana | `datasources`, `health`, `dashboards q=`, `rules` |
| argocd | `apps`, `app name=`, `diagnose name=`, `history name=` |

19개 조회 모두 kind-lab 에서 실행해 확인. 이 과정에서 고친 것:
- grafana `list_alert_groups`·`get_alert_group` 은 Grafana OnCall 도구라 OnCall 이 없으면 404 → 허용 목록에서 뺐다 (22 → 20).
- opensearch `SearchIndexTool` 인자는 `query_dsl`. `*` 전체 검색은 `@timestamp` 없는 시스템 인덱스에서 샤드 실패 → `index=` 필수.
- `query.py` 는 Hermes 와 같은 `include` 를 지킨다: `--call grafana grafana_api_request` → 거부.

## 13. 다음 단계

1. candidate 를 하나씩 verified 로 올린다 (argocd 부터).
   읽기 전용 계정 발급 → `.env` → `--apply --with` → 도구 목록과 쓰기 거부 확인 → `status: verified`.
2. 프로필에 로그 스키마(인덱스 패턴, 주요 필드 이름)를 추가한다. opensearch MCP 로 매핑을 읽어 채운다.
3. 분석 런북 skill 이 "분석 전에 `clusters/<이름>.yaml` 을 먼저 읽는다" 를 따르게 한다.
4. 장애 재현 시나리오로 탐색·분석 성능을 측정한다.
