# KOA 동작 방식 — 사용자가 말한 범위만 분석하고, 지식을 키운다

> 0.2.0 (2026-10-11). 이전의 트리아지(클러스터 전체 훑기)·조사 요청·보고서 구조를 걷어 내고 이 문서로 대체했다.

## 0. 한 줄

사용자가 구성요소나 흐름을 말하고 분석을 부탁하면, KOA 는 **클러스터 지식에서 그 부분만 꺼내** 그 범위만 읽기 전용으로 확인한다.
분석하다 새로 알게 된 것(구성요소, 흐름, 별명, 로그 문구, 정상 패턴, 이번 이슈)은 **사용자가 OK 하면** 지식에 더한다.
쓸수록 지식이 커지고, 다음 분석은 더 짧아진다.

```
사용자: "rmq 소비가 안 돼"
   │
   ▼
① 지식에서 꺼내기   knowledge.py context  → rabbitmq + 그걸 지나는 흐름 + 흐름 위 구성요소 + 관련 과거 이슈 + 정상 패턴
   │
   ▼
② 그 범위만 확인     과거 이슈의 로그 문구 먼저 → 흐름 순서대로 hop 별 (파드·이벤트·로그·다음 hop 호출), 정상 패턴과 대조
   │
   ▼
③ 결론 + 제안        원인 지점, 근거 표, 못 본 것  +  "새로 알게 된 것" 표 → 사용자 OK → knowledge.py add / incident
   │
   ▼
④ 에이전트 성장      클러스터와 무관한 분석 요령 → 스킬 koa-learned,  사용자·환경 사실 → 메모리  (웹 "KOA 가 배운 것" 탭)
```

## 1. 무엇을 어디에 쌓나

| 무엇 | 어디 | 누가 쓰나 |
|---|---|---|
| 클러스터 사실: 구성요소 → 워크로드, 별명, 로그 문구, 흐름, 정상 패턴 | `local/knowledge/<클러스터>/flows.yaml` | 사용자 OK 후 KOA (`knowledge.py add`), 웹 화면 |
| 과거 이슈 1건 = 파일 1개 | `local/knowledge/<클러스터>/incidents/*.md` | 사용자 OK 후 KOA (`knowledge.py incident`), 웹 화면 |
| 아직 확인 안 된 제안 | `local/knowledge/<클러스터>/suggestions.yaml` | flowmap, KOA (`knowledge.py suggest`) → 웹에서 더하기/버리기 |
| 자유 메모 | `local/knowledge/<클러스터>/architecture.md` | 사용자 |
| 분석 요령 (어느 클러스터에나 통하는 것) | 스킬 `koa-learned` (프로필에만 있고 저장소에 없어서 업데이트가 덮지 않는다) | KOA (알려 주고 바로) |
| 사용자·환경에 대한 짧은 사실 | Hermes 메모리 | KOA |

클러스터 이름·흐름은 메모리나 스킬에 넣지 않는다. 메모리는 매 턴 들어가고 크기 제한이 있어서, 클러스터 지식은 파일에 두고 필요할 때 그 부분만 꺼낸다.
`local/` 은 프로필 업데이트에도 유지되고, 회사 내부 이름이 들어가므로 저장소에는 커밋하지 않는다.

## 2. `flows.yaml`

```yaml
schema: koa.knowledge/v1
cluster: prod-a

components:                    # 사람이 부르는 이름 → k8s 워크로드
  rabbitmq:
    workload: mq/StatefulSet/rabbitmq
    aliases: [rmq, 래빗, 큐]     # 질문 속 단어를 이걸로 찾는다
    log_signatures: ["disk resource limit alarm set"]
  order-worker:
    workload: shop/Deployment/order-worker
    aliases: [주문 워커]
  orders-db:
    external: true             # 클러스터 밖. 로그 문구로만 판정
    aliases: [주문 DB]
  api-gateway:
    workload: shop/Deployment/api-gateway
    flow_nodes: [Gateway]      # 흐름 노드 글자와 부분 문자열로 매칭 (여럿이 공유 가능)

flows:
  order-async:
    kind: queue
    hops: [order-api, rabbitmq, order-worker, orders-db]

normal:                        # 정상인데 이상해 보이는 것
  - {component: order-worker, note: "매일 02:00 정산 배치 때 큐가 10만 건까지 쌓였다 30분 안에 빠진다"}
```

과거 이슈 (`incidents/2026-08-14-rmq-disk-alarm.md`):

```markdown
---
date: 2026-08-14
components: [rabbitmq, order-worker]
signature:
  logs: ["disk resource limit alarm set"]
cause: "rabbitmq PVC 90% → 디스크 알람 → 생산자 차단"
found_by: "rabbitmq 파드 로그의 alarm 문구"
---
생산자 쪽에서 publish 가 멈춘 것처럼 보였지만 원인은 브로커 디스크였다. PVC 증설로 해결.
```

모르는 칸은 비워 둔다. 틀린 값보다 빈 값이 낫다. 템플릿: `koa/templates/knowledge/`.

