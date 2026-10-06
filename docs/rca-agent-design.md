# KOA 원인 분석 에이전트 구조 (설계안)

> 2026-10-06 · 제안 단계, 아직 구현 전 · 기준 버전 v0.1.1 (탐색 → MCP 자동 연결까지 완료)

## 0. 요약

| 원칙 | 내용 |
|---|---|
| 목표 | **MTTR 중 "원인 파악 시간"을 줄인다.** 첫 가설이 나오기까지의 시간과, 확정까지의 시간을 잰다 |
| 기본 골격 | **kubernetes MCP 하나만으로 끝까지 분석이 된다.** 다른 MCP 는 없어도 되는 "가속기"다 |
| 가속기 | 붙어 있으면 같은 질문을 더 빨리·더 넓게 답한다 (예: 언제부터 → Prometheus 범위 조회). 없으면 k8s 경로로 대신한다 |
| 클러스터 지식 | 관리자가 적은 **구조 설명**(서비스 의존 관계, 중요도, 정상 패턴, 알려진 문제)을 분석 순서와 판단에 쓴다 |
| 반복 학습 | 끝난 분석은 증상 서명과 원인으로 남겨, 다음에 같은 증상이면 첫 단계에서 바로 맞춘다 |
| 원칙 유지 | 클러스터는 바꾸지 않는다. 지식 파일은 KOA 쪽(`local/clusters/`)에만 쓴다 |

시간을 줄이는 지점은 세 군데다.

1. **넓게 훑는 일은 코드가 한다.** 전체 파드·이벤트·변경 이력을 LLM 이 하나씩 조회하지 않고, 스크립트가 k8s MCP 로 한 번에 모아 40줄 안팎의 "이상 징후 요약"으로 만든다.
2. **어디부터 볼지는 클러스터 지식이 정한다.** 증상 서비스의 의존 관계, 최근 변경, 알려진 문제부터 본다.
3. **같은 일을 두 번 조사하지 않는다.** 과거 분석의 증상 서명이 맞으면 그 원인부터 확인한다.

## 1. 전체 구조

```
                    ┌──────────────────────── 클러스터 지식 (local/clusters/<이름>.*) ────────────────────────┐
                    │  ① 자동 탐색 프로필   ② 관리자 구조 설명   ③ 분석 기록(증상 서명 → 원인)                 │
                    │     <이름>.yaml          <이름>.context.yaml   <이름>.incidents.yaml                     │
                    └───────────────┬──────────────────────────────────────────────────────────────────────────┘
                                    │ 분석 시작 때 한 번 읽음 (요약본)
사용자 증상 ─▶ [0 접수] ─▶ [1 트리아지] ─▶ [2 첫 가설 보고] ─▶ [3 검증 루프] ─▶ [4 결론 보고] ─▶ [5 기록]
                 범위·시각     triage.py         사용자에게          플레이북 단계            근거 표          incidents.yaml
                 정규화        (k8s MCP 일괄)    중간 보고           (능력 → 도구 선택)       미확인 항목       에 추가
                                    │                                    │
                                    ▼                                    ▼
                              ┌─────────── 능력 해석기 (capabilities) ───────────┐
                              │ "질문"을 지금 붙은 도구 중 가장 빠른 경로로 바꿈   │
                              │  기본: kubernetes MCP   가속: prometheus·로그·    │
                              │  argocd·grafana (붙어 있을 때만)                  │
                              └──────────────────────────────────────────────────┘
```

에이전트는 "어떤 MCP 를 부를까"가 아니라 **"무슨 질문에 답해야 하나"**로 생각한다. 질문을 도구로 바꾸는 일은 능력 해석기가 한다. 그래서 MCP 가 하나든 다섯이든 분석 절차는 같고, 붙은 것이 많을수록 각 단계가 빨라질 뿐이다.

## 2. 클러스터 지식: 세 겹

### ① 자동 탐색 프로필 (있음)

`koa/discover.py` 결과. 구성요소, 권한, 이벤트 보존, 분석 한계. 그대로 쓴다.

### ② 관리자 구조 설명 (새로 추가)

> 2026-10-06 갱신: 한 파일(`context.yaml`) 대신 자유 서술 + 흐름 구조 + 과거 이슈 폴더로 나누고, 조사 요청과 합치는 방식을 [koa-analysis-inputs.md](koa-analysis-inputs.md) 에 구체화했다. 아래는 처음 안이다.

클러스터 관리자가 아는 것 중 클러스터만 봐서는 알 수 없는 것. 파일은 `local/clusters/<이름>.context.yaml` (설치본 업데이트에도 유지되는 사용자 데이터 위치).

