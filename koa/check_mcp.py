#!/usr/bin/env python3
"""verified 로 올리기 전 점검: 등록된 MCP 서버를 Hermes 와 같은 방식(stdio)으로 직접 띄워 tools/list 를 받는다.
include 목록과 실제 도구 이름을 비교하고, 쓰기로 보이는 도구를 표시한다. ${VAR} 는 HERMES_HOME/.env 로 채운다.

  python3 koa/check_mcp.py [이름...]

권한 확인은 koa/readonly.py, 실제 조회는 koa/query.py --probe.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_client import MCPError, Server, registered  # noqa: E402

WRITE_LIKE = re.compile(r"(create|update|delete|patch|sync|run_|write|import|set_|manage|publish|close|rollback|terminate|exec)", re.I)

cfg = registered()
for name in sys.argv[1:] or list(cfg):
    try:
        with Server(name, cfg.get(name)) as s:
            tools = sorted(t["name"] for t in s.tools())
            inc, info = s.allowed(), s.info
    except MCPError as x:
        print("## %s: 초기화 실패\n   %s" % (name, x))
        continue
    print("## %s  (server %s)" % (name, info))
    print("   실제 도구 %d개: %s" % (len(tools), ", ".join(tools)))
    if inc:
        print("   include 중 없는 이름: %s" % (sorted(inc - set(tools)) or "없음"))
        exposed = sorted(inc & set(tools))
        print("   노출될 도구 %d개: %s" % (len(exposed), exposed))
    else:
        exposed = tools
        print("   include 없음 → 전부 노출")
    w = [t for t in exposed if WRITE_LIKE.search(t)]
    print("   노출 도구 중 쓰기로 보이는 것: %s" % (w or "없음"))
