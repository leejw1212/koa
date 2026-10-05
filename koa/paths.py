"""KOA 경로 해석. 같은 스크립트가 두 곳에서 돈다.

- 설치본: `hermes profile install` 이 저장소를 ~/.hermes/profiles/<이름>/ 에 풀어 놓은 것.
  프로필 폴더 자체가 HERMES_HOME 이고, 클러스터 결과는 사용자 영역 local/clusters/ 에 쓴다
  (프로필 update 가 local/ 은 건드리지 않는다).
- 개발 체크아웃: git clone 한 저장소. HERMES_HOME 은 환경변수나 ~/.hermes, 결과는 저장소 clusters/.
"""
import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CATALOG = REPO / "koa" / "catalog.yaml"

# 설치본에는 install 이 만든 사용자 폴더(memories/ 등)가 있다. 저장소에는 없다.
INSTALLED = (REPO / "distribution.yaml").is_file() and (REPO / "memories").is_dir()

if INSTALLED:
    HERMES_HOME = REPO
    CLUSTERS = REPO / "local" / "clusters"
else:
    HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
    CLUSTERS = REPO / "clusters"

ENV_FILE = HERMES_HOME / ".env"
ENV_HINT = str(ENV_FILE).replace(str(Path.home()), "~", 1)  # 사용자에게 보여줄 경로
CONFIG = HERMES_HOME / "config.yaml"
# 다른 설치본의 hermes 를 쓰면 게이트웨이 서비스 정의가 깨진다 → 사용자 런처 고정
HERMES_BIN = Path.home() / ".local" / "bin" / "hermes"


def read_env():
    """HERMES_HOME/.env 를 dict 로. 값은 호출한 쪽에서도 출력하지 않는다."""
    out = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                if k.startswith("export "):
                    k = k[len("export "):].strip()
                out[k] = v.strip().strip('"').strip("'")
    return out


def hermes_env():
    """hermes CLI 를 이 HERMES_HOME(프로필)에 대해 실행할 환경.
    ~/.hermes 밖 HERMES_HOME 으로 hermes CLI 를 돌리면 설치본 런처(~/.hermes/hermes-agent/.hermes/bin/hermes)가
    그 폴더 기준 Python 경로로 다시 써져 hermes 명령 전체가 깨진다(2026-10-05 실측) → 거부한다."""
    root = (Path.home() / ".hermes").resolve()
    try:
        HERMES_HOME.resolve().relative_to(root)
    except ValueError:
        raise SystemExit("HERMES_HOME(%s) 이 ~/.hermes 밖이다. hermes CLI 런처가 깨질 수 있어 실행하지 않는다. "
                         "테스트는 ~/.hermes/profiles/<이름> 프로필로 한다." % HERMES_HOME)
    return {**os.environ, "HERMES_HOME": str(HERMES_HOME)}