```yaml
schema: koa.cluster-context/v1
cluster: prod-a
updated: 2026-10-06
services:                      # 분석 단위. k8s 워크로드와 연결
  order-api:
    workload: shop/Deployment/order-api
    tier: critical             # critical | normal | batch → 트리아지 순위에 반영
    entry: "shop.example.com/api/orders (ingress-nginx)"
    depends_on: [payment-api, redis-main, ext:mysql-orders]
    logs: {index: "app-shop-*", fields: {request_id: req_id, level: lvl}}   # 로그 MCP 가 있을 때
    normal:                    # 정상인데 이상해 보이는 것 → 오탐 줄이기
      - "매일 02:00 정산 배치로 CPU 3배"
      - "하루 1~2회 재시작은 알려진 메모리 누수 (JIRA-123), 장애 아님"
  payment-api:
    workload: shop/Deployment/payment-api
    tier: critical
    depends_on: [ext:pg-gateway]
external:                      # 클러스터 밖 의존성. k8s 로는 상태를 못 보니 증상 문구로 판정
  mysql-orders:
    kind: database
    where: "AWS RDS"
    symptoms: ["Communications link failure", "Too many connections"]
  pg-gateway:
    kind: http
    symptoms: ["502 from pg", "timeout after 3000ms"]
known_issues:                  # 관리자가 이미 아는 장애 패턴
  - id: KI-redis-evict
    match: {service: order-api, log: "OOM command not allowed"}
    cause: "redis-main maxmemory 도달"
    check: "redis-main 파드 로그의 used_memory 경고"
change_windows: ["화·목 14:00-16:00 KST 정기 배포"]
notes: |
  자유 서술. 구조 설명, 담당 팀, 과거 큰 장애 등.
```

입력 방법은 세 가지로 연다. 셋 다 같은 파일이 된다.

| 방법 | 설명 |
|---|---|
| 대화 | "order-api 는 payment-api 랑 redis-main 에 의존해" → KOA 가 파일에 반영하고 바뀐 줄을 보여 준다 |
| 초안 자동 생성 | `koa/context.py draft`: 디플로이먼트 env·ConfigMap 의 `*.svc.cluster.local`, `*_HOST`, URL 값에서 의존 관계를 추정해 초안을 만든다. 관리자는 고치기만 한다 (Secret 은 읽지 않는다) |
| 직접 편집 | YAML 또는 `notes` 에 자유 서술 |

검증: `koa/context.py check` 가 설명과 실제 클러스터를 비교한다. 적힌 워크로드가 없으면 "설명이 낡음", 트래픽 받는 디플로이먼트가 설명에 없으면 "설명 빠짐"으로 보고한다. 설명이 틀리면 분석이 엇나가므로, 분석 보고서에도 "설명과 실제가 다른 곳"을 적는다.

### ③ 분석 기록 (새로 추가)

분석이 끝나면 `local/clusters/<이름>.incidents.yaml` 에 한 건씩 남긴다.

```yaml
- at: 2026-10-06T05:12:00Z
  symptom: "order-api 5xx 급증"
  signature: {service: order-api, pod_reason: CrashLoopBackOff, log: "Too many connections"}
  root_cause: "mysql-orders 커넥션 한도. payment-api 배포 후 커넥션 풀 2배"
  evidence: [kubectl_logs order-api, rs payment-api-7f9 생성 05:03]
  time_to_first_hypothesis: 4m
  time_to_root_cause: 11m
  confirmed_by: user
```

트리아지가 증상 서명을 `known_issues` 와 이 기록에 맞춰 보고, 맞으면 첫 가설 1순위로 올린다. 사용자가 확인한 것만 남긴다.

## 3. 분석 흐름

### 단계 0. 접수 (사람 말 → 분석 범위)

- 시각을 UTC 로 바꾼다 (사용자는 KST, 로그는 UTC).
- 증상을 서비스에 연결한다: "주문이 안 돼요" → 구조 설명의 `entry`·서비스 이름으로 `order-api`.
- 범위가 모호해도 묻지 않고 기본값으로 진행한다: 시각 없으면 "최근 1시간", 서비스 없으면 클러스터 전체.

### 단계 1. 트리아지 — `koa/triage.py` (새로 추가, 핵심)

k8s MCP 만으로 넓게 훑고 요약한다. LLM 이 조회 수십 번을 하는 대신 스크립트가 한 번에 한다 (`koa/mcp_client.py` 로 같은 허용 목록 안에서 `kubectl_get -A -o json`).

