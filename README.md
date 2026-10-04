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
- 변경 반영: MCP 는 `/reload-mcp`, 그 외는 Hermes 재시작(새 세션).

## MCP

- `kubernetes` — `mcp-server-kubernetes`, 비파괴 모드 + 조회 도구 4개만 노출
  (`kubectl_get`, `kubectl_describe`, `kubectl_logs`, `explain_resource`).
  kubeconfig 는 `~/.kube/config` 현재 컨텍스트.
