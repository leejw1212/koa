# KOA 탐색 결과 — kind-lab

> 2026-10-05T02:38:03Z · 계정 `system:serviceaccount:hermes:hermes-readonly` · 읽기 전용 ✅

| 항목 | 값 |
|---|---|
| Kubernetes | v1.37.0 (kind, containerd) |
| 노드 | 3/3 Ready (control-plane 1, worker 2) |
| 네임스페이스 / 파드 | 11 / 65 |
| 막힌 권한 | get nodes.metrics.k8s.io, list secrets, create pods/exec, create pods/portforward, get services/proxy, create pods, delete pods, patch deployments.apps |
| metrics API | 없음 |
| 이벤트 보존 | 1h (kube-apiserver 기본값 (--event-ttl 없음)) |
| Prometheus 수집 대상 | 22개 중 22개 up (http://localhost/prometheus) |

## 1. 찾은 구성요소

| 구성요소 | 종류 | 위치 | Ready | 클러스터 밖 주소 (probe) | 지표 수집 |
|---|---|---|---|---|---|
| prometheus | 지표 | monitoring/prometheus-monitoring-kube-prometheus-prometheus | 1/1 | http://localhost/prometheus (200) | ✅ |
| kube-state-metrics | 지표 수집기 | monitoring/monitoring-kube-state-metrics | 1/1 | — | ✅ |
| node-exporter | 지표 수집기 | monitoring/monitoring-prometheus-node-exporter | 3/3 | — | ✅ |
| alertmanager | 경보 | monitoring/alertmanager-monitoring-kube-prometheus-alertmanager | 1/1 | — | ✅ |
| grafana | 대시보드 | monitoring/monitoring-grafana | 1/1 | http://localhost/grafana (200) | ✅ |
| opensearch | 로그 저장소 | logging/opensearch | 1/1 | http://localhost/opensearch (401 🔒) | ❌ |
| fluentd | 로그 수집기 | logging/fluentd-aggregator, logging/fluentd-forwarder | 4/4 | — | ❌ |
| argocd | GitOps | argocd/argocd-application-controller, argocd/argocd-applicationset-controller, argocd/argocd-notifications-controller, argocd/argocd-repo-server, argocd/argocd-server | 5/5 | http://localhost/argocd (200) | ❌ |
| rabbitmq | 메시지 큐 | linkcard/rabbitmq | 1/1 | — | ❌ |
| redis | 캐시 | argocd/argocd-redis, linkcard/redis | 2/2 | — | ❌ |
| ingress-nginx | Ingress | ingress-nginx/ingress-nginx-controller | 1/1 | — | ❌ |

🔒 = 인증 필요 · 지표 수집 = Prometheus 가 이 구성요소를 수집하는지 (`?` = 확인 못 함)

## 2. 붙일 수 있는 MCP

| MCP | 검증 | 근거 | 현재 | 판단 | 필요한 것 |
|---|---|---|---|---|---|
| kubernetes | verified | k8s 조회 권한 있음 | 등록됨 (동일) | 유지 | - |
| opensearch | verified | opensearch 감지 | 등록됨 (동일) | 유지 | - |
| prometheus | candidate | prometheus 감지 | 미등록 | 보류 | ~/.hermes/.env 에 값 채우기: PROMETHEUS_URL; PROMETHEUS_URL 제안값: http://localhost/prometheus |
| grafana | candidate | grafana 감지 | 미등록 | 보류 | ~/.hermes/.env 에 값 채우기: GRAFANA_URL, GRAFANA_SERVICE_ACCOUNT_TOKEN; GRAFANA_URL 제안값: http://localhost/grafana |
| argocd | candidate | argocd 감지 | 미등록 | 보류 | ~/.hermes/.env 에 값 채우기: ARGOCD_BASE_URL, ARGOCD_API_TOKEN; ARGOCD_BASE_URL 제안값: http://localhost/argocd |
| rabbitmq | candidate | rabbitmq 감지 | 미등록 | 보류 | ~/.hermes/.env 에 값 채우기: RABBITMQ_MANAGEMENT_ENDPOINT; 클러스터 밖 접근 주소가 없다 → 지금 범위에서는 붙일 수 없다 (주소를 따로 알면 RABBITMQ_MANAGEMENT_ENDPOINT 에 직접 지정) |
| fluentd | - | fluentd 감지 | - | 붙이지 않음 | 전용 MCP 없음 → kubernetes MCP 로 파드 로그를 본다 |
| redis | - | redis 감지 | - | 붙이지 않음 | 카탈로그에 없음 → kubernetes MCP 로 파드 로그를 본다 |

## 3. 분석 한계와 KOA 대응

KOA 는 클러스터 설정을 바꾸지 않는다. 아래는 이 클러스터에서 분석할 때 감안할 한계와, 지금 쓸 수 있는 도구로 메우는 방법이다.

| # | 영향 | 한계 | 분석에 미치는 영향 |
|---|---|---|---|
| 1 | 🔴 큼 | fluentd 자체 상태(버퍼·재시도·처리량)를 보는 신호 없음 | 로그가 안 보일 때 트래픽이 없는 건지 수집이 끊긴 건지 바로 판정할 수 없다 (kind-lab 에서 실제로 겪음) |
| 2 | 🔴 큼 | k8s 이벤트 보존 1h (kube-apiserver 기본값 (--event-ttl 없음)) | 보존 기간이 지나면 재시작·OOM·스케줄링 실패 이력이 사라진다 |
| 3 | 🟡 중간 | 클러스터 밖 접근 주소 없음: alertmanager, rabbitmq | 클러스터 안에는 있지만 클러스터 밖 접근 주소가 없어 MCP 로 조회할 수 없다 (읽기 전용 계정은 port-forward 도 막혀 있다) |
| 4 | 🟡 중간 | Prometheus 가 수집하지 않는 구성요소: argocd, fluentd, ingress-nginx, opensearch, rabbitmq, redis | 이 구성요소들은 지표 없이 로그와 k8s 상태로만 본다. 큐 적체·버퍼 증가처럼 서서히 나빠지는 문제는 늦게 보인다 |
| 5 | ⚪ 작음 | metrics API(metrics-server) 없음 | 지금 CPU·메모리 사용량(kubectl top)을 볼 수 없다 |
| 6 | ⚪ 작음 | 트레이스 없음 | 서비스 간 어느 구간에서 느려지는지 직접 볼 수 없다 |

### 1. 로그 수집기 상태를 볼 지표 없음

**KOA 대응**
- 수집기 파드 로그(kubectl_logs)에서 flush 실패·retry·연결 오류를 본다
- 원본 파드 로그와 로그 저장소의 마지막 문서 시각을 비교한다. 원본에는 있는데 저장소에 없으면 수집 중단이다
- 수집기 설정(ConfigMap)에서 일부러 버리는 로그(필터)를 확인한다

### 2. k8s 이벤트 보존 짧음

**KOA 대응**
- 분석 첫 단계에서 이벤트부터 읽는다
- 사라진 이력은 파드 status(lastState.terminated 의 reason·exitCode·시각, restartCount)로 복원한다. 마지막 1회만 남는다

### 3. KOA 가 닿지 못하는 관측 시스템

**KOA 대응**
- 주소를 따로 알고 있으면 .env 에 직접 지정해 MCP 를 붙인다
- `alertmanager` — 지금 울리는 경보는 prometheus MCP 로 ALERTS 지표를 조회해 본다
- `rabbitmq` — 파드 로그(메모리·디스크 경보, 연결 끊김)와 소비자 앱 로그(재연결·처리 지연)로 본다. 큐 길이·소비자 수는 지금 범위에서 볼 수 없다

### 4. Prometheus 가 수집하지 않는 구성요소

**KOA 대응**
- `argocd` — argocd MCP 를 붙이면 동기화·헬스 상태를 본다. 읽기 전용 SA 는 Application 리소스 조회 권한이 없어 kubernetes MCP 로는 안 보인다
- `fluentd` — 파드 로그(kubectl_logs)에서 flush 실패·retry·연결 오류를 보고, 원본 파드 로그와 로그 저장소 마지막 문서 시각을 비교한다
- `ingress-nginx` — 접근 로그가 로그 저장소에 있으면 경로별 status·request_time 을 집계한다. 없으면 컨트롤러 파드 로그(kubectl_logs)
- `opensearch` — opensearch MCP 의 ClusterHealthTool·GetShardsTool 로 클러스터 상태·샤드를 직접 본다. 쓰기 거절·디스크 경고는 파드 로그(kubectl_logs)
- `rabbitmq` — 파드 로그(메모리·디스크 경보, 연결 끊김)와 소비자 앱 로그(재연결·처리 지연)로 본다. 큐 길이·소비자 수는 지금 범위에서 볼 수 없다
- `redis` — 파드 로그와 앱 로그의 연결 오류·타임아웃으로 본다

### 5. metrics API 없음

**KOA 대응**
- Prometheus 가 있으면 cAdvisor 지표(container_cpu_usage_seconds_total, container_memory_working_set_bytes)로 본다
- 없으면 OOMKilled 기록과 requests·limits 설정으로 추정한다

### 6. 트레이스 없음

**KOA 대응**
- 로그의 request_id 로 ingress → 앱 → 워커 로그를 이어 붙여 구간별 시간을 계산한다 (필드가 있을 때)

## 4. 다음 단계 (MCP)

1. `prometheus` — 접속 정보(PROMETHEUS_URL)를 받아 `~/.hermes/.env` 에 넣으면 붙일 수 있다 (PROMETHEUS_URL 제안값: http://localhost/prometheus). 근거: PromQL 은 쓰기를 표현할 수 없어 도구 자체가 읽기 전용
2. `grafana` — 접속 정보(GRAFANA_URL, GRAFANA_SERVICE_ACCOUNT_TOKEN)를 받아 `~/.hermes/.env` 에 넣으면 붙일 수 있다 (GRAFANA_URL 제안값: http://localhost/grafana). 근거: --disable-write 로 쓰기 도구 미등록
3. `argocd` — 접속 정보(ARGOCD_BASE_URL, ARGOCD_API_TOKEN)를 받아 `~/.hermes/.env` 에 넣으면 붙일 수 있다 (ARGOCD_BASE_URL 제안값: http://localhost/argocd). 근거: MCP_READ_ONLY + 조회 도구만
4. `rabbitmq` — 클러스터 밖 접근 주소가 없어 지금 범위에서는 붙일 수 없다. 3절의 대응 방법으로 본다
5. 접속 정보를 넣은 뒤 `python3 koa/plan.py kind-lab --apply --with <이름>` → 앱 재시작 → 도구 목록과 쓰기 거부를 확인하고 verified 로 올린다
