#!/usr/bin/env python3
"""KOA 클러스터 지식 — 읽기·검사·조사 계획. 설계: docs/koa-analysis-inputs.md

  python3 koa/knowledge.py list                               # 지식이 있는 클러스터
  python3 koa/knowledge.py init <클러스터> [--example]        # local/knowledge/<클러스터>/ 만들기 (탐색한 큐·캐시·ingress 를 미리 채움)
  python3 koa/knowledge.py check <클러스터>                   # flows.yaml 의 빈 곳·틀린 곳
  python3 koa/knowledge.py plan <클러스터> "rmq 소비가 안 돼"   # 질문 → 조사 계획 (yaml)

웹 화면(koa/web.py)도 이 모듈을 그대로 쓴다. 클러스터는 건드리지 않는다 (파일만 읽고 쓴다).
"""
import datetime as dt
import re
import shutil
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import CLUSTERS, KNOWLEDGE, KNOWLEDGE_TEMPLATE  # noqa: E402

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
ROLES = ("produce", "consume", "call")

# 증상 유형 → 질문에 나오면 그 유형으로 보는 말 (앞에 있을수록 먼저)
SYMPTOMS = [
    ("queue-backlog", ["소비", "컨슘", "consum", "쌓", "적체", "backlog", "lag", "밀려", "안 빠", "안빠", "unacked"]),
    ("log-pipeline", ["로그가 안", "로그 안", "로그가 없", "로그 수집", "fluent", "opensearch", "kibana", "로그 누락"]),
    ("pending", ["pending", "펜딩", "스케줄", "schedul", "안 떠", "안떠", "생성이 안"]),
    ("crash", ["crash", "크래시", "재시작", "restart", "죽", "oom", "꺼져", "내려"]),
    ("errors", ["5xx", "500", "502", "503", "504", "에러", "오류", "error", "실패", "fail", "exception"]),
    ("latency", ["느려", "느림", "지연", "타임아웃", "timeout", "latency", "slow", "응답이 늦", "오래 걸"]),
    ("external-dependency", ["외부", "db 연결", "디비", "연결이 안", "connection refused", "connect"]),
]
SYMPTOM_NAMES = [s for s, _ in SYMPTOMS]


# ---------- 읽기 ----------

def cluster_dir(cluster):
    if not NAME_RE.match(cluster or ""):
        raise ValueError("클러스터 이름은 영문·숫자·_.- 만: %r" % cluster)
    return KNOWLEDGE / cluster


def clusters():
    if not KNOWLEDGE.is_dir():
        return []
    return sorted(p.name for p in KNOWLEDGE.iterdir() if p.is_dir() and NAME_RE.match(p.name))


# 탐색(discover.py)에서 찾은 구성요소 중 앱 흐름에 들어가는 종류 → 지식 구성요소로 미리 채운다
APP_KINDS = ("message-queue", "cache", "ingress", "database")


def discovered():
    """discover.py 가 만든 클러스터 프로필 이름 (<결과>/<이름>.yaml)."""
    if not CLUSTERS.is_dir():
        return []
    return sorted(p.stem for p in CLUSTERS.glob("*.yaml") if NAME_RE.match(p.stem) and not p.stem.endswith(".mcp"))


def profile(cluster):
    f = CLUSTERS / ("%s.yaml" % cluster)
    if not f.is_file():
        return None
    try:
        return yaml.safe_load(f.read_text()) or {}
    except yaml.YAMLError:
        return None


def seed_components(prof):
    """클러스터 프로필의 메시지 큐·캐시·ingress 를 구성요소로."""
    comps = (prof or {}).get("components") or {}
    # 관측·GitOps 도구가 사는 네임스페이스의 것(예: argocd-redis)은 앱 흐름이 아니다
    infra_ns = {i.get("namespace") for c in comps.values() if (c or {}).get("kind") not in APP_KINDS
                for i in ((c or {}).get("instances") or [])}
    out = {}
    for name, c in comps.items():
        c = c or {}
        if c.get("kind") not in APP_KINDS:
            continue
        insts = [i for i in (c.get("instances") or [])
                 if i.get("namespace") and "/" in (i.get("workload") or "") and i["namespace"] not in infra_ns]
        for n, inst in enumerate(insts):
            key = name if n == 0 else "%s-%d" % (name, n + 1)
            out[key] = {"workload": "%s/%s" % (inst["namespace"], inst["workload"]), "kind": c["kind"]}
    return out


