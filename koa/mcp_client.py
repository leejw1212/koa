"""MCP 서버를 Hermes 와 같은 방식(stdio)으로 직접 띄워 쓰는 최소 클라이언트.

check_mcp.py(검증)와 query.py(조회)가 같이 쓴다. 서버 정의는 HERMES_HOME/config.yaml 의 mcp_servers.<이름>,
${VAR} 는 HERMES_HOME/.env 로 채운다. 값은 어디에도 출력하지 않는다.
"""
import json
import os
import re
import select
import subprocess
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import CONFIG, INSTALLED, read_env  # noqa: E402

PROTOCOL = "2024-11-05"


def registered():
    """이 프로필에 등록된 mcp_servers dict."""
    return yaml.safe_load(CONFIG.read_text()).get("mcp_servers") or {}


def _expand(s, subst):
    return re.sub(r"\$\{(\w+)\}", lambda m: subst.get(m.group(1), ""), str(s))


class MCPError(RuntimeError):
    pass


class Server:
    """with Server("argocd") as s: s.tools(); s.call("list_applications", {...})"""

    def __init__(self, name, spec=None, timeout=180):
        self.name = name
        self.spec = spec or registered().get(name)
        if not self.spec:
            raise MCPError("등록되지 않은 MCP: %s" % name)
        self.timeout = timeout
        self.p = None
        self.info = {}
        self._id = 0

    # ── 수명 ──────────────────────────────────────────
    def __enter__(self):
        # 설치본은 프로필 .env 만 쓴다 (터미널이 다른 프로필 값을 물려받았을 수 있음)
        envfile = read_env()
        subst = {"userHome": str(Path.home()), **({} if INSTALLED else os.environ), **envfile}
        env = {**os.environ, **{k: _expand(v, subst) for k, v in (self.spec.get("env") or {}).items()}}
        cmd = [self.spec["command"]] + [_expand(a, subst) for a in self.spec.get("args", [])]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        r = self._request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                         "clientInfo": {"name": "koa", "version": "1"}}, self.timeout)
        self.info = r.get("serverInfo", {})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    def __exit__(self, *exc):
        if self.p and self.p.poll() is None:
            self.p.kill()
        return False

    # ── JSON-RPC ──────────────────────────────────────
    def _send(self, msg):
        self.p.stdin.write((json.dumps(msg) + "\n").encode())
        self.p.stdin.flush()

    def _request(self, method, params, timeout):
        self._id += 1
        rid = self._id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        end = time.time() + timeout
        while time.time() < end:
            r, _, _ = select.select([self.p.stdout], [], [], 1)
            if not r:
                if self.p.poll() is not None:
                    break
                continue
            line = self.p.stdout.readline()
            if not line:
                break
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("id") == rid:
                if "error" in m:
                    raise MCPError("%s: %s" % (method, m["error"].get("message", m["error"])))
                return m.get("result", {})
        if self.p.poll() is not None:
            err = self.p.stderr.read().decode(errors="replace")
            raise MCPError("서버 종료: %s" % err.strip()[-600:])
        raise MCPError("%s 응답 없음 (%ds)" % (method, timeout))

    # ── 기능 ──────────────────────────────────────────
    def tools(self):
        """실제 서버가 내놓는 도구 [{name, description, inputSchema}, ...]"""
        return self._request("tools/list", {}, 60).get("tools", [])

    def allowed(self):
        """Hermes 가 노출하는 도구 이름 (include 가 있으면 그것만)."""
        return set((self.spec.get("tools") or {}).get("include") or [])

    def call(self, tool, args=None, timeout=60):
        """도구 호출 → (is_error, text). include 밖 도구는 거부한다."""
        inc = self.allowed()
        if inc and tool not in inc:
            raise MCPError("%s.%s 는 허용 목록(include) 밖이라 호출하지 않는다" % (self.name, tool))
        r = self._request("tools/call", {"name": tool, "arguments": args or {}}, timeout)
        text = "\n".join(c.get("text", "") for c in r.get("content", []) if c.get("type") == "text")
        if not text and r.get("structuredContent") is not None:
            text = json.dumps(r["structuredContent"], ensure_ascii=False)
        return bool(r.get("isError")), text
