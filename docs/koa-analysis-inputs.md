# KOA 분석 입력 설계 — 클러스터 지식 + 조사 요청

> 2026-10-06 · 설계안 · [rca-agent-design.md](rca-agent-design.md) 2절 ②③ 를 구체화 (그쪽 `context.yaml` 한 파일 안을 이 문서로 대체)

## 0. 요약

분석에 들어가는 정보는 두 종류다.

| | ① 클러스터 지식 | ② 조사 요청 |
|---|---|---|
| 예 | "우리 아키텍처는 이래", "HTTP 는 ingress → gateway → order-api 로 흘러", "예전에 rmq 디스크 알람으로 멈춘 적 있어" | "rmq 가 소비가 안 되는데 어디가 문제야?" |
| 수명 | 오래 간다. 쌓인다 | 이번 장애 한 번 |
| 누가 | 클러스터 관리자 (평소에) | 장애 대응자 (지금) |
| 저장 | `local/knowledge/<클러스터>/` | `local/clusters/<클러스터>.triage/` 의 조사 기록 |

KOA 는 ②를 받으면 ①에서 **관련된 흐름과 과거 이슈만 꺼내** "조사 계획"을 만든다. 조사 계획은 "흐름 위의 어느 지점(hop)을 어떤 순서로, 무엇으로 확인할지"다. 트리아지와 확인 조회는 그 경로에 맞춰 돌고, 결론은 **흐름 위에서 정상 → 비정상으로 바뀌는 지점 = 원인 지점**으로 보고한다.

```
② 조사 요청 ──┐
              ├─▶ [조사 계획] 대상 찾기 → 흐름 경로 → 과거 이슈 매칭 → 가설·확인 순서
① 클러스터 지식 ┘                 │
                                 ▼
                  [트리아지 --focus 경로]  k8s MCP 로 경로 위 워크로드만 상세히
                                 │
                                 ▼
                  [hop 별 확인]  증상 플레이북의 확인 항목 (k8s 기본, 가속기 있으면 먼저)
                                 │
                                 ▼
                  [원인 지점 보고]  생산자 ✅ → rabbitmq ⚠ → consumer ❌ → DB ✅
                                 │
                                 ▼
                  [기록]  사용자가 확인하면 ① 과거 이슈에 추가
```

## 1. ① 클러스터 지식

### 1.1 왜 자유 서술 + 구조 둘 다인가

관리자는 "구조는 이렇고 흐름은 이래" 를 **말로** 쓰는 게 편하다. 하지만 "rmq 소비 안 됨" 에서 "그 큐의 소비자가 누구고 소비자는 뭘 부르나" 를 매번 LLM 이 긴 글에서 다시 찾으면 느리고 틀린다. 그래서

- 사람은 자유롭게 쓴다 (`architecture.md`, 과거 이슈 메모).
- KOA 가 거기서 **흐름과 구성요소를 구조로 뽑아** `flows.yaml` 에 정리하고, 바뀐 줄을 보여 주고 확인받는다.
- 분석은 구조(`flows.yaml`)로 경로를 정하고, 자유 서술은 판단할 때 참고로 읽는다.

### 1.2 파일

```
local/knowledge/<클러스터>/
  architecture.md        자유 서술: 전체 구조, 팀, 운영 관행, 주의할 점
  flows.yaml             구조: 구성요소(→ k8s 워크로드) + 흐름(hop 순서) + 정상 패턴
  incidents/             과거 이슈 1건 = 파일 1개 (머리말 + 자유 서술)
    2026-08-14-rmq-disk-alarm.md
```

템플릿: [`koa/templates/knowledge/`](../koa/templates/knowledge/). 설치본 업데이트에도 `local/` 은 유지된다. 회사 내부 이름이 들어가므로 저장소에는 커밋하지 않는다.

### 1.3 `flows.yaml` — 분석이 실제로 쓰는 부분

```yaml
schema: koa.knowledge/v1
cluster: prod-a

components:                    # 사람이 부르는 이름 → k8s 워크로드. aliases 로 질문 속 단어를 찾는다
  rabbitmq:
    workload: mq/StatefulSet/rabbitmq
    aliases: [rmq, 래빗, 큐, mq]
    kind: message-queue
  order-api:
    workload: shop/Deployment/order-api
    aliases: [주문, 주문 API]
    tier: critical
  order-worker:
    workload: shop/Deployment/order-worker
    aliases: [주문 워커, 컨슈머]
  orders-db:
    external: true               # 클러스터 밖. k8s 로 상태를 못 보니 증상 문구로 판정
    aliases: [주문 DB, mysql]
    log_signatures: ["Communications link failure", "Too many connections", "Lock wait timeout"]

flows:
  http-order:                  # 사용자 요청 흐름
    kind: http
    hops: [ingress-nginx, gateway, order-api, orders-db]
  order-async:                 # 비동기 처리 흐름
    kind: queue
    hops:
      - {from: order-api, to: rabbitmq, via: "exchange orders / queue order.created", role: produce}
      - {from: rabbitmq, to: order-worker, via: "queue order.created", role: consume}
      - {from: order-worker, to: orders-db, role: call}
      - {from: order-worker, to: payment-api, role: call}

normal:                        # 정상인데 이상해 보이는 것 (오탐 줄이기)
  - {component: order-worker, note: "매일 02:00 정산 배치 때 큐가 10만 건까지 쌓였다 30분 안에 빠진다"}
```