def init(cluster, example=False):
    """지식 폴더를 만든다. 기본은 빈 구조 + 클러스터 프로필에서 찾은 큐·캐시·ingress.
    example=True 면 템플릿 예시(order-api 등)를 그대로 — 연습용. 실제 클러스터에 쓰면 예시가 분석 경로에 섞인다."""
    d = cluster_dir(cluster)
    if d.exists():
        raise ValueError("이미 있다: %s" % d)
    shutil.copytree(str(KNOWLEDGE_TEMPLATE), str(d), ignore=shutil.ignore_patterns("README.md"))
    f = d / "flows.yaml"
    if example:
        f.write_text(f.read_text().replace("cluster: <클러스터>", "cluster: %s" % cluster))
    else:
        for x in (d / "incidents").glob("*.md"):
            x.unlink()
        data = {"schema": "koa.knowledge/v1", "cluster": cluster,
                "components": seed_components(profile(cluster)), "flows": {}, "normal": []}
        f.write_text("# KOA 클러스터 지식 — 구조 (분석 경로를 정한다). 설계: docs/koa-analysis-inputs.md\n"
                     "# 예시: koa/templates/knowledge/flows.yaml\n"
                     + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=None, width=120))
    a = d / "architecture.md"
    a.write_text(a.read_text().replace("<클러스터>", cluster, 1))
    return d


def load_flows(cluster):
    f = cluster_dir(cluster) / "flows.yaml"
    if not f.is_file():
        return {"schema": "koa.knowledge/v1", "cluster": cluster, "components": {}, "flows": {}, "normal": []}
    data = yaml.safe_load(f.read_text()) or {}
    data.setdefault("components", {})
    data.setdefault("flows", {})
    data.setdefault("normal", [])
    for k in ("components", "flows"):
        if data[k] is None:
            data[k] = {}
    if data["normal"] is None:
        data["normal"] = []
    return data


def edges(flow):
    """flow 의 hops 를 {from, to, role, via} 목록으로. 문자열 목록은 동기 호출 체인(call)."""
    hops = (flow or {}).get("hops") or []
    out = []
    if hops and all(isinstance(h, str) for h in hops):
        for a, b in zip(hops, hops[1:]):
            out.append({"from": a, "to": b, "role": "call", "via": ""})
        if len(hops) == 1:
            out.append({"from": hops[0], "to": "", "role": "call", "via": ""})
        return out
    for h in hops:
        if isinstance(h, dict):
            out.append({"from": str(h.get("from") or ""), "to": str(h.get("to") or ""),
                        "role": str(h.get("role") or "call"), "via": str(h.get("via") or "")})
        elif isinstance(h, str):
            out.append({"from": h, "to": "", "role": "call", "via": ""})
    return out


def flow_nodes(flow):
    seen = []
    for e in edges(flow):
        for n in (e["from"], e["to"]):
            if n and n not in seen:
                seen.append(n)
    return seen


