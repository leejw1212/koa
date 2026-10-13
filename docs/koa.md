# KOA 동작 방식 — 사용자가 말한 범위만 분석하고, 지식을 키운다

> 0.2.0 (2026-10-11). 이전의 트리아지(클러스터 전체 훑기)·조사 요청·보고서 구조를 걷어 내고 이 문서로 대체했다.

## 0. 한 줄

사용자가 구성요소나 흐름을 말하고 분석을 부탁하면, KOA 는 **클러스터 지식에서 그 부분만 꺼내** 그 범위만 읽기 전용으로 확인한다.
분석하다 새로 알게 된 것(구성요소, 흐름, 별명, 로그 문구, 노트, 이번 이슈)은 **사용자가 OK 하면** 지식에 더한다.
쓸수록 지식이 커지고, 다음 분석은 더 짧아진다.

```
사용자: "rmq 소비가 안 돼"
   │
   ▼
① 지식에서 꺼내기   knowledge.py context  → rabbitmq + 그걸 지나는 흐름 + 흐름 위 구성요소 + 관련 과거 이슈
                     + 그 구성요소들의 노트 + 클러스터 전체 주제 노트 (종류와 읽는 법 포함)
   │
   ▼
② 그 범위만 확인     시간은 질문에서 (없으면 최근 1시간, 첫 답에 밝힘). 노트를 종류대로 먼저 쓴다 (판정 규칙으로 hop 가르기,
                     보는 법대로 조회, 함정 보정). 과거 이슈의 로그 문구 →
                     흐름 순서대로 hop 별 (파드·이벤트·로그·다음 hop 호출), 노트와 대조
   │
   ▼
③ 결론 + 제안        원인 지점, 근거 표, 못 본 것  +  "새로 알게 된 것" 표 → 사용자 OK → knowledge.py add / incident
   │
   ▼
④ 에이전트 성장      클러스터와 무관한 분석 요령 → 스킬 koa-learned (웹 "KOA 가 배운 것" 탭),  사용자·환경 사실 → 메모리
```

## 1. 무엇을 어디에 쌓나

| 무엇 | 어디 | 누가 쓰나 |
|---|---|---|
| 클러스터 사실: 구성요소 → 워크로드, 별명, 로그 문구, 흐름(별명), 노트(종류) | `local/knowledge/<클러스터>/flows.yaml` | 사용자 OK 후 KOA (`knowledge.py add`), 웹 화면 |
| 과거 이슈 1건 = 파일 1개 | `local/knowledge/<클러스터>/incidents/*.md` | 사용자 OK 후 KOA (`knowledge.py incident`), 웹 화면 |
| 아직 확인 안 된 제안 | `local/knowledge/<클러스터>/suggestions.yaml` | flowmap, KOA (`knowledge.py suggest`) → 웹에서 더하기/버리기 |
| 자유 메모 | `local/knowledge/<클러스터>/architecture.md` | 사용자 |
| 분석 요령 (어떻게 보는가. 클러스터 이름이 예로 들어가도 된다) | 스킬 `koa-learned` (프로필에만 있고 저장소에 없어서 업데이트가 덮지 않는다). 분석마다 통째로 읽히니 한 줄~두 줄씩 | KOA (알려 주고 바로) |
| KOA 자체를 만들고 운영하는 요령 (웹 재시작, 프로필 동기화, 벤치마크) | 프로필 전용 스킬 (예: `koa-dev-notes`) — 분석 때는 안 읽힌다 | KOA (처음 만들 때만 사용자 OK) |
| 사용자·환경에 대한 짧은 사실 | Hermes 메모리 | KOA |

**새 스킬은 사용자에게 묻고 만든다.** KOA 는 스킬로 남길 만한 것이 보이면 이름·언제 쓰이는지·내용을 두세 줄로 설명하고, 사용자가 OK 해야 만든다 (`koa-learned` 에 교훈 한 줄 더하기는 지금처럼 알리고 바로).
Hermes 가 대화 끝에 뒤에서 스킬을 만들고 고치는 기능(background review)은 배포 `config.yaml` 에서 끈다 (`auxiliary.background_review.enabled: false`). 켜 두면 세션 복구 절차처럼 클러스터 분석과 상관없는 스킬이 묻지도 않고 생긴다.
프로필 업데이트는 이미 있는 `config.yaml` 을 덮지 않으므로, 전에 설치한 프로필에는 한 번 직접 넣는다: `hermes -p koa config set auxiliary.background_review.enabled false`.