## 3. `koa/knowledge.py`

```bash
python3 koa/knowledge.py list
python3 koa/knowledge.py context <c> "rmq 소비가 안 돼"        # ① 이 질문에 해당하는 지식만
python3 koa/knowledge.py context <c> --with rabbitmq,order-worker   # 사용자가 이름을 짚어 줬을 때
python3 koa/knowledge.py add <c> '<json>' [--dry-run]           # ③ OK 받은 것만 더한다
python3 koa/knowledge.py incident <c> '<json>'                  # 과거 이슈 1건
python3 koa/knowledge.py recur <c> <이슈파일> '{"date":…,"note":…}'  # 같은 원인 재발: 기존 이슈에 한 줄 (새 파일 안 만듦)
python3 koa/knowledge.py suggest <c> '<json>' --title … --why …  # 나중에 고를 제안
python3 koa/knowledge.py suggestions <c> | accept <c> <id> | dismiss <c> <id>
python3 koa/knowledge.py check <c>                              # 워크로드·흐름 매칭 검사
```

`context` 가 꺼내는 범위: 질문에서 찾은(또는 `--with`) 구성요소 → 그걸 지나는 흐름 → 그 흐름 위 구성요소 → 이들과 관련된 과거 이슈·정상 패턴. 더 넓히지 않는다.
못 찾으면 "어느 구성요소/흐름인지 사용자에게 묻는다" 고 알려 준다 — 클러스터 전체를 훑지 않는다. 사용자의 답은 새 별명으로 제안한다.

`add` 는 더하기만 한다: 같은 이름·같은 워크로드의 구성요소는 목록 칸(aliases, log_signatures, flow_nodes)을 합치고 빈 칸만 채운다. 기존 흐름과 같거나 그 일부인 흐름은 건너뛴다. 저장 전 파일은 `flows.yaml.bak`.

제안은 내용으로 중복을 거르고, 버린 제안은 기억해서 다시 내지 않는다. 이미 지식에 있는 내용이면 남기지 않는다.

## 4. 웹 화면 (`koa/web.py`)

지식을 표와 폼으로 보고 고치는 곳. LLM 을 부르지 않는다. 이 컴퓨터에서만 열린다 (127.0.0.1).

```bash
cd ~/.hermes/profiles/koa && /usr/bin/python3 koa/web.py     # → http://127.0.0.1:8765
```

| 탭 | 하는 일 |
|---|---|
| 클러스터 지식 | 맨 위에 **제안** 카드(무엇이 바뀌는지 미리 보기 + 더하기/버리기). 흐름(노드 칩, 순서 편집), 구성요소(별명·로그 문구·워크로드, "클러스터에서 고르기"는 읽기 전용 kubectl 로 워크로드 목록), 정상 패턴 |
| 과거 이슈 | 한 문단으로 쓰면 날짜·구성요소·로그 문구·원인을 뽑아 미리 보여 주고 저장. 저장한 이슈는 다음 `context` 에 바로 나온다 |
| KOA 가 배운 것 | 이 프로필(`koa`)의 Hermes 메모리(항목 지우기 가능)와, KOA 가 만들거나 고친 스킬(`koa-learned` + curator 기록·`created_by: agent` 인 것, 본문·변경 기록 보기). 꺼 둔 기본 스킬은 안 보인다. `koa/learned.py` |

POST 는 `X-KOA: 1` 헤더가 있어야 받는다 (다른 사이트에서 보낸 요청 막기). 클러스터 이름·이슈 파일 이름은 정규식으로 검사한다.

## 5. 흐름 찾기 (`koa/flowmap.py`) 와 제안

사용자가 "흐름 찾아줘" 할 때만 돌린다. 설정(Ingress·env·ConfigMap·NetworkPolicy)과 파드 로그만 읽는다 (get/list/logs).
지식에 아직 없는 흐름·구성요소는 **제안**으로 남기고 지식은 고치지 않는다. 사용자가 웹의 제안 카드나 채팅으로 고른 것만 더해진다.
방법과 판정: [koa-flowmap.md](koa-flowmap.md).

## 6. 0.2.0 에서 뺀 것과 이유

| 뺀 것 | 이유 |
|---|---|
| `triage.py` (클러스터 전체 이상 징후 훑기) | 사용자가 말한 범위만 본다. 전체를 먼저 훑으면 범위 밖 잡음이 분석을 끌고 간다 |
| `knowledge.py plan` (증상별 확인 순서 규칙) | `context` 로 범위만 주고, 확인 순서는 스킬 절차 + 모델 판단으로 |
| 웹의 조사 요청·결과·보고서 탭, `local/requests/`, `web/md.js` | 트리아지와 함께 의미가 없어짐. 분석과 결과는 채팅에서, 웹은 지식 편집만 |
| `knowledge.py merge` | `add` (OK 받은 것) 와 제안(`suggest`) 으로 대체 |

탐색·MCP 설치(`discover.py`, `plan.py`, `report.py` 의 탐색 보고서)는 그대로다 — 스킬 `koa-cluster-discovery`.
