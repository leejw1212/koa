#!/usr/bin/env python3
"""KOA 조회 — 등록된 MCP 서버를 Hermes 와 같은 방식(stdio)으로 띄워 카탈로그에 정의한 조회를 실행한다.

  python3 koa/query.py                                  # 쓸 수 있는 조회 목록
  python3 koa/query.py prometheus up                    # 이름 붙인 조회
  python3 koa/query.py kubernetes logs name=echo-xxx ns=default tail=50
  python3 koa/query.py prometheus promql q='rate(http_requests_total[5m])'
  python3 koa/query.py --probe [이름...]                 # 서버마다 probe 조회 1개 → 실제로 읽히는지 표
  python3 koa/query.py --call argocd get_application '{"params": {"name": "linkcard"}}'   # 허용 목록 안 도구 직접 호출

조회 정의는 koa/catalog.yaml 의 mcp_servers.<이름>.probe / queries. 허용 목록(include) 밖 도구는 부르지 않는다.
결과는 MCP 서버가 돌려준 텍스트 그대로 출력한다(--max 로 자름). 비밀 값은 출력하지 않는다.
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_client import MCPError, Server, registered  # noqa: E402
from paths import CATALOG  # noqa: E402


def catalog():
    return yaml.safe_load(CATALOG.read_text())["mcp_servers"]


def fill(obj, vals):
    """args 안의 "{key}" 를 값으로 바꾼다. 문자열 전체가 "{key}" 하나면 숫자도 원래 타입으로 넣는다."""
    if isinstance(obj, dict):
        return {k: fill(v, vals) for k, v in obj.items()}
    if isinstance(obj, list):
        return [fill(v, vals) for v in obj]
    if isinstance(obj, str):
        m = re.fullmatch(r"\{(\w+)\}", obj)
        if m:
            if m.group(1) not in vals:
                raise SystemExit("값이 필요하다: %s=" % m.group(1))
            v = vals[m.group(1)]
            return int(v) if isinstance(v, str) and v.isdigit() else v
        def one(mm):
            if mm.group(1) not in vals:
                raise SystemExit("값이 필요하다: %s=" % mm.group(1))
            return str(vals[mm.group(1)])
        return re.sub(r"\{(\w+)\}", one, obj)
    return obj


def list_queries(cat, reg):
    print("등록된 MCP 와 조회 (python3 koa/query.py <MCP> <조회> [key=value ...])\n")
    for name, m in cat.items():
        if name not in reg:
            continue
        print("● %s" % name)
        for q, d in (m.get("queries") or {}).items():
            dflt = ", ".join("%s=%s" % kv for kv in (d.get("defaults") or {}).items())
            print("    %-12s %s%s" % (q, d.get("help", d["tool"]), ("  [기본 %s]" % dflt) if dflt else ""))
    missing = [n for n in cat if n not in reg]
    if missing:
        print("\n(미등록: %s)" % ", ".join(missing))


def probe(names, cat, reg):
    """서버마다 probe 조회 1개. 표로 출력하고 결과 dict 를 돌려준다."""
    rows, out = [], {}
    for n in names:
        p = (cat.get(n) or {}).get("probe")
        if n not in reg:
            out[n] = {"ok": False, "detail": "미등록"}
        elif not p:
            out[n] = {"ok": False, "detail": "카탈로그에 probe 없음"}
        else:
            t0 = time.time()
            try:
                with Server(n, reg[n]) as s:
                    exposed = s.allowed() & {t["name"] for t in s.tools()}
                    err, text = s.call(p["tool"], p.get("args") or {})
                ok = not err and bool(text.strip())
                out[n] = {"ok": ok, "tools": len(exposed), "secs": round(time.time() - t0, 1),
                          "detail": "%s → %s" % (p["tool"], summary(text) if ok else ("오류: " + text[:160]))}
            except MCPError as x:
                out[n] = {"ok": False, "detail": "실행 실패: %s" % str(x)[:200]}
        r = out[n]
        rows.append("| %s | %s | %s | %s | %s |" % (n, "✅" if r["ok"] else "❌", r.get("tools", "–"), r.get("secs", "–"),
                                                  r["detail"].replace("|", "\\|").replace("\n", " ")))
    print("| MCP | 조회 | 노출 도구 | 초 | 결과 |\n|---|---|---|---|---|\n" + "\n".join(rows))
    return out


def summary(text, n=120):
    """조회 결과 한 줄 요약."""
    try:
        d = json.loads(text)
    except ValueError:
        return text.strip().splitlines()[0][:n]
    if isinstance(d, dict):
        if "items" in d:
            return "%d개 항목" % len(d["items"])
        if d.get("resultType") == "vector":
            res = d.get("result", [])
            return "값 %s" % res[0]["value"][1] if len(res) == 1 else "시계열 %d개" % len(res)
        for k in ("datasources", "applications", "dashboards"):
            if k in d:
                return "%s %d개" % (k, len(d[k]))
        if "status" in d:
            return "status=%s" % d["status"]
    return json.dumps(d, ensure_ascii=False)[:n]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", action="store_true", help="서버마다 probe 조회 1개를 돌려 표로 보여준다")
    ap.add_argument("--call", action="store_true", help="<MCP> <도구> '<json 인자>' 를 그대로 호출 (허용 목록 안만)")
    ap.add_argument("--max", type=int, default=6000, help="출력 최대 글자 수 (기본 6000, 0=제한 없음)")
    ap.add_argument("rest", nargs="*")
    a = ap.parse_args()
    cat, reg = catalog(), registered()

    if a.probe:
        res = probe(a.rest or list(reg), cat, reg)
        sys.exit(0 if all(r["ok"] for r in res.values()) else 1)
    if not a.rest:
        list_queries(cat, reg)
        return
    name = a.rest[0]
    if name not in reg:
        sys.exit("등록되지 않은 MCP: %s (등록됨: %s)" % (name, ", ".join(reg) or "없음"))

    if a.call:
        if len(a.rest) < 2:
            sys.exit("사용법: --call <MCP> <도구> '<json 인자>'")
        tool, args = a.rest[1], json.loads(a.rest[2]) if len(a.rest) > 2 else {}
    else:
        qs = (cat.get(name) or {}).get("queries") or {}
        if len(a.rest) < 2 or a.rest[1] not in qs:
            sys.exit("%s 조회: %s" % (name, ", ".join(qs) or "없음"))
        q = qs[a.rest[1]]
        vals = dict(q.get("defaults") or {})
        for kv in a.rest[2:]:
            if "=" not in kv:
                sys.exit("인자는 key=value: %s" % kv)
            k, v = kv.split("=", 1)
            vals[k] = v
        tool, args = q["tool"], fill(q.get("args") or {}, vals)

    try:
        with Server(name, reg[name]) as s:
            err, text = s.call(tool, args, timeout=120)
    except MCPError as x:
        sys.exit("실패: %s" % x)
    if a.max and len(text) > a.max:
        text = text[:a.max] + "\n… (%d자 중 %d자, --max 로 조정)" % (len(text), a.max)
    print(text)
    sys.exit(1 if err else 0)


if __name__ == "__main__":
    main()
