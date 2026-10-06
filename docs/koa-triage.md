# KOA 트리아지 — 장애 분석 첫 단계 (`koa/triage.py`)

> 2026-10-06 · 설계안 [rca-agent-design.md](rca-agent-design.md) 6절 2단계 · kubernetes MCP 만 사용

## 1. 왜

원인 파악 시간(MTTR)의 앞부분은 "어디가 이상한지 찾는 시간"이다. LLM 이 파드·이벤트·롤아웃을 하나씩 조회하면
도구 호출이 수십 번이고 컨텍스트가 원본 JSON 으로 찬다. 트리아지는 그 일을 스크립트가 한 번에 하고,
LLM 은 40줄 안팎의 요약에서 시작한다.

관측 시스템(Prometheus, 로그 저장소 등)은 클러스터 밖 접근 주소가 없으면 붙일 수 없다. 그래서 트리아지는
**kubernetes MCP 하나만으로** 동작하게 만들었다. 가속기 MCP 는 나중에 한 줄씩 더하는 구조다.

## 2. 사용

```bash
python3 koa/triage.py                          # 최근 1시간, 클러스터 전체
python3 koa/triage.py --since 3h               # 기간
python3 koa/triage.py --at 2026-10-06T05:10Z   # 그 시각까지 (사후 분석)
python3 koa/triage.py --ns shop,payment        # 이 네임스페이스만 (노드는 전체)
python3 koa/triage.py --source kubectl         # MCP 대신 읽기 전용 kubeconfig 로 kubectl
python3 koa/triage.py --json                   # 전체 결과 JSON
```

결과 JSON 은 `<결과 폴더>/<클러스터>.triage/<시각>.json` (설치본 `local/clusters/`, 개발 `clusters/`, 커밋 안 함).

## 3. 무엇을 보나

| 대상 | 잡는 것 | 점수 (높을수록 위) |
|---|---|---|
| 노드 | NotReady 100, NetworkUnavailable 90, *Pressure 80, 기간 안 Ready 복귀 50, cordon 30 | |
| 파드 → 워크로드로 묶음 | CrashLoopBackOff 90, 이미지 못 받음·설정 오류 85, OOMKilled 85, 스케줄 안 됨 80, 종료·Failed 60, NotReady 60, 기간 안 재시작 50 | |
| 워크로드 | 가용 0 75 / 일부 45, 롤아웃 멈춤(ProgressDeadlineExceeded) 70, Job 실패 55 | |
| 서비스 | 준비된 엔드포인트 0 → 뒤의 워크로드에 80, 맞는 파드가 없으면 서비스로 65 | |
| 이벤트 | Warning 을 대상·사유로 묶어 근거로 붙임. 묶일 곳이 없으면 사유별 점수로 따로 | |
| 동시 재시작 | 같은 1~2분에 워크로드 3개 이상 재시작 → "노드·호스트 사건" 한 줄 95. 재시작 말고 다른 이상이 없는 워크로드는 이 줄로 접는다 | |
| NotReady 노드 위 파드 | 노드 사건의 결과로 보고 40 으로 낮추고 노드 쪽 근거로 돌린다 | |
| 변경 (최근 6h) | Deployment 새 ReplicaSet(이미지 전후 비교, 같으면 "설정·env 변경"), StatefulSet·DaemonSet revision, 노드 추가, 이상 있는 네임스페이스의 ConfigMap | |
| 직전 변경 | 이상 징후마다 같은 네임스페이스에서 시작 시각 직전 변경을 붙인다 (자기 워크로드 변경 우선, +10/+5) | |

"시작"은 근거 시각 중 가장 이른 것: 재시작 종료 시각, Ready 전환 시각, 첫 Warning 이벤트 시각.

## 4. 검증 (2026-10-06)

### 테스트 환경

이번 작업 환경에서는 kind 가 뜨지 않았다(컨테이너 안에서 컨테이너 생성 불가). 대신 **etcd + kube-apiserver 만** 띄우고
(controller-tools envtest 바이너리 v1.34.1), 장애가 났을 때 API 서버에 남는 모습(파드 status, 이벤트, RS, EndpointSlice)을
`lab/triage-fixtures.py` 로 직접 써 넣었다. 실제 kubernetes MCP(`mcp-server-kubernetes@4.1.9`, 카탈로그 정의 그대로)와
`k8s/hermes-readonly.yaml` 의 읽기 전용 SA 로 조회했다.

- API 서버만 있으면 `view` ClusterRole 이 비어 있다(집계 컨트롤러가 없음). lab 에서는 `aggregate-to-view` 규칙을 `view` 에 직접 복사했다.
- API 서버가 생성 시각을 실제 시각으로 찍으므로 fixture 는 장면 기준 시각을 20분 뒤로 잡고, 트리아지는 출력된 `--at` 으로 돌린다.