클러스터에 대한 사실(라벨, 시계 오프셋, 무엇이 무엇을 부르는지)은 메모리나 스킬이 아니라 지식 노트에 둔다. 유출 걱정 때문이 아니라 읽히는 방식 때문이다:
메모리는 매 턴, 스킬은 분석마다 통째로 들어가지만, 지식은 질문에 걸린 구성요소의 것만 꺼낸다. 같은 사실이 두 곳에 있으면 한쪽만 고쳐져 어긋난다.
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
  admin-console-wrapper:
    workload: console/Deployment/admin-wrapper
    group: wrapper             # 그룹: 흐름 칸에 'wrapper' 라고 쓰면 이 그룹의 구성요소 전부를 가리킨다
  user-console-wrapper:
    workload: console/Deployment/user-wrapper
    group: wrapper

flows:
  order-async:
    kind: queue
    hops: [order-api, rabbitmq, order-worker, orders-db]
    aliases: [주문 처리]          # 질문에서 이 흐름을 부르는 말

normal:                        # 노트 (키 이름은 예전 그대로 normal)
  - {component: order-worker, note: "매일 02:00 정산 배치 때 큐가 10만 건까지 쌓였다 30분 안에 빠진다", kind: normal}
  - {component: wrapper, note: "배포 때 30초 동안 502", kind: normal}     # 그룹 이름: 그 그룹의 구성요소 모두에 해당
  - {component: Nginx 로그, note: "upstream_status 가 비고 504 면 백엔드 미도달 → 앱 로그는 볼 필요 없음", kind: rule}  # 주제 노트
  - {component: Nginx 로그, note: "nginx 로그 시각은 UTC 라 9시간 이르다 — 다른 계층과 시각을 맞출 때 보정", kind: pitfall}
```

**노트.** `component` 가 구성요소·그룹 이름(또는 별명)이면 그것에 붙은 노트라 그 구성요소가 범위에 들 때만 꺼낸다.
그 밖의 말(`Nginx 로그`, `알림 인덱스`)이면 **주제 노트**: 클러스터 전체에 통하는 것이라 모든 분석에 같이 간다
(질문 낱말과 겹치는 것부터, 글자 수 3,000 자까지. 넘는 것은 주제 이름과 개수만 알리고 `knowledge.py notes` 로 읽게 한다).
`kind` 는 분석이 노트를 어떻게 쓰는지 정한다. 없으면 '종류 없음' 으로 내용대로 쓴다 (예전처럼 모두 "원인 아님" 으로 읽지 않는다).

| kind | 이름 | 분석이 쓰는 법 | 예 |
|---|---|---|---|
| `rule` | 판정 규칙 | 어느 hop 탓인지 가르고 다음에 볼 곳을 정한다 | upstream_status 가 비고 504 면 백엔드 미도달 |
| `howto` | 보는 법 | 이대로 조회한다 (인덱스 목록·매핑을 더듬기 전에) | logs-* 인덱스를 app 라벨로 좁혀 조회 |
| `pitfall` | 함정 | 데이터를 읽을 때 보정한다 | 한 인덱스에 두 리전 이벤트가 섞여 있다 (region 으로 좁힌다) |
| `baseline` | 평소 수치 | 지금 값과 견준다 (잰 날짜를 같이 적는다) | 평소 분당 300 건 안팎 (2026-10-12 잼) |
| `structure` | 구조 | 흐름·구성요소를 읽을 때 쓴다 | 워커가 rabbitmq 에 AMQP 로 알림 발행 |
| `normal` | 정상 패턴 | 같으면 원인이 아니다 | 매일 02:00 배치 때 큐가 쌓였다 빠진다 |

종류 없는 노트는 웹 '종류 제안' 이나 `knowledge.py notes <c> --untyped` 가 글을 보고 종류를 짐작해 준다 (단서 낱말 기준이라 사람이 확인한다).

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
python3 koa/knowledge.py notes <c> [찾을 말] [--untyped]         # 노트 목록 (주제 노트 포함, 종류 없는 것은 제안 종류와)
python3 koa/knowledge.py add <c> '<json>' [--dry-run]           # ③ OK 받은 것만 더한다
python3 koa/knowledge.py note <c> '{"component":…,"match":"글 일부","note"?:…,"kind"?:…,"remove"?:true}' [--dry-run]
                                                                # 노트 하나 고치기·지우기 (틀린 노트, 중복, 종류 잘못, 지난 평소 수치)
python3 koa/knowledge.py incident <c> '<json>'                  # 과거 이슈 1건
python3 koa/knowledge.py recur <c> <이슈파일> '{"date":…,"note":…,"components"?:[…]}'  # 같은 원인 재발: 기존 이슈에 한 줄 (새 파일 안 만듦).
                                                                # 다른 구성요소에서 재발했으면 components 로 그 구성요소도 이슈에 붙인다
python3 koa/knowledge.py suggest <c> '<json>' --title … --why …  # 나중에 고를 제안
python3 koa/knowledge.py suggestions <c> | accept <c> <id> | dismiss <c> <id>
python3 koa/knowledge.py check <c>                              # 워크로드·흐름 매칭 검사
```

