#!/usr/bin/env python3
"""verified 로 올리기 전 점검: config.yaml 의 MCP 서버를 Hermes 와 같은 방식(stdio)으로 직접 띄워 tools/list 를 받는다.
include 목록과 실제 도구 이름을 비교한다. ${VAR} 는 ~/.hermes/.env 로 채운다(값은 출력하지 않음)."""
import json, os, re, select, subprocess, sys, time
from pathlib import Path
import yaml

HOME = Path.home()
env_file = {}
for l in (HOME / ".hermes" / ".env").read_text().splitlines():
    if "=" in l and not l.lstrip().startswith("#"):
        k, v = l.split("=", 1); env_file[k.strip()] = v.strip()
subst = {"userHome": str(HOME), **env_file}
expand = lambda s: re.sub(r"\$\{(\w+)\}", lambda m: subst.get(m.group(1), ""), str(s))

cfg = yaml.safe_load((HOME / "hermes-config" / "config.yaml").read_text())["mcp_servers"]
names = sys.argv[1:] or list(cfg)


def rpc(p, msg):
    p.stdin.write((json.dumps(msg) + "\n").encode()); p.stdin.flush()


def read(p, want_id, timeout):
    end = time.time() + timeout
    buf = b""
    while time.time() < end:
        r, _, _ = select.select([p.stdout], [], [], 1)
        if not r:
            if p.poll() is not None:
                return None
            continue
        line = p.stdout.readline()
        if not line:
            return None
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("id") == want_id:
            return m
    return None


for name in names:
    s = cfg[name]
    env = {**os.environ, **{k: expand(v) for k, v in (s.get("env") or {}).items()}}
    cmd = [s["command"]] + [expand(a) for a in s.get("args", [])]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    rpc(p, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "koa-check", "version": "0"}}})
    init = read(p, 1, 180)
    if not init:
        err = p.stderr.read1(2000).decode(errors="replace") if p.poll() is not None else "(timeout)"
        print("## %s: 초기화 실패\n%s" % (name, err[-800:])); p.kill(); continue
    rpc(p, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    rpc(p, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tl = read(p, 2, 60)
    p.kill()
    tools = sorted(t["name"] for t in (tl or {}).get("result", {}).get("tools", []))
    inc = (s.get("tools") or {}).get("include")
    print("## %s  (server %s)" % (name, init["result"].get("serverInfo", {})))
    print("   실제 도구 %d개: %s" % (len(tools), ", ".join(tools)))
    if inc:
        print("   include 중 없는 이름: %s" % (sorted(set(inc) - set(tools)) or "없음"))
        print("   노출될 도구: %s" % sorted(set(inc) & set(tools)))
    w = [t for t in tools if re.search(r"(create|update|delete|patch|sync|run_|write|import|set_|manage|publish|close)", t, re.I)]
    print("   쓰기로 보이는 도구: %s" % (w or "없음"))