### 1.4 과거 이슈 — `incidents/*.md`

```markdown
---
date: 2026-08-14
symptom: queue-backlog            # 증상 유형 (3절 표)
components: [rabbitmq, order-worker]
signature:                        # 다음에 이 흔적이 보이면 이 이슈를 1순위로
  logs: ["disk resource limit alarm set", "blocked"]
cause: "rabbitmq PVC 90% → 디스크 알람 → 생산자 차단"
found_by: "rabbitmq 파드 로그의 alarm 문구"
---
생산자 쪽에서 publish 가 멈춘 것처럼 보였지만 원인은 브로커 디스크였다. PVC 증설로 해결.
```

### 1.5 입력 방법

| 방법 | 동작 |
|---|---|
| 대화 | "우리 HTTP 흐름은 ingress → gateway → order-api 야" → KOA 가 `architecture.md` 에 원문을 붙이고 `flows.yaml` 에 반영할 줄을 보여 준 뒤 쓴다 |
| 과거 이슈 | "예전에 rmq 디스크 알람으로 멈춘 적 있어" → `incidents/` 에 새 파일. 모르는 칸(signature 등)은 비워 두고 다음 분석 때 채운다 |
| 분석 후 자동 제안 | 원인을 사용자가 확인하면 이슈 파일 초안을 만들어 확인받는다 |
| 직접 편집 | 파일을 고치면 다음 분석부터 반영 |

검증 (`koa/knowledge.py check`): `workload` 가 실제 클러스터에 없거나, flows 의 hop 이 components 에 없으면 알린다. 지식이 틀리면 경로가 틀리므로, 분석 보고서에도 "지식과 실제가 다른 곳" 을 적는다.

## 2. ② 조사 요청 → 조사 계획

사용자는 평소 말로 묻는다. KOA 는 그걸 아래 형태로 정리하고 **보고서 첫 줄에 보여 준다** (틀리면 사용자가 바로 고칠 수 있게). 묻지 않고 기본값으로 진행한다.

```yaml
request: "rmq 가 소비가 안 되는데 어디가 문제야?"
target: rabbitmq                 # aliases 로 찾음 (rmq)
symptom: queue-backlog           # 3절 표에서
since: 최근 1h (말이 없으면)
path:                            # flows 에서 target 을 지나는 흐름의 hop 들, 확인 순서대로
  - order-worker   (consume)     # 소비가 안 됨 → 소비자부터
  - rabbitmq       (broker)
  - orders-db, payment-api (소비자가 부르는 하류: 여기가 느리면 소비자가 멈춘다)
  - order-api      (produce)     # 들어오는 양이 갑자기 늘었나
known: [2026-08-14-rmq-disk-alarm]   # symptom·components 가 겹치는 과거 이슈
```

계획 만드는 규칙 (`koa/knowledge.py plan "<질문>"`):

1. **대상 찾기**: 질문 단어를 components 의 이름·aliases 와 맞춘다. 못 찾으면 트리아지 상위 이상 징후를 대상으로 쓴다.
2. **증상 유형**: 질문 문구로 3절 표의 유형을 고른다 ("소비가 안 됨/쌓임" → queue-backlog, "느려/타임아웃" → latency, "5xx/에러" → errors, "로그가 안 보여" → log-pipeline …).
3. **경로**: 대상을 지나는 flows 를 찾고, 증상 유형이 정한 방향으로 hop 을 정렬한다. queue-backlog 는 소비자 → 브로커 → 소비자의 하류 → 생산자, latency/errors 는 사용자에 가까운 쪽부터 하류로.
4. **과거 이슈**: symptom 이나 components 가 겹치는 이슈를 붙이고, signature 를 1순위 확인 항목으로 올린다.
5. **정상 패턴**: 해당 구성요소의 `normal` 을 붙여 오탐을 거른다 (예: 02:00 배치 시간대면 먼저 말한다).

## 3. 증상 플레이북 — hop 마다 무엇을 보나

플레이북은 증상 유형별로 "역할(role)마다 확인할 것" 을 적는다. 구성요소 이름이 아니라 역할로 적어서 어느 클러스터에서나 쓴다. 지금은 k8s MCP 만으로 되는 항목을 기본으로 둔다.

**queue-backlog (큐가 소비되지 않음)**