흐름의 각 칸(hop)은 **구성요소 이름이나 그룹 이름 그대로** 쓴다. 그룹 칸은 그 그룹의 구성요소 전부를 뜻한다. 이름이 정확히 같을 때만 그 구성요소로 본다 (별명·부분 문자열로 붙이지 않는다).
노트의 `component` 와 과거 이슈의 `components` 도 같은 규칙이다: 그룹 이름을 쓰면 그 그룹의 구성요소 중 하나라도 범위에 들 때 꺼낸다
(`노트 wrapper (그룹: admin-wrapper, user-wrapper) [정상 패턴]: …`). 과거 이슈에서 그룹은 한 곳으로 센다.
별명(`aliases`, 구성요소와 흐름 둘 다)은 질문에서 부르는 말을 찾는 데만 쓴다. 예전 지식의 `flow_nodes` 는 읽을 때 한 번 옮긴다
(흐름 칸을 구성요소 이름으로 바꾸고, 여럿이 공유하던 이름은 워크로드를 모은 구성요소로, 나머지는 별명으로). 옮기기 전 파일은 `flows.yaml.pre-names.bak`.

`context` 가 꺼내는 범위: 질문에서 찾은(또는 `--with`) 구성요소·흐름 → 그걸 지나는 흐름 → 그 흐름 위 구성요소 → 이들과 관련된 과거 이슈·노트,
그리고 주제 노트. 구성요소는 더 넓히지 않는다.
**그룹 칸은 "이 중 하나 이상"** 이라 형제 멤버는 이번 경로가 아니다. 그래서 질문이 그룹의 멤버 하나(`billing-api`)를 짚으면 그 칸에서는
그 멤버만 범위에 넣고, 나머지는 이름만 한 줄로 알린다 (`그룹 백엔드 (16개 …): 이번 범위 billing-api — 나머지는 범위에서 뺐다: …`).
아무 멤버도 안 짚은 그룹 칸은 `GROUP_EXPAND`(5) 개 이하면 전부, 넘으면 이름만 둔다. 그룹 이름이나 흐름 이름을 말하면 전부.
그룹에 붙은 노트는 그 그룹 칸이 범위의 흐름 위에 있으면 늘 같이 간다. (0.2.20 실측: 백엔드 16개 그룹이 있는 실제 지식에서 멤버 하나를 물을 때 context 25~52% 감소)
이름이 하나도 안 걸리면 질문 낱말(조사를 뗀 한글 두 글자 이상, 영문·숫자 세 글자 이상)이 가장 많이 겹치는 흐름으로 시작한다
(흐름 이름·별명, 칸 이름, 그 구성요소의 별명·로그 문구·그룹과 견준다. 같은 점수면 둘까지). 분석은 첫 줄에 "X 흐름으로 읽었어요. 아니면 알려 주세요" 라고 쓰고
계속한다. 겹치는 흐름이 없거나 셋 이상 비기면 "어느 구성요소/흐름인지 사용자에게 묻는다" 고 알려 준다 — 클러스터 전체를 훑지 않는다.
사용자가 고쳐 준 말은 새 별명으로 제안한다.

`add` 는 더하기만 한다: 같은 이름·같은 워크로드의 구성요소는 목록 칸(aliases, log_signatures)을 합치고 빈 칸만 채운다. 기존 흐름과 같거나 그 일부인 흐름은 건너뛴다.
기존 흐름 이름에 `aliases` 만 주면 그 흐름에 별명을 더한다. 같은 대상·같은 글의 노트에 `kind` 를 주면 종류가 비어 있을 때만 채운다 (정해 둔 종류는 안 바꾼다).
모르는 종류는 건너뛰고 알린다. 저장 전 파일은 `flows.yaml.bak`.

제안은 내용으로 중복을 거르고, 버린 제안은 기억해서 다시 내지 않는다. 이미 지식에 있는 내용이면 남기지 않는다.

