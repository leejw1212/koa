# hermes-config

Hermes Agent 설정 저장소. 어느 장비에서든 clone → `install.sh` → `.env` 채우기로 같은 구성이 된다.

```bash
git clone git@github.com:leejw1212/koa.git ~/hermes-config
~/hermes-config/install.sh
```

| 파일 | 커밋 | 내용 |
|---|---|---|
| `config.yaml` | ✅ | 모델·MCP 설정. `~/.hermes/config.yaml` 이 이 파일로 가는 심볼릭 링크 |
| `.env.example` | ✅ | 필요한 비밀 키 목록 |
| `install.sh` | ✅ | 링크 연결 + `.env` 생성 |
| `~/.hermes/.env` | ❌ | 실제 키 |

## 주의

- `~/.hermes/config.yaml` 이 링크라서 `hermes config set` 은 **이 저장소 파일을 바로 고친다.**
  설정 바꾼 뒤 `git diff` → 커밋.
- 장비마다 다른 값은 `${VAR}` 로 쓰고 `.env` 에 둔다.
- 변경 반영: **데스크톱 앱 재시작** (MCP 서버가 다시 뜸. 새 채팅만으로는 안 바뀜).
  메시징 게이트웨이를 쓰면 `hermes gateway restart` 도 — 게이트웨이는 앱과 별도 프로세스라 MCP 서버를 따로 띄운다.
- 에이전트 터미널: `terminal/agent-env.sh` 가 `KUBECONFIG` 를 읽기 전용 kubeconfig 로 고정한다
  (`terminal.shell_init_files`, 새 세션부터). 저장소를 `~/hermes-config` 가 아닌 곳에 clone 하면 config 의 경로를 맞춘다.

## 클러스터 탐색 → MCP 설치 (KOA 첫 동작)

```bash
python3 koa/discover.py --probe     # 클러스터 프로필 clusters/<이름>.yaml
python3 koa/plan.py <이름>          # 어떤 MCP 를 붙일지 계획
python3 koa/plan.py <이름> --apply  # verified 서버 등록
```

자세히: [koa/README.md](koa/README.md)

## MCP

전체 절차와 검증 방법: [docs/setup-guide.md](docs/setup-guide.md)

- `opensearch` — `opensearch-mcp-server-py`, 쓰기 차단(`OPENSEARCH_SETTINGS_ALLOW_WRITE=false`) + 조회 도구 5개만 노출.
  접속 정보는 `~/.hermes/.env` 의 `OPENSEARCH_URL/USERNAME/PASSWORD`.
- `kubernetes` — `mcp-server-kubernetes`, 비파괴 모드 + 조회 도구 4개만 노출
  (`kubectl_get`, `kubectl_describe`, `kubectl_logs`, `explain_resource`).
  kubeconfig 는 **읽기 전용 SA** 전용 파일 `~/.kube/hermes-readonly.yaml` (`KUBECONFIG_PATH`).
  도구 필터가 풀려도 쓰기·exec·Secret 조회는 API 서버(RBAC)에서 거부된다.

### 읽기 전용 kubeconfig 만들기 (장비/클러스터마다 1회)

```bash
~/hermes-config/k8s/make-readonly-kubeconfig.sh            # 현재 컨텍스트 기준
~/hermes-config/k8s/make-readonly-kubeconfig.sh kind-lab   # 컨텍스트 지정
```

- `k8s/hermes-readonly.yaml` 적용: ns `hermes`, SA `hermes-readonly`, ClusterRole `view` + 노드/PV 조회.
- 생성된 kubeconfig 에는 토큰이 들어 있으므로 **저장소에 넣지 않는다** (600 권한).
- 토큰 폐기: `kubectl -n hermes delete secret hermes-readonly-token` 후 스크립트 재실행.
- 확인: `kubectl --kubeconfig ~/.kube/hermes-readonly.yaml auth can-i delete pods -A` → `no`
