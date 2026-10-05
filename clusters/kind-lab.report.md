# KOA 탐색 결과 — kind-lab

> 2026-10-05T02:33:03Z · 계정 `system:serviceaccount:hermes:hermes-readonly` · 읽기 전용 ✅

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
| rabbitmq | candidate | rabbitmq 감지 | 미등록 | 보류 | ~/.hermes/.env 에 값 채우기: RABBITMQ_MANAGEMENT_ENDPOINT; 클러스터 밖 접근 주소가 없다 → Ingress/LB 로 열거나 RABBITMQ_MANAGEMENT_ENDPOINT 를 직접 지정 |
| fluentd | - | fluentd 감지 | - | 붙이지 않음 | 전용 MCP 없음. prometheus 플러그인 지표 → Prometheus 로 보는 것이 정석 |
| redis | - | redis 감지 | - | 붙이지 않음 | 카탈로그에 없음 (장애 분석용으로는 지표가 우선) |

## 3. 빈 곳과 제안

| # | 우선 | 빈 곳 | 분석에 미치는 영향 | 누가 |
|---|---|---|---|---|
| 1 | 🔴 높음 | fluentd 자체 상태(버퍼·재시도·처리량)를 보는 신호 없음 | 로그가 안 보일 때 트래픽이 없는 건지 수집이 끊긴 건지 구분할 수 없다 (kind-lab 에서 실제로 겪음) | 클러스터 운영팀 |
| 2 | 🔴 높음 | k8s 이벤트 보존 1h (kube-apiserver 기본값 (--event-ttl 없음)) | 장애 뒤 분석을 시작하면 재시작·OOM·스케줄링 실패 이력이 이미 사라져 있다 | 클러스터 운영팀 |
| 3 | 🟡 중간 | 클러스터 밖 접근 주소 없음: alertmanager, rabbitmq | 클러스터 안에는 있지만 KOA(MCP)가 닿지 못해 그 데이터를 분석에 쓸 수 없다. 읽기 전용 SA 는 port-forward 도 막혀 있다 | 클러스터 운영팀 |
| 4 | 🟡 중간 | Prometheus 가 수집하지 않는 구성요소: argocd, fluentd, ingress-nginx, opensearch, rabbitmq, redis | 이 구성요소들은 문제가 생겨도 로그로만 보인다 — 큐 적체, 버퍼 증가처럼 서서히 나빠지는 장애를 놓친다 | 클러스터 운영팀 |
| 5 | ⚪ 낮음 | metrics API(metrics-server) 없음 | kubectl top 과 HPA 를 쓸 수 없다. Prometheus 가 있으면 CPU·메모리는 cAdvisor 지표로 대신 본다 | 클러스터 운영팀 |
| 6 | ⚪ 낮음 | 트레이스 없음 | 서비스 간 어느 구간에서 느려지는지 로그의 request_id 를 이어 붙여 추정한다 | 클러스터 운영팀 |

### 1. 로그 수집기 자체 상태 감시

- 수집기 지표(버퍼 길이, 재시도, 출력 오류, 처리량)를 Prometheus 로 수집 — 구성요소별 방법은 아래 '수집하지 않는 구성요소' 참고
- 노드마다 1분에 한 번 heartbeat 레코드를 로그 저장소로 보내 마지막 수신 시각을 바로 볼 수 있게 한다
- 수신이 N분 끊기면 울리는 경보

### 2. k8s 이벤트 장기 보관

- kubernetes-event-exporter 로 이벤트를 로그 저장소(OpenSearch 등)에 보낸다 — --event-ttl 을 늘리는 것보다 etcd 부담이 없다
- 그 전까지 KOA 는 분석 첫 단계에서 이벤트부터 읽는다 (1시간 안에 사라짐)

### 3. 관측 시스템 접근 경로

- Ingress 로 열되 반드시 인증(Basic Auth/OIDC)과 읽기 전용 계정을 붙인다. 또는 MCP 서버를 클러스터 안에서 띄운다
- `alertmanager` — 급하지 않다 — 지금 울리는 경보는 Prometheus 의 ALERTS 지표·/api/v1/alerts 로 볼 수 있다. silence·이력까지 보려면 Ingress(routePrefix /alertmanager)로 연다 (서비스: alertmanager-operated (http-web:9093, tcp-mesh:9094, udp-mesh:9094), monitoring-kube-prometheus-alertmanager (http-web:9093, reloader-web:8080))
- `rabbitmq` — 관리 API(15672)를 Ingress + 인증으로 열고 monitoring 태그 전용 사용자를 만든다. 큐 상태만 필요하면 15692 지표를 Prometheus 로 보는 것으로 충분하다 (서비스: rabbitmq (amqp:5672, management:15672))

### 4. 지표를 수집하지 않는 구성요소

- `argocd` — argocd-metrics(8082)·argocd-server-metrics(8083)·argocd-repo-server(8084) 서비스가 이미 있다 → ServiceMonitor 만 추가 (Helm 이면 *.metrics.serviceMonitor.enabled=true) — 동기화 실패·앱 헬스
- `fluentd` — fluent-plugin-prometheus(이미지에 보통 포함)로 <source> @type prometheus(24231) + prometheus_monitor + prometheus_output_monitor 를 켜고 ServiceMonitor 추가 — 버퍼 길이·재시도·출력 오류
- `ingress-nginx` — Helm controller.metrics.enabled=true + controller.metrics.serviceMonitor.enabled=true (10254) — 경로별 요청 수·5xx·지연. 장애 영향 범위를 가장 먼저 보여주는 지표
- `opensearch` — prometheus-community/elasticsearch-exporter(OpenSearch 호환) 를 띄우고 ServiceMonitor 로 수집 — 클러스터 상태·디스크·쓰기 거절(rejected) 지표
- `rabbitmq` — rabbitmq_prometheus 플러그인(15692)을 서비스 포트로 열고 ServiceMonitor 추가 — 큐 길이·소비자 수·unacked. 큐별 값은 /metrics/per-object 또는 /metrics/detailed. 공식 이미지는 플러그인이 기본으로 켜져 있는지 확인 필요
- `redis` — oliver006/redis_exporter(9121) 사이드카 + ServiceMonitor — 메모리·연결 수·eviction

### 5. metrics-server 설치

- metrics-server 설치 (HPA 를 쓰려면 필수)

### 6. 분산 트레이스

- OpenTelemetry SDK + collector + Tempo/Jaeger. ingress 에서 request_id 를 이미 넘기고 있다면 trace_id 로 확장

## 4. 다음 단계

1. **KOA 가 바로 할 수 있는 것** — 접근 주소가 이미 있는 MCP 를 붙여 verified 로 올린다: `prometheus`, `grafana`, `argocd`
2. **클러스터 운영팀에 요청할 것 (우선 높음)** — 로그 수집기 자체 상태 감시 / k8s 이벤트 장기 보관
3. 변경 후 `python3 koa/discover.py --probe` 로 다시 탐색해 이 보고서가 바뀌는지 확인한다.
