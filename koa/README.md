# koa/ — 클러스터 탐색 → MCP 설치 계획

클러스터를 연결하면 KOA 가 처음 하는 일. 무엇이 있는지 훑어 **클러스터 프로필**을 만들고, 그걸 보고 붙일 MCP 서버를 정한다.

보통은 KOA 프로필에서 "클러스터 확인하고 MCP 세팅해줘" 라고 하면 에이전트가 스킬 `koa-cluster-discovery` 를 따라 아래를 돌린다.

```bash
cd "$HERMES_HOME"                    # 설치본 ~/.hermes/profiles/koa, 개발은 저장소 루트
python3 koa/discover.py --probe      # 1) 프로필 <이름>.yaml + 결과 표·제안 보고서 <이름>.report.md
python3 koa/plan.py <이름>           # 2) 설치 계획 (아무것도 바꾸지 않음)
python3 koa/plan.py <이름> --apply   # 3) 준비된 verified 서버를 이 프로필에 등록 → 앱 재시작
python3 koa/check_mcp.py <MCP...>    # 4) 서버를 직접 띄워 실제 도구 목록·쓰기 도구 확인
python3 koa/report.py <이름>         # (선택) 프로필만으로 보고서 다시 만들기
```

**원칙: KOA 는 클러스터를 바꾸지 않는다.** 지금 쓸 수 있는 범위 안에서만 수집·분석하고, KOA 가 하는 변경은 MCP 설치뿐이다.

보고서는 네 부분이다: ① 찾은 구성요소 표(접근 주소·probe·Prometheus 수집 여부) ② 붙일 수 있는 MCP 표
③ 분석 한계와 KOA 대응(지금 쓸 수 있는 도구로 메우는 방법) ④ 다음 단계(MCP 만). 문구는 `catalog.yaml` 의
`gap_advice`(한계별 영향·workaround)와 구성요소의 `fallback` 에서 온다. 클러스터 설정 변경은 제안하지 않는다.

필요: `kubectl`, `python3` + PyYAML, 읽기 전용 kubeconfig(`~/.kube/hermes-readonly.yaml`, 만드는 법은 루트 README).

## 파일

| 파일 | 내용 |
|---|---|
| `discover.py` | 읽기 전용 kubeconfig 로 클러스터를 훑어 프로필을 쓴다 |
| `catalog.yaml` | 무엇을 찾을지(구성요소 감지 규칙)와 찾으면 어떤 MCP 를 붙일지(서버 정의) |
| `plan.py` | 프로필 + 카탈로그 → 설치 계획. `--apply` 는 `hermes config set mcp_servers.<이름>` 으로 등록 |
| `check_mcp.py` | MCP 서버를 stdio 로 띄워 실제 도구 목록과 `include` 비교 |
| `paths.py` | 경로 판별: 설치본이면 프로필 폴더(`.env`, `config.yaml`, 결과는 `local/clusters/`), 개발 체크아웃이면 저장소 `clusters/` + `HERMES_HOME` |
| `<결과>/<이름>.yaml` | 클러스터 프로필. 우리만의 형식(`koa.cluster-profile/v1`). 자동 생성, 직접 고치지 않는다 |

## 프로필에 들어가는 것

- `kubernetes` — 버전, 배포판(provider), 노드·파드 수
- `access` — 원인 분석에 필요한 권한(`can`)과 막혀 있어야 할 권한(`cannot`), `read_only` 판정.
  쓰기·exec·port-forward·Secret 중 하나라도 허용되면 경고하고 `plan.py` 가 진행을 거부한다
- `components` — 감지한 구성요소: 종류(metrics/logs/gitops/message-queue …), 위치(ns/워크로드), 이미지,
  서비스 포트, **클러스터 밖 접근 주소**(Ingress), `--probe` 결과(HTTP 상태)
- `events` — 남아 있는 k8s 이벤트 수와 가장 오래된 것의 나이(= 사실상 보존 기간)
- `gaps` — 원인 분석에 필요한데 없는 신호 (지표 없음, 이벤트 보존 짧음, 접근 주소 없음 …)

## 안전 장치

- 클러스터에는 get/list 와 `auth can-i` 만 보낸다. `~/.kube/config`(관리자) 로는 실행을 거부한다.
- `--probe` 는 찾아낸 관측 시스템 주소의 health 경로에 GET 한 번. 앱 경로로는 보내지 않는다.
- MCP 는 `catalog.yaml` 의 `status` 로 나뉜다.
  - `verified` — lab 에서 직접 붙여 읽기 전용임을 확인. `--apply` 로 등록된다.
  - `candidate` — README 로 읽기 전용 설정만 확인. `--with <이름>` 을 줘야 등록된다.
- 비밀 값은 `~/.hermes/.env` 에 "있는지"만 본다. 없으면 등록하지 않고 할 일로 안내한다.

## candidate 를 verified 로 올리는 법

1. 읽기 전용 계정을 만든다 (Grafana Viewer, Argo CD get/list role, RabbitMQ `monitoring` 태그 …).
2. `.env` 를 채우고 `plan.py <이름> --apply --with <서버>` → 앱 재시작.
3. 도구 목록이 카탈로그의 `include` 와 같은지, 쓰기 시도가 거부되는지 확인한다.
4. `catalog.yaml` 의 `status` 를 `verified` 로 바꾸고 커밋.

## 감지 방식과 한계

- 컨테이너 **이미지 이름** + **CRD API 그룹**으로 찾는다. 사내 미러로 이미지 이름이 바뀐 경우는
  `catalog.yaml` 의 `image` 정규식을 늘린다.
- 클러스터 밖(SaaS: Datadog, Grafana Cloud, Elastic Cloud …)에 있는 관측 시스템은 클러스터만 봐서는 알 수 없다.
  에이전트(DaemonSet) 이미지로 존재만 감지하고, 주소는 사람이 넣어야 한다.
- 접근 주소는 Ingress 만 본다. LoadBalancer·NodePort·VPN 너머 주소는 직접 지정한다.