`check` 는 흐름·구성요소 말고도 다음을 알린다 (0.2.20):
- 과거 이슈의 머리말이 YAML 오류로 안 읽히거나(따옴표 없는 `?…]` 등), `components` 가 지식의 구성요소·그룹 이름이 아니다 → 그 이슈는 어떤 분석에도 안 나온다.
- 과거 이슈의 로그 문구가 IP 나 짧은 낱말(9자 이하, 예: `aiohttp`) → 정상 로그에도 걸려 거짓 단서가 된다.
- `baseline`(평소 수치) 노트에 잰 날짜가 없거나 `BASELINE_DAYS`(60) 일이 지났다 → 다시 재서 `knowledge.py note` 로 고친다.

## 4. 웹 화면 (`koa/web.py`)

지식을 줄로 보고, 줄을 눌러 그 자리에서 고치는 곳. LLM 을 부르지 않는다. 이 컴퓨터에서만 열린다 (127.0.0.1).

```bash
cd ~/.hermes/profiles/koa && /usr/bin/python3 koa/web.py     # → http://127.0.0.1:8765
```

| 탭 | 하는 일 |
|---|---|
| 클러스터 지식 | 맨 위에 **제안** 카드(무엇이 바뀌는지 미리 보기 + 더하기/버리기). 흐름(노드 칩, 순서 편집, 별명), 구성요소(별명·로그 문구·워크로드·그룹, "클러스터에서 고르기"는 읽기 전용 kubectl 로 워크로드 목록). 노트 수는 칩으로만 보이고 누르면 'KOA 가 배운 것' 으로 간다 |
| 과거 이슈 | 한 문단으로 쓰면 날짜·구성요소·로그 문구·원인을 뽑아 미리 보여 주고 저장. 저장한 이슈는 다음 `context` 에 바로 나온다 |
| KOA 가 배운 것 | 노트(전체 + 구성요소의 그룹, 없으면 구성요소 이름·주제별 탭과 찾기. 줄마다 종류를 고르고, **종류 제안** 은 종류 없는 노트에 짐작한 종류를 저장 전 상태로 채운다. 대상 칸에 그룹 이름이나 주제를 쓸 수 있다)과, KOA 가 만들거나 고친 스킬(`koa-learned` + curator 기록·`created_by: agent` 인 것, 한국어 요약·원문·수정내역 보기). 꺼 둔 기본 스킬은 안 보인다. 메모리는 보이지 않는다 (Hermes 의 메모리 도구 몫). `koa/learned.py` |

POST 는 `X-KOA: 1` 헤더가 있어야 받는다 (다른 사이트에서 보낸 요청 막기). 클러스터 이름·이슈 파일 이름은 정규식으로 검사한다.

**다른 곳에서 바뀐 지식.** 화면은 연 때의 지식을 들고 있다가 통째로 저장한다. 그래서 저장할 때 그때 읽은 버전(`version`, flows.yaml 내용 해시)과
그때 모습을 같이 보내고, 서버는 그사이 파일이 바뀌었으면 덮어쓰지 않고 합쳐서 쓴다 (`knowledge.save_shards(base=…)` → `merge3`).
KOA 채팅이 `knowledge.py add` 로 더한 별명·구성요소·노트·노트 종류는 웹에서 저장해도 남는다. 같은 칸(예: 같은 구성요소의 워크로드, 같은 흐름의 경로,
같은 노트의 종류)을 양쪽에서 다르게 고쳤을 때만 저장하지 않고 "최신 내용을 다시 불러올까요?" 를 묻는다. 고치던 것이 없으면 화면이 다시 보일 때(다른 창에서
돌아올 때) 새로 읽는다 (`GET /api/knowledge/<c>/stamp`).

**버전과 다시 시작.** 머리 줄 오른쪽에 이 웹의 버전과 폴더가 보인다 (`KOA 0.2.18 · ~/.hermes/profiles/koa`, 마우스를 올리면 지식 폴더).
프로필 업데이트나 git pull 로 디스크의 버전(`distribution.yaml`)이 이 웹과 달라지면 "웹 다시 시작" 버튼이 뜨고, 누르면 새 코드로 다시 뜬다
(같은 프로세스에서 exec 라서 LaunchAgent 로 띄운 것도 그대로). 새 코드가 import 되지 않으면 다시 시작하지 않고 오류를 보여 준다.
화면이 웹 서버보다 새로우면(예전 웹 서버) 다시 시작하는 법을, 개발 체크아웃에서 띄운 웹이면 KOA 채팅이 쓰는 지식(설치된 프로필)과 다른 폴더라는 것을 안내한다.
`GET /api/about`: `version`(뜰 때) · `disk_version`(지금 디스크) · `home` · `knowledge` · `installed`.

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