이 fixture 는 **컨트롤러가 없는 API 서버 전용**이다. kind-lab 처럼 컨트롤러·kubelet 이 있는 클러스터에 넣으면
Deployment/RS 컨트롤러가 진짜 파드를 만들고 노드 수명 컨트롤러가 가짜 노드를 정리해 장면이 바뀐다.
kind-lab 에서는 실제 장애(잘못된 이미지 태그, 낮은 limits, 레플리카 0)를 넣어 확인한다.

### 결과 — 장면 7개, kubernetes MCP, 3.4초

| 장면 | 넣은 것 | 트리아지 결과 |
|---|---|---|
| A | order-api 롤아웃(1.2→1.3) 뒤 3/3 CrashLoop, 엔드포인트 0 | **1위**. 가용 0/3·엔드포인트 0, 직전 변경 "새 ReplicaSet rev 2 (order:1.2→order:1.3)" 연결 |
| D | lab-worker-2 NotReady | 2위. node-exporter 파드는 "NotReady 노드 위 파드" 로 7위로 내림 |
| E | lab-worker-1 워크로드 4개가 같은 분 exit 255 | 3위 한 줄 "모두 노드 lab-worker-1 → 노드·호스트 사건 의심", 4개 워크로드는 이 줄로 접힘 |
| F | legacy ImagePullBackOff | 4위 |
| B | payment-api OOMKilled 2건 | 5위, 직전 변경 ConfigMap payment-config |
| C | report Pending (메모리 부족) | 6위, 스케줄 실패 메시지·이벤트 12건 |
| G | 정상 워크로드·Normal 이벤트 | 안 나옴 |

출력 예 (요약):

```
# 트리아지 kind-lab · 2026-10-06 08:16Z (17:16 KST) · 최근 1.0h · kubernetes MCP · 3.4초
이상 7건 (워크로드 5, 노드 1, 클러스터 1) · 최근 6.0h 변경 5건
| 1 | shop/Deployment/order-api | CrashLoopBackOff, 가용 0/3, 엔드포인트 0 | 07:57 | back-off 5m0s ... | 07:56 Deployment/order-api 새 ReplicaSet rev 2 (order-api order:1.2→order:1.3) |
| 2 | node/lab-worker-2 | NotReady | 08:06 | NodeStatusUnknown: Kubelet stopped posting node status. ... | – |
| 3 | cluster/동시재시작/07:36 | 워크로드 4개 동시 재시작 | 07:36 | 모두 노드 lab-worker-1 → 노드·호스트 사건 의심; 대상: infra/... | – |
...
## 다음 확인
1. shop/Deployment/order-api 이전 컨테이너 로그: python3 koa/query.py --call kubernetes kubectl_logs '{... "previous": true}'
```

첫 실행은 `npx` 가 MCP 패키지를 받느라 22초, 그다음부터 3초 안팎이다 (MCP 기동 1.8초 + 조회 12개).

## 5. 만들면서 알게 된 것

| 문제 | 원인 | 대응 |
|---|---|---|
| `kubectl_get` 목록이 이름·상태만 온다 | mcp-server-kubernetes 4.1.9 는 `output: json` 목록을 name/namespace/status/createdAt 으로 줄인다 | `output: yaml` 로 받아 전체 필드를 쓴다 |
| 큰 클러스터에서 출력 한도 | MCP 기본 `SPAWN_MAX_BUFFER` 1MB | 트리아지가 띄우는 MCP 프로세스에서만 256MB 로 올린다 (Hermes 등록 설정은 그대로) |
| ConfigMap 수정 시각이 안 보인다 | kubectl 기본 출력이 `managedFields` 를 뺀다. MCP 에서 바꿀 방법이 없다 | MCP 에서는 생성 시각만 보고 "확인 못 한 것"에 적는다. `--source kubectl` 은 `--show-managed-fields` 로 수정도 본다 |
| 재시작 시작 시각이 이틀 전으로 나옴 | `lastState.terminated.startedAt`(그 컨테이너가 뜬 시각)을 썼다 | 종료 시각(`finishedAt`)으로 바꿈 |

## 6. 한계

- k8s 상태만 본다. 파드는 멀쩡한데 지연·오류율이 오른 장애는 안 보인다 (출력 끝에 항상 적는다).
- 이벤트는 보존 기간(보통 1h) 안의 것만, 재시작 이력은 파드별 마지막 1회만 남는다.
- ConfigMap 은 이상 있는 네임스페이스만 읽는다 (전체는 크다: Grafana 대시보드 등).
- 실제 kubelet 이 있는 클러스터(kind-lab, 운영)에서는 아직 돌려 보지 않았다. 다음 확인 대상이다.

## 7. 다음

1. kind-lab 이 있는 장비에서 실제 장애(잘못된 이미지, OOM, 레플리카 0)로 다시 확인
2. 관리자 구조 설명(`context.yaml`)의 `tier`·`depends_on`·`normal` 을 순위에 반영 (설계안 6절 3단계)
