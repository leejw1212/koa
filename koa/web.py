#!/usr/bin/env python3
"""KOA 웹 화면 — 클러스터 지식 편집, 조사 요청, 결과 보기. 이 컴퓨터에서만 연다 (127.0.0.1).

  python3 koa/web.py                 # http://127.0.0.1:8765
  python3 koa/web.py --port 9000

표준 라이브러리 + PyYAML 만 쓴다 (설치본에 따로 깔 것 없음). 하는 일은 파일 읽기·쓰기와
트리아지(koa/triage.py, 읽기 전용) 실행뿐이고 클러스터는 바꾸지 않는다.

  local/knowledge/<클러스터>/       flows.yaml · architecture.md · incidents/*.md  (knowledge.py 와 같은 파일)
  local/requests/<클러스터>/<시각>/  request.yaml(조사 계획) · triage.txt · report.md(KOA 가 분석 후 씀)
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
from urllib.parse import parse_qs, urlparse

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import knowledge as K  # noqa: E402
from paths import REPO, REQUESTS  # noqa: E402

HTML = Path(__file__).resolve().parent / "web" / "index.html"
ID_RE = re.compile(r"^\d{8}T\d{6}Z$")
FILE_RE = re.compile(r"^[0-9A-Za-z가-힣._-]{1,80}\.md$")
_lock = threading.Lock()
_REMOTE = False  # --host 0.0.0.0 이면 True (외부 접속 허용)


def req_dir(cluster, rid=None):
    K.cluster_dir(cluster)  # 이름 검사
    d = REQUESTS / cluster
    if rid is None:
        return d
    if not ID_RE.match(rid):
        raise ValueError("요청 id 형식이 아니다")
    return d / rid


def rel(p):
    try:
        return str(Path(p).relative_to(REPO))
    except ValueError:
        return str(p)


def chat_prompt(d):
    return "%s 의 조사 요청을 분석해줘. 결과는 같은 폴더의 report.md 에 써줘." % rel(d / "request.yaml")


# ---------- 지식 ----------

def get_knowledge(c):
    d = K.cluster_dir(c)
    if not d.is_dir():
        raise LookupError("지식이 없는 클러스터: %s" % c)
    data = K.load_flows(c)
    f = d / "flows.yaml"
    a = d / "architecture.md"
    return {
        "cluster": c,
        "flows": data,
        "edges": {n: K.edges(fl) for n, fl in (data.get("flows") or {}).items()},
        "raw": f.read_text() if f.is_file() else "",
        "architecture": a.read_text() if a.is_file() else "",
        "incidents": K.load_incidents(c),
        "check": [{"level": lv, "msg": m} for lv, m in K.check(c, data)],
        "path": rel(d),
    }


def save_flows(c, body):
    d = K.cluster_dir(c)
    if "raw" in body:
        text = body["raw"]
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            raise ValueError("flows.yaml 최상위는 매핑이어야 한다")
    else:
        data = body["data"]
        if not isinstance(data, dict):
            raise ValueError("data 형식 오류")
        data = {"schema": data.get("schema") or "koa.knowledge/v1", "cluster": c,
                "components": data.get("components") or {}, "flows": data.get("flows") or {},
                "normal": data.get("normal") or []}
        text = ("# KOA 클러스터 지식 — 구조 (분석 경로를 정한다). 설계: docs/koa-analysis-inputs.md\n"
                "# 웹 화면(koa/web.py)에서 저장함. 직접 고쳐도 된다.\n"
                + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=None, width=120))
    with _lock:
        f = d / "flows.yaml"
        if f.is_file():
            (d / "flows.yaml.bak").write_text(f.read_text())
        f.write_text(text)
    return [{"level": lv, "msg": m} for lv, m in K.check(c, data)]


def save_architecture(c, body):
    d = K.cluster_dir(c)
    with _lock:
        (d / "architecture.md").write_text(body.get("text") or "")


def save_incident(c, body):
    d = K.cluster_dir(c) / "incidents"
    d.mkdir(parents=True, exist_ok=True)
    meta = body.get("meta") or {}
    if not isinstance(meta, dict):
        raise ValueError("meta 형식 오류")
    meta = {k: v for k, v in meta.items() if v not in (None, "", [], {})}
    try:
        meta["date"] = dt.date.fromisoformat(str(meta.get("date")))  # yaml 에 따옴표 없는 날짜로
    except ValueError:
        pass
    name = body.get("file") or "%s-%s.md" % (meta.get("date") or dt.date.today().isoformat(), K.slug(meta.get("cause") or ""))
    if not FILE_RE.match(name):
        raise ValueError("파일 이름 형식 오류: %s" % name)
    with _lock:
        (d / name).write_text(K.incident_text(meta, body.get("body") or ""))
    return name


def delete_incident(c, name):
    if not FILE_RE.match(name or ""):
        raise ValueError("파일 이름 형식 오류")
    f = K.cluster_dir(c) / "incidents" / name
    if f.is_file():
        f.unlink()


# ---------- 클러스터 등록 도우미 ----------

def kubeconfig_context():
    """읽기 전용 kubeconfig 의 현재 컨텍스트에서 -readonly 를 뺀 이름 (트리아지가 쓰는 클러스터 이름)."""
    kc = Path(os.environ.get("KOA_KUBECONFIG") or Path.home() / ".kube" / "hermes-readonly.yaml").expanduser()
    try:
        ctx = (yaml.safe_load(kc.read_text()) or {}).get("current-context") or ""
    except (OSError, yaml.YAMLError):
        return None
    return re.sub(r"-readonly$", "", ctx) or None


def cluster_info():
    have = K.clusters()
    cands = []
    for n in K.discovered() + [kubeconfig_context()]:
        if n and K.NAME_RE.match(n) and n not in have and n not in cands:
            cands.append(n)
    return {"clusters": have, "candidates": cands, "context": kubeconfig_context()}


_wl_cache = {}


def workloads(cluster):
    """구성요소 워크로드 입력 도우미: 읽기 전용 kubeconfig 로 Deployment·StatefulSet·DaemonSet 목록 (get/list 만)."""
    K.cluster_dir(cluster)  # 이름 검사
    hit = _wl_cache.get(cluster)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    kc = Path(os.environ.get("KOA_KUBECONFIG") or Path.home() / ".kube" / "hermes-readonly.yaml").expanduser()
    prof = K.profile(cluster) or {}
    ctx = (prof.get("kubeconfig") or {}).get("context")
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


# ---------- 조사 요청 ----------

def run_triage(d, plan, source):
    cmd = [sys.executable, str(REPO / "koa" / "triage.py"), "--name", plan["cluster"], "--since", plan["since"], "--source", source]
    if plan.get("namespaces"):
        cmd += ["--ns", ",".join(plan["namespaces"])]
    (d / "status.json").write_text(json.dumps({"state": "running", "cmd": cmd[1:]}, ensure_ascii=False))
    try:
        p = subprocess.run(cmd, cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
        out, rc = p.stdout.decode("utf-8", "replace"), p.returncode
    except subprocess.TimeoutExpired:
        out, rc = "트리아지가 600초 안에 끝나지 않았다", -1
    (d / "triage.txt").write_text(out)
    (d / "status.json").write_text(json.dumps({"state": "done" if rc == 0 else "failed", "rc": rc, "cmd": cmd[1:]}, ensure_ascii=False))


def create_request(body):
    c = body.get("cluster") or ""
    q = (body.get("question") or "").strip()
    if not q:
        raise ValueError("질문이 비었다")
    has_k = K.cluster_dir(c).is_dir()
    plan = K.plan(c, q, (body.get("since") or "").strip() or None) if has_k else {
        "request": q, "cluster": c, "target": None, "symptom": K.find_symptom(q), "since": (body.get("since") or "").strip() or K.find_since(q) or "1h",
        "flows": [], "path": [], "namespaces": [], "known": [], "normal": [], "notes": ["클러스터 지식이 없다 → 트리아지만으로 본다"],
        "created": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    rid = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    d = req_dir(c, rid)
    with _lock:
        while d.exists():  # 같은 초에 두 번
            rid = (dt.datetime.strptime(rid, "%Y%m%dT%H%M%SZ") + dt.timedelta(seconds=1)).strftime("%Y%m%dT%H%M%SZ")
            d = req_dir(c, rid)
        d.mkdir(parents=True)
    (d / "request.yaml").write_text(
        "# KOA 조사 요청 (웹 화면). 분석: cluster-incident-analysis 스킬, 결과는 이 폴더 report.md\n"
        + yaml.safe_dump(plan, allow_unicode=True, sort_keys=False, width=120))
    if body.get("triage", True):
        source = body.get("source") if body.get("source") in ("mcp", "kubectl") else "mcp"
        threading.Thread(target=run_triage, args=(d, plan, source), daemon=True).start()
    return rid


def list_requests(cluster=None):
    out = []
    if not REQUESTS.is_dir():
        return out
    for cd in sorted(REQUESTS.iterdir()):
        if not cd.is_dir() or (cluster and cd.name != cluster):
            continue
        for d in cd.iterdir():
            if not (d.is_dir() and ID_RE.match(d.name) and (d / "request.yaml").is_file()):
                continue
            try:
                p = yaml.safe_load((d / "request.yaml").read_text()) or {}
            except yaml.YAMLError:
                p = {}
            st = json.loads((d / "status.json").read_text()) if (d / "status.json").is_file() else {"state": "none"}
            out.append({"cluster": cd.name, "id": d.name, "question": p.get("request", ""), "target": p.get("target"),
                        "symptom": p.get("symptom"), "triage": st.get("state"), "report": (d / "report.md").is_file()})
    out.sort(key=lambda r: r["id"], reverse=True)
    return out


def get_request(c, rid):
    d = req_dir(c, rid)
    if not (d / "request.yaml").is_file():
        raise LookupError("없는 요청")
    plan = yaml.safe_load((d / "request.yaml").read_text()) or {}
    read = lambda n: (d / n).read_text() if (d / n).is_file() else None  # noqa: E731
    st = json.loads(read("status.json")) if read("status.json") else {"state": "none"}
    return {"cluster": c, "id": rid, "plan": plan, "plan_text": K.plan_text(plan), "triage": st,
            "triage_text": read("triage.txt"), "report": read("report.md"), "prompt": chat_prompt(d), "path": rel(d)}


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "koa-web"

    def log_message(self, fmt, *args):  # 조용히: 오류만
        if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            sys.stderr.write("%s\n" % (fmt % args))

    def _host_ok(self):
        # 기본(로컬 전용)이면 DNS 리바인딩 막기: 이 컴퓨터 주소로 들어온 요청만.
        # --host 0.0.0.0 으로 열면 외부 접속을 허용하므로 Host 가드를 푼다 (인증 없음 — 사용자가 위험 인지하고 여는 경우).
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host in ("127.0.0.1", "localhost", "[::1]"):
            return True
        return _REMOTE

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
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        qs = parse_qs(u.query)
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
                return self._send(200, dict(cluster_info(), knowledge=rel(K.KNOWLEDGE), symptoms=K.SYMPTOM_NAMES))
            if method == "POST" and p == ["clusters"]:
                K.init(body.get("name") or "", bool(body.get("example")))
                return self._send(200, {"ok": True})
            if method == "GET" and len(p) == 2 and p[0] == "workloads":
                return self._send(200, workloads(p[1]))
            if p[:1] == ["knowledge"] and len(p) >= 2:
                c = p[1]
                if method == "GET" and len(p) == 2:
                    return self._send(200, get_knowledge(c))
                if method == "POST" and p[2:] == ["flows"]:
                    return self._send(200, {"check": save_flows(c, body)})
                if method == "POST" and p[2:] == ["architecture"]:
                    save_architecture(c, body)
                    return self._send(200, {"ok": True})
                if method == "POST" and p[2:] == ["incidents"]:
                    return self._send(200, {"file": save_incident(c, body)})
                if method == "POST" and p[2:3] == ["incidents"] and p[4:] == ["delete"]:
                    delete_incident(c, p[3])
                    return self._send(200, {"ok": True})
            if method == "POST" and p == ["plan"]:
                c = body.get("cluster") or ""
                q = (body.get("question") or "").strip()
                if not q or not K.cluster_dir(c).is_dir():
                    return self._send(200, {"plan": None})
                pl = K.plan(c, q, (body.get("since") or "").strip() or None)
                return self._send(200, {"plan": pl, "text": K.plan_text(pl)})
            if p == ["requests"]:
                if method == "GET":
                    return self._send(200, {"requests": list_requests((qs.get("cluster") or [None])[0])})
                return self._send(200, {"id": create_request(body)})
            if method == "GET" and len(p) == 3 and p[0] == "requests":
                return self._send(200, get_request(p[1], p[2]))
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
                    help="바인딩 주소 (기본 127.0.0.1 로컬 전용). 0.0.0.0 으로 열면 외부 접속 허용 — 인증이 없어 위험, X-KOA 헤더 가드만 남는다")
    a = ap.parse_args()
    if a.host != "127.0.0.1":
        _REMOTE = True
        print("⚠ 경고: %s 으로 열어 외부 접속을 허용했습니다. 인증이 없어 사내망 누구나 지식/쓰기 가능." % a.host)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print("KOA 웹: http://%s:%d  (지식 %s · 요청 %s)  Ctrl+C 로 끝" % (a.host, a.port, rel(K.KNOWLEDGE), rel(REQUESTS)))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