def parse_front(text):
    """'---\\n<yaml>\\n---\\n본문' → (dict, 본문)."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), m.group(2)


def load_incidents(cluster):
    d = cluster_dir(cluster) / "incidents"
    out = []
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.md"), reverse=True):
        meta, body = parse_front(f.read_text())
        out.append({"file": f.name, "meta": meta, "body": body.strip()})
    return out


def incident_text(meta, body):
    head = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=None).strip()
    return "---\n%s\n---\n%s\n" % (head, (body or "").strip())


def slug(text):
    s = re.sub(r"[^0-9A-Za-z가-힣]+", "-", text or "").strip("-").lower()
    return s[:40] or "incident"


# ---------- 검사 ----------

def check(cluster, data=None):
    """flows.yaml 의 문제 목록 [(level, 문구)]. level: error | warn."""
    data = data if data is not None else load_flows(cluster)
    comps = data.get("components") or {}
    out = []
    alias_owner = {}
    for name, c in comps.items():
        c = c or {}
        if not c.get("external") and not c.get("workload"):
            out.append(("warn", "%s: workload 가 비었다 (클러스터 밖이면 external: true)" % name))
        w = c.get("workload")
        if w and not re.match(r"^[^/\s]+/[A-Za-z]+/[^/\s]+$", str(w)):
            out.append(("error", "%s: workload 는 <ns>/<Kind>/<이름> 형식이어야 한다 (지금 %r)" % (name, w)))
        for a in [name] + list(c.get("aliases") or []):
            k = str(a).lower()
            if k in alias_owner and alias_owner[k] != name:
                out.append(("warn", "'%s' 가 %s 와 %s 둘 다의 이름이다 (질문에서 대상을 못 가린다)" % (a, alias_owner[k], name)))
            alias_owner[k] = name
    for fname, f in (data.get("flows") or {}).items():
        es = edges(f)
        if not es:
            out.append(("warn", "흐름 %s: hop 이 없다" % fname))
        for e in es:
            for n in (e["from"], e["to"]):
                if n and n not in comps:
                    out.append(("warn", "흐름 %s: '%s' 가 구성요소에 없다 (경로에는 나오지만 워크로드를 모른다)" % (fname, n)))
            if e["role"] not in ROLES:
                out.append(("error", "흐름 %s: role '%s' 는 produce | consume | call 중 하나" % (fname, e["role"])))
    for n in data.get("normal") or []:
        if isinstance(n, dict) and n.get("component") and n["component"] not in comps:
            out.append(("warn", "정상 패턴: '%s' 가 구성요소에 없다" % n["component"]))
    # 중복 제거, 순서 유지
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


# ---------- 조사 계획 ----------

def find_target(question, comps):
    q = question.lower()
    best, best_len = None, 0
    for name, c in comps.items():
        for a in [name] + list((c or {}).get("aliases") or []):
            a = str(a).lower().strip()
            if a and a in q and len(a) > best_len:
                best, best_len = name, len(a)
    return best


def find_symptom(question):
    q = question.lower()
    for name, words in SYMPTOMS:
        if any(w in q for w in words):
            return name
    return None


def find_since(question):
    m = re.search(r"(\d+)\s*(분|시간|일|m\b|min|h\b|d\b)", question.lower())
    if not m:
        return None
    n, u = int(m.group(1)), m.group(2)
    return "%d%s" % (n, {"분": "m", "min": "m", "m": "m", "시간": "h", "h": "h", "일": "d", "d": "d"}[u])


def order_path(target, symptom, data):
    """대상을 지나는 흐름에서 확인 순서대로 [(구성요소, 역할 설명)]."""
    comps = data.get("components") or {}
    flows = data.get("flows") or {}
    hit = {n: f for n, f in flows.items() if target in flow_nodes(f)}
    path = []

    def add(name, why):
        if name and name not in [p[0] for p in path]:
            path.append((name, why))

    if symptom == "queue-backlog":
        for f in hit.values():
            es = edges(f)
            brokers = {e["to"] for e in es if e["role"] == "produce"} | {e["from"] for e in es if e["role"] == "consume"}
            consumers = [e["to"] for e in es if e["role"] == "consume"]
            producers = [e["from"] for e in es if e["role"] == "produce"]
            for c in consumers:
                add(c, "consume — 소비가 안 됨 → 소비자부터")
            for b in sorted(brokers):
                add(b, "broker")
            for e in es:
                if e["from"] in consumers and e["role"] == "call":
                    add(e["to"], "소비자의 하류 — 느리면 소비자가 멈춘다")
            for p in producers:
                add(p, "produce — 유입·메시지 형식 변화")
    elif symptom in ("crash", "pending"):
        # 대상 자체가 먼저, 그다음 대상이 부르는 하류 (의존성이 죽어서 죽는 경우), 나머지
        add(target, "대상 — 상태·재시작·이벤트")
        for f in hit.values():
            for e in edges(f):
                if e["from"] == target and e["to"]:
                    add(e["to"], "대상의 하류 — 의존성 실패로 죽는지")
    else:
        for fname, f in hit.items():
            for n in flow_nodes(f):
                role = ""
                for e in edges(f):
                    if e["to"] == n and e["role"] != "call":
                        role = e["role"]
                add(n, ("%s · %s" % (fname, role)) if role else fname)
    add(target, "대상")
    # 대상이 흐름 밖이면 경로는 대상 하나
    return path, sorted(hit)


def namespaces_for(names, comps):
    out = []
    for n in names:
        w = str((comps.get(n) or {}).get("workload") or "")
        if "/" in w:
            ns = w.split("/", 1)[0]
            if ns not in out:
                out.append(ns)
    return out


def plan(cluster, question, since=None, data=None, incidents=None):
    data = data if data is not None else load_flows(cluster)
    incidents = incidents if incidents is not None else load_incidents(cluster)
    comps = data.get("components") or {}
    target = find_target(question, comps)
    symptom = find_symptom(question)
    notes = []
    if not target:
        notes.append("질문에서 구성요소를 못 찾았다 → 트리아지 상위 이상 징후를 대상으로 본다 (구성요소 aliases 에 질문 속 단어를 넣으면 다음부터 찾는다)")
    if not symptom:
        notes.append("증상 유형을 못 정했다 → 트리아지 결과로 정한다")
    path, flows_hit = order_path(target, symptom, data) if target else ([], [])
    names = [p[0] for p in path]
    known = []
    for inc in incidents:
        m = inc["meta"]
        ic = [str(x) for x in (m.get("components") or [])]
        score = (2 if symptom and m.get("symptom") == symptom else 0) + (2 if target in ic else 0) + len(set(ic) & set(names))
        if score >= 2:  # 유형이나 대상이 겹치거나, 경로 위 구성요소가 둘 이상 겹칠 때만
            sig = (m.get("signature") or {}).get("logs") if isinstance(m.get("signature"), dict) else None
            known.append({"file": inc["file"], "score": score, "date": str(m.get("date") or ""),
                          "cause": m.get("cause") or "", "signature": sig or []})
    known.sort(key=lambda k: -k["score"])
    normal = [n for n in (data.get("normal") or []) if isinstance(n, dict) and n.get("component") in names]
    ext = [n for n in names if (comps.get(n) or {}).get("external")]
    for n in ext:
        sig = (comps.get(n) or {}).get("log_signatures") or []
        notes.append("%s 는 클러스터 밖: 호출하는 쪽 로그에서 %s 로 판정" % (n, ", ".join('"%s"' % s for s in sig) or "오류 문구"))
    return {
        "request": question,
        "cluster": cluster,
        "target": target,
        "symptom": symptom,
        "since": since or find_since(question) or "1h",
        "flows": flows_hit,
        "path": [{"component": n, "why": why, "workload": (comps.get(n) or {}).get("workload") or ("클러스터 밖" if (comps.get(n) or {}).get("external") else "?")} for n, why in path],
        "namespaces": namespaces_for(names, comps),
        "known": known[:5],
        "normal": normal,
        "notes": notes,
        "created": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def plan_text(p):
    lines = ["요청: %s" % p["request"],
             "대상 %s · 유형 %s · 최근 %s%s" % (p["target"] or "?", p["symptom"] or "?", p["since"],
                                           (" · 흐름 %s" % ", ".join(p["flows"])) if p["flows"] else "")]
    if p["path"]:
        lines.append("확인 순서:")
        for i, h in enumerate(p["path"], 1):
            lines.append("  %d. %-16s %-34s %s" % (i, h["component"], h["workload"], h["why"]))
    for k in p["known"]:
        lines.append("과거 이슈: %s %s%s" % (k["file"], k["cause"], (" (먼저 볼 로그: %s)" % ", ".join(k["signature"])) if k["signature"] else ""))
    for n in p["normal"]:
        lines.append("정상 패턴: %s — %s" % (n.get("component"), n.get("note")))
    for n in p["notes"]:
        lines.append("참고: %s" % n)
    return "\n".join(lines)


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list")
    ip = sub.add_parser("init")
    ip.add_argument("cluster")
    ip.add_argument("--example", action="store_true", help="템플릿 예시를 채워서 (연습용)")
    sub.add_parser("check").add_argument("cluster")
    pp = sub.add_parser("plan")
    pp.add_argument("cluster")
    pp.add_argument("question")
    pp.add_argument("--since", default=None)
    pp.add_argument("--yaml", action="store_true", help="계획을 yaml 로")
    a = ap.parse_args()
    try:
        if a.cmd == "list":
            print("\n".join(clusters()) or "(없음) — init <클러스터> 로 만든다")
        elif a.cmd == "init":
            print("만들었다: %s" % init(a.cluster, a.example))
        elif a.cmd == "check":
            probs = check(a.cluster)
            for lv, msg in probs:
                print("%s %s" % ("❌" if lv == "error" else "⚠", msg))
            if not probs:
                print("✅ 문제 없음")
            sys.exit(1 if any(lv == "error" for lv, _ in probs) else 0)
        elif a.cmd == "plan":
            p = plan(a.cluster, a.question, a.since)
            print(yaml.safe_dump(p, allow_unicode=True, sort_keys=False) if a.yaml else plan_text(p))
        else:
            ap.print_help()
    except ValueError as x:
        sys.exit(str(x))


if __name__ == "__main__":
    main()