| 보는 것 | 뽑는 신호 |
|---|---|
| 노드 | NotReady, Memory/Disk/PIDPressure, 최근 재부팅 |
| 파드 | 비정상(Pending 사유, CrashLoopBackOff, ImagePull), 기간 안 재시작과 `lastState.terminated`(OOMKilled, exitCode, 시각) |
| 동시성 | 여러 파드가 같은 시각에 재시작 → 노드·호스트 단위 사건 (기존 스킬 3단계를 코드로) |
| 서비스 | 준비된 엔드포인트 0개인 서비스 (트래픽이 실제로 끊긴 곳) |
| 이벤트 | Warning 을 사유·대상별로 묶어 건수·첫/마지막 시각 |
| 변경 | 기간 안 새 ReplicaSet·롤아웃, StatefulSet revision, ConfigMap `managedFields` 시각, HPA 스케일 |
| 지식 결합 | 구조 설명의 `tier` 로 순위, `depends_on` 으로 "증상 서비스의 의존 대상 중 이상 있는 것", `normal` 로 오탐 표시, `known_issues`·분석 기록 서명 일치 |

출력은 두 가지다.
- 화면용: 40줄 안팎 요약 (이상 징후 순위표 + 변경 타임라인 + 서명 일치).
- 파일: `local/clusters/<이름>.triage/<시각>.json` 전체 결과. 이후 단계가 다시 조회하지 않고 참조한다.

가속기가 붙어 있으면 트리아지에 한 줄씩 더한다: Prometheus `ALERTS{alertstate="firing"}`, 로그 저장소의 서비스별 오류 건수 급증, Argo CD 최근 동기화. 없으면 그 칸은 "확인 안 함(도구 없음)"으로 남긴다.

### 단계 2. 첫 가설 보고 (중간 보고)

트리아지 직후, 검증 전에 사용자에게 먼저 보낸다. 장애 대응 중인 사람에게는 확정된 답보다 **"어디를 보고 있는지"가 빨리 가는 것**이 시간을 줄인다.

```
지금까지: order-api 파드 3개 CrashLoopBackOff (05:04~), 같은 시각 payment-api 새 ReplicaSet (05:03).
가설 1 (유력): payment-api 배포 후 DB 커넥션 한도 초과 — 과거 기록 1건과 서명 일치
가설 2: mysql-orders 자체 장애
다음 확인: order-api 이전 컨테이너 로그, payment-api 이전·현재 설정 비교
```

### 단계 3. 검증 루프 — 플레이북 + 능력 해석기

가설마다 확인할 "질문"을 순서대로 푼다. 질문 목록은 증상 유형별 플레이북(`koa/playbooks.yaml`, 새로 추가)에 둔다.

| 플레이북 | 주요 질문 |
|---|---|
| crashloop | 종료 사유·exitCode, 이전 로그 마지막 줄, 최근 변경, 의존 대상 상태 |
| oom | limits 대비 사용량, 언제부터 늘었나, 같은 노드 다른 파드 |
| pending | 스케줄 실패 사유, 노드 자원·taint, PVC 바인딩 |
| no-endpoints / 5xx | readiness 실패 사유, ingress 백엔드, 의존 대상 |
| node | 노드 조건, 그 노드 파드 일괄 재시작 |
| deploy-regression | 변경 시각과 증상 시각 일치, 이전/현재 스펙 차이 |
| log-pipeline | (기존 스킬 4단계) 원본 vs 저장소 비교, 의도된 필터 빼기 |
| external-dependency | 구조 설명의 `symptoms` 문구가 로그에 있나 |

각 질문은 능력 해석기가 도구로 바꾼다. 붙어 있는 것 중 위에서부터 쓴다.

| 질문 | 기본 (k8s MCP 만) | 가속기 (붙어 있으면 먼저) |
|---|---|---|
| 언제부터인가 | 이벤트, `lastState` 시각, 재시작 시각 | Prometheus 범위 조회로 지표 변곡점 |
| 무엇이 바뀌었나 | ReplicaSet 생성·revision, `managedFields` 시각, 스펙 비교 | Argo CD history·diff (누가·어떤 커밋) |
| 어디가 아픈가 | 파드 상태, 엔드포인트, Warning 이벤트 | `ALERTS`, Grafana 경보 규칙 |
| 오류 내용 | `kubectl_logs` (`previous`, `since`, `tail`) — 살아 있는 파드만 | OpenSearch/Loki 검색 — 삭제·재시작 전 파드, 여러 파드 집계 |
| 자원 | requests/limits, OOMKilled 기록 | cAdvisor 지표 추세 |
| 의존 대상 | 구조 설명 `depends_on` + 의존 대상 파드 상태·로그 | 트레이스, 서비스 지표 |
| 외부 의존성 | 구조 설명 `symptoms` 문구를 앱 로그에서 찾기 | 로그 저장소 집계 |

`koa/playbooks.yaml` 은 `catalog.yaml` 처럼 질문 → `{k8s: <query>, prometheus: <query>, ...}` 형태로 적는다. 기존 `query.py` 의 이름 붙인 조회를 그대로 재사용하고, 탐색 프로필의 `gap_advice.workaround` 는 "가속기가 없을 때의 설명"으로 보고서에 붙인다.