| 역할 | 확인 (k8s MCP 만) | 원인일 때 보이는 것 |
|---|---|---|
| consume (소비자) | 파드 수·Ready·재시작, 직전 롤아웃, 로그 `since` 구간 | 0개/CrashLoop, 연결 오류(`ACCESS_REFUSED`, `connection reset`, `missed heartbeats`), 처리 예외 반복, 로그가 아예 멈춤(처리 중 멈춤) |
| broker | 파드 Ready·재시작, 로그 | `memory/disk resource limit alarm`, `blocked`, `closing AMQP connection`, 쿼럼 큐 리더 선출 실패, PVC 이벤트 |
| 소비자의 하류 (call) | 하류 워크로드 상태, 외부면 소비자 로그에서 `log_signatures` | 소비자 로그에 DB 타임아웃 → 소비자가 메시지를 붙잡고 멈춤 |
| produce (생산자) | 최근 배포, 로그의 publish 오류·급증 | 배포 후 메시지 형식 변경 → 소비자 역직렬화 실패, 유입 급증 |
| 공통 | 같은 시각 노드 사건, 직전 변경 (트리아지) | |

못 보는 것 (보고서에 적는다): 큐 길이·소비자 수·unacked 수는 RabbitMQ 관리 API 가 필요하다. 클러스터 밖 주소가 없고 읽기 전용 계정은 `services/proxy`·`exec` 가 막혀 있어 k8s 로는 볼 수 없다. 그래서 "쌓였는지" 는 사용자의 말과 로그로 판단하고, 그 사실을 밝힌다.

그 외 유형: `latency`, `errors`(5xx), `crash`, `pending`, `log-pipeline`(기존 스킬 4단계), `external-dependency`. 형식은 같다 (역할 × 확인 × 보이는 것).

## 4. 결과 — 원인 지점 보고

```
요청: rmq 가 소비가 안 됨 → 대상 rabbitmq, 유형 queue-backlog, 최근 1h (지식: order-async 흐름)

흐름 위 상태
  order-api ✅ ──publish──▶ rabbitmq ✅ ──consume──▶ order-worker ❌ ──▶ orders-db ⚠
                                                     CrashLoop 3/3      소비자 로그에 "Too many connections" 120건
원인 지점: order-worker → orders-db 구간
  근거 | order-worker 재시작 14건 exit 1, 직전 로그 "Too many connections" (kubectl_logs previous)
       | 07:56 payment-api 배포 (커넥션 풀 32→64) — 같은 DB 를 씀 (flows)
  과거 이슈: 일치 없음 (2026-08-14 디스크 알람은 broker 로그에 alarm 문구가 없어 배제)
확인 못 한 것: 큐 길이 (관리 API 없음), orders-db 자체 상태 (클러스터 밖)
```

hop 하나하나에 ✅/⚠/❌/? 를 붙이고, 그 판정 근거를 표로 남긴다. **원인 지점은 "정상 → 비정상으로 바뀌는 첫 구간"**, 그 하류만 비정상이면 하류를, 그 지점 자체가 비정상이면 그 지점을 지목한다.

## 5. 프롬프트에 무엇을 넣나 (컨텍스트 아끼기)

지식 전체를 매번 넣지 않는다. 조사 계획이 고른 것만 넣는다.

| 넣는 것 | 크기 |
|---|---|
| 조사 계획 (위 yaml) | 20줄 |
| 경로 위 구성요소의 flows·normal | 10~30줄 |
| 매칭된 과거 이슈 머리말 + 본문 첫 단락 | 건당 5줄 |
| `architecture.md` | 대상 구성요소 이름이 나오는 단락만 |
| 트리아지 (`--focus` 경로) | 40줄 |

## 6. 구현 순서

| # | 내용 | 확인 |
|---|---|---|
| 1 | `koa/templates/knowledge/` 템플릿 (이번 커밋) | – |
| 2 | `koa/knowledge.py`: load / check / plan "<질문>" (aliases·symptom 매칭, 경로 정렬, 과거 이슈 매칭) | 예시 지식으로 질문 5개 → 계획이 맞는지 |
| 3 | `triage.py --focus <대상>`: 계획의 경로 워크로드만 상세, 경로 순서로 표시, hop 판정(✅/⚠/❌) | API 서버 lab 에 rmq 장면 (소비자 CrashLoop, 브로커 정상) |
| 4 | 증상 플레이북 `koa/playbooks.yaml` (queue-backlog 부터) + 로그 문구 검사 | 같은 장면에서 원인 지점이 소비자 → DB 로 나오는지 |
| 5 | 스킬: `koa-cluster-knowledge` (대화로 지식 입력) + `cluster-incident-analysis` 를 계획 → 트리아지 → hop 확인 → 원인 지점 보고로 | 새 채팅에서 "rmq 소비 안 돼" 1회 |
| 6 | 분석 후 과거 이슈 초안 → 확인 → `incidents/` | 같은 장면 두 번째에 1순위로 나오는지 |

## 7. 정할 것 (따로 말이 없으면 기본값으로 간다)

| 항목 | 기본값 |
|---|---|
| 지식 위치 | 클러스터별 `local/knowledge/<클러스터>/` (커밋 안 함) |
| 대화로 넣은 지식 | 바뀔 줄을 보여 주고 쓴다 (묻고 쓰기) |
| 조사 계획 | 묻지 않고 바로 진행, 보고서 첫 줄에 해석을 보여 줌 |
| 큐 길이처럼 k8s 로 못 보는 것 | 추정하지 않고 "확인 못 함" 으로 적는다 |
