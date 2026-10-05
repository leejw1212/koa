# KOA — Kubernetes 장애 분석 에이전트 (Hermes 프로필 배포판)

연결한 클러스터를 **읽기 전용으로** 탐색하고, 쓸 수 있는 관측 도구를 정리한 뒤, 사용자와 함께 읽기 전용 MCP 를 붙인다.
그다음 같은 도구로 장애 원인을 분석한다. **대상 클러스터는 절대 바꾸지 않는다.**

이 저장소는 [Hermes profile distribution](https://hermes-agent.nousresearch.com/docs/user-guide/profile-distributions) 이다.
설치하면 별도 프로필 `koa` 가 생기고, 규칙(SOUL.md)·스킬·도구가 처음부터 주입된다.

## 설치 (장비마다 1회)

```bash
hermes profile install github.com/leejw1212/koa --alias   # → ~/.hermes/profiles/koa, 명령 `koa`
```

- 모델 인증은 프로필마다 따로다. `koa setup` 으로 모델·키를 넣거나, 기본 프로필의 `~/.hermes/auth.json` 을 쓰면 된다.
- 클러스터 쪽 준비는 **클러스터 관리자가 1회**: 읽기 전용 ServiceAccount 와 kubeconfig

  ```bash
  ~/.hermes/profiles/koa/k8s/make-readonly-kubeconfig.sh <관리자-컨텍스트>   # → ~/.kube/hermes-readonly.yaml
  ```

## 사용

데스크톱 앱에서 프로필을 **koa** 로 바꾸고(또는 터미널에서 `koa`) 이렇게만 말하면 된다.

> 클러스터 확인하고, 쓸 수 있는 도구 정리해서, 같이 MCP 세팅하자

KOA 가 하는 일 (스킬 `koa-cluster-discovery`):

| 단계 | 내용 | 사용자 |
|---|---|---|
| 1. 사전 점검 | 읽기 전용 kubeconfig 인지 `can-i` 로 확인 | – |
| 2. 탐색 | `koa/discover.py --probe` → 클러스터 프로필 + 보고서 | – |
| 3. 보고 | 구성요소 표, 붙일 수 있는 MCP 표, 분석 한계와 대응 | 확인 |
| 4. 선택 | 어떤 MCP 를 붙일지, 필요한 읽기 전용 토큰 | 고르고 `.env` 에 토큰 입력 |
| 5. 등록·검증 | `koa/plan.py --apply`, `koa/check_mcp.py` (도구 목록·쓰기 차단) | – |
| 6. 적용 | 앱 재시작 후 새 채팅에서 확인 | 앱 재시작 |

장애 분석은 스킬 `cluster-incident-analysis`.

## 무엇이 들어 있나

| 경로 | 내용 | 프로필에 주입 방식 |
|---|---|---|
| `SOUL.md` | KOA 정체성과 항상 지키는 규칙 (클러스터 무변경, 읽기 전용, 토큰은 사용자에게) | 모든 대화의 시스템 프롬프트 |
| `skills/devops/` | `koa-cluster-discovery`, `cluster-incident-analysis`, `hermes-mcp-servers`, `kubernetes-agent-access` | 스킬 목록 → 해당 작업 때 로드 |
| `config.yaml` | 모델, 터미널 `KUBECONFIG` 기본값. **MCP 는 비어 있음** (클러스터마다 온보딩 때 등록) | 프로필 설정 |
| `koa/` | `discover.py`, `plan.py`, `report.py`, `check_mcp.py`, `catalog.yaml` (MCP 정의·검증 상태) | 도구 |
| `k8s/` | 읽기 전용 SA 매니페스트 + kubeconfig 생성 스크립트 | 클러스터 관리자용 |
| `terminal/agent-env.sh` | 에이전트 터미널의 `KUBECONFIG` 를 읽기 전용 파일로 | `terminal.shell_init_files` |
| `distribution.yaml` | 배포 매니페스트 (설치 대상 경로, 선택 env 목록) | – |
| `docs/` | 설계·검증 기록 | 참고 |
| `lab/`, `clusters/` | kind-lab 개발용 (설치본에는 안 들어감) | – |

설치본에서 사용자 데이터는 업데이트해도 유지된다: `.env`(토큰), `memories/`, `sessions/`, `local/clusters/`(탐색 결과).

## 업데이트

```bash
hermes profile update koa                 # SOUL·스킬·koa/ 갱신, config.yaml 은 유지 (등록한 MCP 보존)
hermes profile update koa --force-config  # config.yaml 도 저장소 것으로 (등록한 MCP 사라짐 → 온보딩 다시)
```

## 개발 (이 저장소에서 직접)

```bash
git clone git@github.com:leejw1212/koa.git ~/hermes-config
cd ~/hermes-config
python3 koa/discover.py --probe          # 결과는 clusters/ (설치본은 local/clusters/)
python3 koa/plan.py kind-lab
hermes profile install ~/hermes-config --name koa-dev   # 로컬 디렉터리로 설치 테스트
```

- 기본 프로필에서 개발할 때는 `skills.external_dirs: [~/hermes-config/skills]` 로 저장소 스킬을 바로 읽는다.
- 스크립트의 경로는 `koa/paths.py` 가 정한다 (설치본이면 프로필 폴더, 아니면 `HERMES_HOME` 또는 `~/.hermes`).
- MCP 를 verified 로 올리는 절차: 스킬 `koa-cluster-discovery` → "Promoting candidate → verified".

## 한계

- 터미널 `KUBECONFIG` 고정은 기본값일 뿐 보안 경계가 아니다. `--kubeconfig ~/.kube/config` 를 직접 주면 관리자 권한이 된다. 경계는 MCP 의 읽기 전용 SA(RBAC) 다.
- 메모리(`memories/`)는 배포에 포함되지 않는다(Hermes 설계). 항상 필요한 규칙은 `SOUL.md`, 작업 절차는 스킬에 둔다.

자세히: [koa/README.md](koa/README.md) · [docs/koa-cluster-discovery.md](docs/koa-cluster-discovery.md) · [docs/setup-guide.md](docs/setup-guide.md)