멈추는 조건: 한 가설에 "지지 근거 2개 이상, 반대 근거 없음"이면 확정 후보, 3회 루프 안에 안 좁혀지면 그때까지의 순위와 막힌 이유(빠진 신호)를 보고한다.

### 단계 4. 결론 보고 (기존 형식 유지)

결론 + 확신도, 근거 표(행마다 출처 도구), 타임라인, **확인하지 못한 것과 그 이유(어떤 도구가 있었으면 확인됐는지)**, 권장 조치(실행하지 않음). "어떤 도구가 있었으면"은 가속기를 붙일지 판단하는 근거가 된다.

### 단계 5. 기록

사용자가 원인을 확인하면 분석 기록에 남기고, 구조 설명에 없던 의존 관계를 찾았으면 추가를 제안한다.

## 4. MCP 를 적게 붙여야 할 때

| 상황 | 방법 |
|---|---|
| kubernetes MCP 하나 | 위 흐름 전부 동작. 로그는 살아 있는 파드만, 시작 시각은 이벤트·재시작 시각으로 추정. 보고서에 그렇게 밝힌다 |
| 하나 더 붙일 수 있다면 | **로그 저장소**(OpenSearch/Loki) 우선. 재시작·삭제된 파드 로그와 여러 파드 집계가 k8s 로는 대신이 안 된다. 그다음 Prometheus(시작 시각·추세), Argo CD(변경 출처) |
| 도구 수·컨텍스트가 문제라면 | 가속기를 대화 도구로 등록하지 않고 `triage.py`·`query.py` 가 stdio 로만 부르게 할 수도 있다. 에이전트 도구 목록은 kubernetes 하나로 두고, 가속기 결과는 요약으로만 들어온다 (지금 `query.py` 가 이미 이 방식) |

"MCP 를 많이 못 붙인다"가 회사 승인 문제인지, 도구 수·컨텍스트 문제인지에 따라 세 번째 줄의 가치가 달라진다.

## 5. MTTR 을 재는 법

kind-lab 에 장애를 일부러 넣고(lab 에서만) 시간을 잰다. 같은 장애를 "kubernetes 만"과 "가속기 포함"으로 두 번씩 돌린다.

| 장면 | 넣는 방법 (lab) |
|---|---|
| 잘못된 이미지 배포 | 존재하지 않는 태그로 롤아웃 |
| OOM | limits 를 낮춘 배포 |
| 의존 대상 다운 | redis 레플리카 0 |
| 설정 회귀 | ConfigMap 의 접속 주소 오타 |
| 노드 장애 | kind 워커 컨테이너 정지 |
| 로그 파이프라인 중단 | fluentd 출력 주소 오류 |

지표: 첫 가설까지 시간, 원인 확정까지 시간, 도구 호출 수, 정답 여부, 구조 설명 유무에 따른 차이. 결과는 `docs/` 에 남긴다.

## 6. 구현 순서 (한 단계씩 검증)

| # | 내용 | 산출물 | 확인 |
|---|---|---|---|
| 1 | 구조 설명 형식 + `context.py draft/check` | `koa/context.py`, 스키마, kind-lab 예시 | kind-lab 에서 초안 생성·불일치 검출 |
| 2 | 트리아지 (k8s MCP 만) ✅ 2026-10-06 | `koa/triage.py` ([기록](koa-triage.md)) | 장애 장면 7개, 원인 징후 상위 (API 서버 전용 lab). 실제 kind-lab 확인 남음 |
| 3 | 트리아지에 지식 결합 (tier, depends_on, normal, 서명) | 같은 파일 | 구조 설명 있을 때/없을 때 순위 비교 |
| 4 | 플레이북 + 능력 해석기 | `koa/playbooks.yaml` | 가속기 없을 때 k8s 경로로 대체되는지 |
| 5 | 스킬 개편 | `cluster-incident-analysis` SKILL.md 를 위 단계로 | 새 채팅에서 장애 분석 1회 |
| 6 | 분석 기록 + 서명 일치 | `incidents.yaml` | 같은 장애 두 번째에 첫 가설 1순위로 나오는지 |
| 7 | MTTR 측정 | `docs/` 결과 표 | 5절 장면 전부 |

## 7. 정해야 할 것

| 항목 | 기본값 (따로 말이 없으면) |
|---|---|
| 구조 설명 입력 형식 | YAML + 자유 서술 `notes`. 대화 입력은 KOA 가 YAML 로 옮긴다 |
| 첫 가설 중간 보고 | 항상 보낸다 |
| 가속기 등록 방식 | 지금처럼 Hermes 에 등록. 도구 수가 문제로 확인되면 스크립트 전용으로 전환 |
| 회사 클러스터의 구조 설명·분석 기록 | `local/` 에만, 저장소에는 커밋하지 않는다 (내부 호스트명 포함) |