## 7. 분석 재기 (`koa/stats.py`)

지식 정리나 절차를 바꿔도 분석이 정말 빨라지고 가벼워졌는지는 재 봐야 안다. `stats.py` 는 Hermes 가 프로필마다 남기는 세션 기록
(`state.db`, SQLite)을 **읽기 전용**으로 열어 분석 대화마다 걸린 시간·모델 응답 수·도구 호출 수·토큰·비용을 보여 준다. 클러스터는 건드리지 않는다.

```bash
python3 koa/stats.py                         # 최근 14일 분석 대화 (knowledge.py context 를 부른 대화)
python3 koa/stats.py --until 2026-10-14      # 바꾸기 전
python3 koa/stats.py --since 2026-10-14      # 바꾼 뒤 → 두 중앙값을 견준다
python3 koa/stats.py --session <id>          # 대화 하나: 질문마다 걸린 시간과 도구 호출 순서
python3 koa/stats.py --all --json            # 분석이 아닌 대화도, JSON 으로
```

- 한 줄 = 대화 하나. 압축으로 이어진 세션과 하위 에이전트 실행은 처음 세션에 합친다 (리셋·갈래로 새로 시작한 대화는 따로).
- **작업 시간** 은 질문마다 '질문 → 그 질문의 마지막 기록' 을 더한 것이다. 사용자가 다음 질문을 하기까지 기다린 시간은 빠진다.
- **탐색** 은 OpenSearch 인덱스 목록·매핑 보기 호출 수다 (`ListIndexTool`·`IndexMappingTool`). 보는 법 노트가 있으면 줄어야 한다.
- 비밀처럼 보이는 인자 값(password·token 등)은 `--session` 출력에서 가린다.
- 같은 장애를 여러 번 돌려 견주는 것은 lab 의 `compare.py --repeat` ([lab/chaos/README.md](../lab/chaos/README.md)).

## 8. 정상 세션 잇기 (`koa/hoptrace.py`)

"정상 세션 추적해줘", "요청이 어디서 멈추는지" 처럼 홉을 이어야 할 때. 에이전트가 로그를 덤프하고 즉석 스크립트로
잇던 일(실측 약 70콜)을 한 번 실행(get pods/svc 네임스페이스당 1회 + 파드당 logs 1회)으로 바꾼다.

```bash
python3 koa/hoptrace.py <클러스터> "백엔드 정상 세션" --with 백엔드 [--since 1h] [--all-flows] [--json]
```

- 범위: `knowledge.context` 의 말한 구성요소 + 그 구성요소를 지나는 http 흐름(큐 흐름은 `--all-flows`).
- 파드: 워크로드의 ownerReferences 로 고른다 (Deployment selector 는 같은 앱의 job·listener 까지 잡는다).
  로그는 동시에 읽는다 — 버퍼가 짧은 nginx 가 그사이 밀려나지 않게.
- 잇는 규칙: `req-<UUID>` 와 `req-<32hex>` 는 하이픈을 떼고 같은 ID. 한 줄에 함께 찍힌 ID(최대 2개: global+local)는
  한 사슬. nginx access log(ingress-nginx 형식)의 upstream 이름은 Service 로 풀고 selector 로 구성요소에 붙인다.
  시각은 kubelet 타임스탬프를 UTC 로 맞춘 것 (컨테이너 시계·노드 시간대가 달라도 견줄 수 있다).
- 결과: 구성요소별 요청 수·상태, 바깥 host, 같은 사슬에 찍힌 구성요소, nginx 를 거친 줄(워크로드·UA),
  세 홉(호출자 호출 줄 → nginx → 앱)이 이어진 가장 최근 정상 세션과 시간 대조(바깥 ≥ 안쪽이면 "맞음"),
  워크로드별 로그 보존 깊이(가장 얕은 파드). 요청 줄을 못 찾은 구성요소는 따로 적는다.
- 못 하는 것: 앱 로그 형식이 `Processed request: <상태> … - <메서드> <URL>` 나 `"<메서드> <경로> HTTP/x" <상태>` 가
  아니면 요청 줄을 못 찾는다. 호출 줄은 `URL … time|took|elapsed|duration <초>` 형식만. 401 처럼 미들웨어가 끊은
  요청은 앱 로그에 없으니 nginx 쪽 숫자를 본다.

