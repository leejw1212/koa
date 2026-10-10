# KOA 흐름 찾기 (`koa/flowmap.py`)

"요청이 지나는 길"을 **설정과 로그만** 읽어서 찾는다. 어떤 클러스터든 같은 방법으로 돈다.
결과는 **후보**다. 사람이 보고 고른 것만 클러스터 지식(`local/knowledge/<클러스터>/flows.yaml`)에 더한다.

## 원칙: 클러스터에 하는 것

| 한다 | 하지 않는다 |
|---|---|
| 목록·객체 조회 (get/list): 파드, Service, Ingress, HTTPRoute/GRPCRoute, IngressClass, NetworkPolicy, Namespace, CronJob, 워크로드가 참조하는 ConfigMap | 앱·Service·Ingress 에 요청 (curl, 헬스 GET, 시험 요청) |
| 파드 로그 읽기 (`kubectl logs` / kubernetes MCP `kubectl_logs`) | 포트 연결 시험, DNS 조회, exec, port-forward, services/proxy |
| | Secret 읽기 (env 가 Secret 에서 온다는 사실만 본다) |
| | 클러스터·지식 파일 변경 (후보는 따로 쓴다) |

출력 첫 줄에 클러스터에 보낸 요청 수(목록·객체·로그)가 그대로 찍힌다.

KOA 원칙상 **통신 확인(헬스·상태 경로 GET 같은 도달 확인)은 허용**된다. `discover.py --probe` 와 MCP probe 조회가 그 일을 한다.
flowmap 은 그것도 하지 않고 설정·로그만으로 판단한다 — 접속이 막힌 환경에서도 같은 결과가 나오게. 앱이 일을 하게 만드는 요청
(데이터 생성, 표식 요청, 부하)은 여전히 사용자에게 묻는다.

접속 정보는 다른 KOA 도구와 같다.
- 기본: 이 프로필에 등록된 kubernetes MCP. MCP 가 쓰는 kubeconfig 는 `config.yaml` 에 적힌 경로다.
- `--source kubectl`: `KOA_KUBECONFIG` 환경변수 또는 `--kubeconfig` 로 준 읽기 전용 kubeconfig. 기본값은 `~/.kube/hermes-readonly.yaml` 이고 `~/.kube/config` 는 거부한다.

코드에 클러스터 고유 이름은 없다. 구성요소 종류(큐·DB·캐시·로그 수집기)는 다음 순서로 정한다.
1. 탐색 프로필(`discover.py` 결과)
2. 카탈로그 이미지 규칙(`koa/catalog.yaml`)
3. 주소의 프로토콜·잘 알려진 포트

## 근거와 판정

| 근거 | 어디서 | 무엇을 |
|---|---|---|
| 입구 (`entry`) | Ingress, HTTPRoute, GRPCRoute, LoadBalancer/NodePort Service | 주소·경로 → Service → selector 로 고른 워크로드. 입구 컨트롤러는 다음 순서로 찾는다: Ingress status 의 LB 주소 → IngressClass controller 이름 → 탐색 프로필 |
| 설정 (`config`) | 컨테이너 env, `configMapKeyRef`, `envFrom`, command/args, ConfigMap 볼륨 파일, Ingress 어노테이션, initContainer 의 대기 명령 | 적힌 주소를 Service 로 해석한다 (아래 규칙) |
| 허용 규칙 (`netpol`) | NetworkPolicy ingress 의 podSelector | 허용됐다는 것만 알 수 있다. 실제로 쓰는지는 모르므로 이것만으로는 흐름에 넣지 않는다 |
| 로그 주소 (`log-name`) | 워크로드 로그에 적힌 Service 주소 | 그 워크로드가 그 주소로 연결했다는 근거 (방향 있음) |
| 로그 IP (`log-ip`) | 워크로드 로그에 나온 다른 워크로드의 파드 IP·ClusterIP | 둘이 통신했다는 근거. 방향은 모른다 → 설정이나 허용 규칙의 방향을 확인하는 데만 쓴다 |
| 요청 ID (`log-id`) | 서로 다른 ID 2개 이상이 두 워크로드 로그에 함께 나옴 (`request_id=`, UUID, traceparent) | 먼저 찍힌 쪽 → 나중 쪽. 사이에 큐 같은 다른 곳이 있을 수 있어서 흐름의 순서를 확인하는 데만 쓴다 |

판정은 연결마다 하나다: **설정+로그 > 설정 > 로그 > 허용규칙+로그 > 요청ID > 허용규칙**.

흐름은 `설정+로그`, `설정`, `로그`, `허용규칙+로그` 연결로만 만든다. 다음 세 가지는 표에만 보이고 흐름에는 들어가지 않는다.
- 허용 규칙만 있는 연결
- 방향을 모르는 IP 근거
- 로그에 나온 클러스터 밖 주소: 배너나 문서 링크가 섞이기 때문

### 주소 해석 규칙

다음 표기를 Service 로 해석한다.

| 표기 | 해석 |
|---|---|
| `name` | 같은 네임스페이스의 Service. 다른 네임스페이스의 짧은 이름은 맞추지 않는다 |
| `name.ns` | 그 네임스페이스의 Service |
| `name.ns.svc[.cluster.local]` | 그 네임스페이스의 Service |
| `pod-0.headless` | 헤드리스 Service (StatefulSet 파드 주소) |
| `pod-0.headless.ns` | 헤드리스 Service (StatefulSet 파드 주소) |
| ClusterIP | 그 IP 를 가진 Service |
| 파드 IP | 지금 떠 있는 그 파드의 워크로드 |

