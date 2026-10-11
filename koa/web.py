#!/usr/bin/env python3
"""KOA 웹 화면 — 클러스터 지식(구성요소·흐름·정상 패턴·제안)과 과거 이슈를 보고 고친다.
KOA 가 배운 것(이 프로필의 메모리, KOA 가 만들거나 고친 스킬)도 보여 준다 (learned.py).

  python3 koa/web.py                 # http://127.0.0.1:8765
  python3 koa/web.py --port 9000

분석은 KOA 채팅에서 한다. 이 화면은 지식 파일만 읽고 쓴다 (local/knowledge/<클러스터>/, knowledge.py 와 같은 파일).
표준 라이브러리 + PyYAML 만 쓴다. 클러스터에는 워크로드 목록 조회(get/list) 하나만 보낸다.
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import knowledge as K  # noqa: E402
import learned as L  # noqa: E402
from paths import REPO  # noqa: E402

HTML = Path(__file__).resolve().parent / "web" / "index.html"
FILE_RE = re.compile(r"^[0-9A-Za-z가-힣._-]{1,80}\.md$")
SID_RE = re.compile(r"^[0-9a-f]{10}$")
_lock = threading.Lock()
_REMOTE = False  # --host 0.0.0.0 이면 True (외부 접속 허용)


def rel(p):
    try:
        return str(Path(p).relative_to(REPO))
    except ValueError:
        return str(p)


def checks(lst):
    return [{"level": lv, "msg": m} for lv, m in lst]


# ---------- 지식 ----------

def get_knowledge(c):
    d = K.cluster_dir(c)
    if not d.is_dir():
        raise LookupError("지식이 없는 클러스터: %s" % c)
    data = K.load_flows(c)
    incs = K.load_incidents(c)
    for i in incs:
        i["text"] = i["body"] or incident_text_from_meta(i["meta"])
    return {
        "cluster": c,
        "understood": {"components": data["components"],
                       "flows": {n: {"kind": f.get("kind"), "nodes": K.flow_nodes(f)} for n, f in data["flows"].items()},
                       "normal": data["normal"]},
        "incidents": incs,
        "suggestions": K.list_suggestions(c),
        "check": checks(K.check(c, data)),
        "path": rel(d),
    }


def incident_text_from_meta(m):
    parts = [str(m.get("date") or ""), m.get("cause") or ""]
    if m.get("found_by"):
        parts.append("찾은 방법: %s" % m["found_by"])
    logs = K.incident_logs(m)
    if logs:
        parts.append("로그: " + ", ".join('"%s"' % x for x in logs))
    return "\n".join(p for p in parts if p)


def incident_preview(c, body):
    return K.incident_meta(body.get("text") or "", K.load_flows(c)["components"])


def save_incident(c, body):
    d = K.cluster_dir(c) / "incidents"
    d.mkdir(parents=True, exist_ok=True)
    text = (body.get("text") or "").strip()
    if not text:
        raise ValueError("내용이 비었다")
    meta = incident_preview(c, body)
    meta.setdefault("date", dt.date.today())
    name = body.get("file") or "%s-%s.md" % (meta["date"].isoformat(), K.slug(meta.get("cause") or ""))
    if not FILE_RE.match(name):
        raise ValueError("파일 이름 형식 오류: %s" % name)
    with _lock:
        (d / name).write_text(K.incident_text(meta, text))
    return name


def delete_incident(c, name):
    if not FILE_RE.match(name or ""):
        raise ValueError("파일 이름 형식 오류")
    f = K.cluster_dir(c) / "incidents" / name
    if f.is_file():
        f.unlink()


# ---------- 클러스터 고르기 ----------

def kubeconfig_context():
    """읽기 전용 kubeconfig 의 현재 컨텍스트에서 -readonly 를 뺀 이름."""
    kc = Path(os.environ.get("KOA_KUBECONFIG") or Path.home() / ".kube" / "hermes-readonly.yaml").expanduser()
    try:
        ctx = (yaml.safe_load(kc.read_text()) or {}).get("current-context") or ""
    except (OSError, yaml.YAMLError):
        return None
    return re.sub(r"-readonly$", "", ctx) or None


def cluster_name(n):
    """'user@cluster' 컨텍스트/탐색 프로필 이름은 @ 뒤를 쓴다."""
    n = (n or "").strip()
    return (n.rsplit("@", 1)[1] if "@" in n else n) or None


def cluster_info():
    have = K.clusters()
    cands = []
    for n in K.discovered() + [kubeconfig_context()]:
        cname = cluster_name(n)
        if cname and K.NAME_RE.match(cname) and cname not in have and cname not in cands:
            cands.append(cname)
    return {"clusters": have, "candidates": cands, "context": kubeconfig_context()}


_wl_cache = {}


def workloads(cluster):
    """구성요소 워크로드 입력 도우미: 읽기 전용 kubeconfig 로 Deployment·StatefulSet·DaemonSet 목록 (get/list 만)."""
    K.cluster_dir(cluster)  # 이름 검사
    hit = _wl_cache.get(cluster)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    kc = Path(os.environ.get("KOA_KUBECONFIG") or Path.home() / ".kube" / "hermes-readonly.yaml").expanduser()
    ctx = ((K.profile(cluster) or {}).get("kubeconfig") or {}).get("context")
    cmd = ["kubectl", "--kubeconfig", str(kc), "--request-timeout=15s"] + (["--context", ctx] if ctx else []) + \
          ["get", "deployments,statefulsets,daemonsets", "-A", "-o",
           "jsonpath={range .items[*]}{.metadata.namespace}/{.kind}/{.metadata.name}{\"\\n\"}{end}"]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as x:
        return {"workloads": [], "error": "kubectl 실행 실패: %s" % x}
    if p.returncode != 0:
        return {"workloads": [], "error": p.stderr.decode("utf-8", "replace").strip()[:200]}
    out = {"workloads": sorted(x for x in p.stdout.decode().splitlines() if x.count("/") == 2)}
    _wl_cache[cluster] = (time.time(), out)
    return out


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "koa-web"

    def log_message(self, fmt, *args):  # 조용히: 오류만
        if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            sys.stderr.write("%s\n" % (fmt % args))

    def _host_ok(self):
        # 로컬 전용이면 DNS 리바인딩 막기: 이 컴퓨터 주소로 들어온 요청만.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost", "[::1]") or _REMOTE

    def _send(self, code, obj=None, ctype="application/json; charset=utf-8", raw=None):
        data = raw if raw is not None else json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _route(self, method):
        if not self._host_ok():
            return self._send(403, {"error": "localhost 로만 연다"})
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        body = {}
        if method == "POST":
            # 다른 사이트의 폼 전송 막기: 사용자 지정 헤더는 같은 출처 fetch 만 붙일 수 있다
            if self.headers.get("X-KOA") != "1":
                return self._send(403, {"error": "X-KOA 헤더 필요"})
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        try:
            if method == "GET" and parts == []:
                return self._send(200, raw=HTML.read_bytes(), ctype="text/html; charset=utf-8")
            if parts[:1] != ["api"]:
                return self._send(404, {"error": "not found"})
            p = parts[1:]
            if method == "GET" and p == ["clusters"]:
                return self._send(200, dict(cluster_info(), knowledge=rel(K.KNOWLEDGE)))
            if method == "POST" and p == ["clusters"]:
                K.init(body.get("name") or "")
                return self._send(200, {"ok": True})
            if method == "GET" and len(p) == 2 and p[0] == "workloads":
                return self._send(200, workloads(p[1]))
            if method == "GET" and p == ["learned"]:
                return self._send(200, L.summary())
            if method == "POST" and p == ["learned", "memory", "delete"]:
                with _lock:
                    left = L.delete_memory(body.get("target") or "", body.get("text") or "")
                return self._send(200, {"left": len(left)})
            if method == "POST" and len(p) == 4 and p[:2] == ["learned", "skills"] and p[3] == "summarize":
                if not re.match(r"^[a-z0-9][a-z0-9._-]{0,63}$", p[2]):
                    raise ValueError("스킬 이름 형식이 아니다")
                try:
                    return self._send(200, L.summarize(p[2]))
                except (RuntimeError, subprocess.TimeoutExpired) as x:
                    return self._send(502, {"error": str(x)[:400]})
            if p[:1] == ["knowledge"] and len(p) >= 2:
                c = p[1]
                if method == "GET" and len(p) == 2:
                    return self._send(200, get_knowledge(c))
                if method == "GET" and p[2:] == ["candidates"]:
                    return self._send(200, {"candidates": K.discover_candidates(c)})
                if method == "POST" and p[2:] == ["add_components"]:
                    with _lock:
                        added, chk = K.add_discovered_components(c, body.get("workloads") or [])
                    return self._send(200, {"added": added, "check": checks(chk)})
                if method == "POST" and p[2:] == ["shards"]:
                    with _lock:
                        saved = K.save_shards(c, body)
                    return self._send(200, {"check": checks(saved["check"])})
                if method == "POST" and len(p) == 5 and p[2] == "suggestions" and p[4] in ("accept", "dismiss"):
                    if not SID_RE.match(p[3]):
                        raise ValueError("제안 id 형식이 아니다")
                    with _lock:
                        r = K.resolve_suggestion(c, p[3], p[4] == "accept")
                    return self._send(200, {"result": K.additions_text(r) if r else None})
                if method == "POST" and p[2:] == ["incidents", "preview"]:
                    return self._send(200, {"meta": incident_preview(c, body)})
                if method == "POST" and p[2:] == ["incidents"]:
                    return self._send(200, {"file": save_incident(c, body)})
                if method == "POST" and p[2:3] == ["incidents"] and p[4:] == ["delete"]:
                    delete_incident(c, p[3])
                    return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})
        except LookupError as x:
            return self._send(404, {"error": str(x)})
        except (ValueError, yaml.YAMLError, KeyError, TypeError) as x:
            return self._send(400, {"error": str(x)})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")


def main():
    global _REMOTE
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1",
                    help="바인딩 주소 (기본 127.0.0.1 로컬 전용). 0.0.0.0 은 인증 없이 외부 접속을 허용한다")
    a = ap.parse_args()
    if a.host != "127.0.0.1":
        _REMOTE = True
        print("⚠ 경고: %s 으로 열어 외부 접속을 허용했습니다. 인증이 없어 누구나 지식을 고칠 수 있습니다." % a.host)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print("KOA 웹: http://%s:%d  (지식 %s)  Ctrl+C 로 끝" % (a.host, a.port, rel(K.KNOWLEDGE)))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