주소를 찾는 방법:
- `scheme://` URL 과 `host:port` 는 어디에 있든 찾는다.
- 포트 없는 낱말은 키 이름이 주소를 뜻할 때만 주소로 본다. 예: `*_HOST`, `*_URL`, `bootstrap.servers`, `proxy_pass`, `endpoint`.

찾은 주소의 처리:
- `ExternalName` Service 는 클러스터 밖 대상으로 본다.
- `*.svc` 형식인데 그런 Service 가 없으면 "적혀 있는데 없음" 표에 올린다. 오타나 지워진 Service 를 찾는 근거다.

다음 키는 주소로 보지 않는다. 문서 링크이거나 자기 자신의 주소이기 때문이다.
- `runbook_url`, `homepage`, `*external-url`, `root_url`, `advertised.listeners`, `redirect_uri`
- 주소 키가 아닌 곳에 적힌, 포트 없는 외부 URL

### 큐의 방향

브로커와의 연결은 프로토콜(amqp, kafka 등)이나 대상 종류(message-queue)로 알아본다. 넣는 쪽과 꺼내는 쪽은 설정만으로는 구분되지 않아서 다음 규칙으로 **추정**한다.
- 입구에서 요청을 받거나 다른 워크로드의 호출을 받는 워크로드 → 넣는 쪽
- 그 밖의 워크로드 → 꺼내는 쪽

출력에도 "추정"이라고 적는다.

### 로그에서 빼는 것

다음 워크로드의 로그에는 남의 파드 IP 나 요청 ID 가 들어 있어서, IP·ID 근거로 쓰지 않는다.
- 로그 수집기와 관측 도구 (종류는 탐색 프로필이나 카탈로그로 정한다)
- `hostNetwork` 파드 (CNI, kube-proxy, CSI 같은 노드 에이전트)
- 남의 파드 IP 가 5개가 넘는 워크로드에서 나온, 방향을 모르는 IP 근거. 목록에 늘어놓지 않고 "확인 못 한 것"에 한 줄로 접는다.

kubelet 헬스 체크(`kube-probe/`) 줄도 뺀다.

## 쓰는 법

```bash
python3 koa/flowmap.py                          # 전체 (kubernetes MCP)
python3 koa/flowmap.py --ns shop,payment        # 이 네임스페이스에서 출발하는 것만
python3 koa/flowmap.py --no-logs                # 설정만 (로그 읽기도 줄이고 싶을 때)
python3 koa/flowmap.py --source kubectl         # MCP 등록 전
python3 koa/flowmap.py --shards cand.json       # 후보를 지식 파편 형식으로

python3 koa/knowledge.py merge <클러스터> cand.json            # 무엇이 더해질지 미리 보기
python3 koa/knowledge.py merge <클러스터> cand.json --pick 1,3 --apply   # 고른 흐름만 더한다
```

`merge` 가 하는 일:
- 사람이 쓴 구성요소·흐름·정상 패턴은 지우거나 고치지 않는다.
- 같은 workload 의 구성요소는 기존 이름으로 바꿔 쓴다.
- 기존 흐름과 같거나 그 일부인 후보는 건너뛴다.
- 저장하기 전 파일은 `flows.yaml.bak` 으로 남긴다.

결과 JSON 은 `<결과 폴더>/<클러스터>.flowmap/<시각>.json` 에 남는다. 연결마다 근거 목록(where, value, 줄 수, 처음·마지막 시각)이 들어 있다.

비용: 로그를 읽는 컨테이너 수가 기본값으로 정해져 있다.
- 워크로드마다 파드 2개, 컨테이너마다 마지막 2000줄
- 컨테이너는 최대 150개. 이미 흐름에 들어간 워크로드부터 읽는다.

kind-lab(워크로드 37개)에서는 MCP 로 약 11초 걸렸다.

## 이 방법으로 알 수 없는 것 (출력의 "확인 못 한 것")

- **Secret 에 든 주소.** env 이름은 보이지만 대상은 모른다. 로그에 그 Service 주소나 IP 가 찍혔을 때만 이어진다.
- **요청 내용으로 정해지는 대상.** 사용자가 넘긴 URL 같은 것이다. 로그에 남았을 때만 "클러스터 밖 주소" 표에 보인다.
- **큐의 넣기·꺼내기 방향.** 위의 추정 규칙을 쓴다.
- **오래된 통신.** 로그는 마지막 N줄만 읽고, 재시작 전 로그와 로그 저장소(OpenSearch·Loki)는 보지 않는다. 오래된 통신을 확인하려면 로그 저장소 MCP 로 따로 찾는다 (`cluster-incident-analysis`).
- **설정을 못 보는 경우.** 파드가 없는 워크로드(0 replica, 끝난 Job)와, 사이드카가 라벨로 찾아 읽는 ConfigMap.
- **해석하지 않은 NetworkPolicy.** `matchExpressions` 를 쓰거나 범위가 너무 넓은 규칙.
- **입구 컨트롤러 로그의 요청 주체.** 사람과 도구(KOA 의 MCP 조회 포함)가 보낸 요청이 섞여 있다.

## 검사

```bash
python3 koa/tests/test_flowmap.py           # 주소 해석·가리기·판정·흐름 조립 (합성 데이터, 클러스터 없음)
python3 koa/tests/test_knowledge_merge.py   # 후보 합치기 (임시 폴더)
```
