#!/usr/bin/env python3
"""KOA 클러스터 지식 — 읽기 · 꺼내기 · 더하기 · 제안.

분석은 사용자가 말한 구성요소·흐름에 해당하는 지식만 쓴다. 분석하다 알게 된 것은 사용자가 OK 하면 더하고,
도구(flowmap)가 찾은 것은 '제안'으로 남겨 두었다가 사용자가 고른 것만 더한다.

  python3 koa/knowledge.py list                                   # 지식이 있는 클러스터
  python3 koa/knowledge.py init <클러스터>                         # local/knowledge/<클러스터>/ 만들기
  python3 koa/knowledge.py check <클러스터>                        # 빈 곳·틀린 곳
  python3 koa/knowledge.py context <클러스터> "rmq 소비가 안 돼" [--with a,b]
                                                                # 질문(또는 지정한 이름)에 해당하는 지식만 꺼낸다
  python3 koa/knowledge.py add <클러스터> '<json>' [--dry-run]     # 지식에 더한다 (지우거나 덮지 않음)
  python3 koa/knowledge.py incident <클러스터> '<json>'            # 과거 이슈 1건 기록
  python3 koa/knowledge.py suggest <클러스터> '<json>' --title "…" [--source 분석]   # 제안으로만 남긴다
  python3 koa/knowledge.py suggestions <클러스터>                  # 남아 있는 제안
  python3 koa/knowledge.py accept|dismiss <클러스터> <id>          # 제안을 지식에 더하기 / 버리기

<json> 모양 (전부 선택):
  {"components": {"order-api": {"workload": "shop/Deployment/order-api", "kind": "app", "group": "주문",
                                "aliases": ["주문 API"], "log_signatures": ["Too many connections"]}},
   "flows": [{"name": "주문", "nodes": ["ingress-nginx", "order-api", "orders-db"]}],
   "normal": [{"component": "order-worker", "note": "매일 02:00 배치 때 큐가 쌓였다 빠진다"}]}
incident: {"date": "2026-10-11", "components": ["rabbitmq"], "logs": ["disk resource limit alarm set"],
           "cause": "PVC 90% → 디스크 알람", "body": "자유 서술"}

웹 화면(koa/web.py)도 이 모듈을 그대로 쓴다. 클러스터는 건드리지 않는다 (파일만 읽고 쓴다).
"""
import datetime as dt
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import CLUSTERS, KNOWLEDGE, KNOWLEDGE_TEMPLATE  # noqa: E402

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
ROLES = ("produce", "consume", "call")
QUEUE_WORDS = re.compile(r"rabbit|rmq|kafka|sqs|nats|pulsar|queue|큐|^mq$|-mq$|activemq|redis-stream", re.I)
QUOTED = re.compile(r"[\"“”'‘’`]([^\"“”'‘’`]{4,120})[\"“”'‘’`]")
LIST_FIELDS = ("aliases", "log_signatures")
SCALAR_FIELDS = ("workload", "kind", "tier", "external", "group")
HEADER = "# KOA 클러스터 지식 — 구성요소 · 흐름 · 정상 패턴. 웹 화면이나 knowledge.py add 로 고친다.\n"


# ---------- 읽기 ----------

def cluster_dir(cluster):
    if not NAME_RE.match(cluster or ""):
        raise ValueError("클러스터 이름은 영문·숫자·_.- 만: %r" % cluster)
    return KNOWLEDGE / cluster


def clusters():
    if not KNOWLEDGE.is_dir():
        return []
    return sorted(p.name for p in KNOWLEDGE.iterdir() if p.is_dir() and NAME_RE.match(p.name))


# 탐색(discover.py)에서 찾은 구성요소 중 앱 흐름에 들어가는 종류 → 웹에서 고를 후보
APP_KINDS = ("message-queue", "cache", "ingress", "gateway", "database", "app")


def discovered():
    """discover.py 가 만든 클러스터 프로필 이름 (<결과>/<이름>.yaml)."""
    if not CLUSTERS.is_dir():
        return []
    return sorted(p.stem for p in CLUSTERS.glob("*.yaml") if NAME_RE.match(p.stem) and not p.stem.endswith(".mcp"))


def profile(cluster):
    # discover 프로필은 컨텍스트 이름(dev2-admin@dev2-kr-west1)으로 저장될 수 있다 → 클러스터명으로도 찾는다
    cands = [CLUSTERS / ("%s.yaml" % cluster)]
    if CLUSTERS.is_dir():
        cands += sorted(CLUSTERS.glob("*@%s.yaml" % cluster))
    for f in cands:
        if not f.is_file():
            continue
        try:
            return yaml.safe_load(f.read_text()) or {}
        except yaml.YAMLError:
            return None
    return None


def discover_candidates(cluster):
    """discover 프로필에서 구성요소로 추가할 수 있는 후보. 이미 구성요소에 있는 워크로드는 뺀다."""
    prof = profile(cluster)
    if not prof:
        return []
    have_wl = {w for c in (load_flows(cluster).get("components") or {}).values() for w in comp_workloads(c)}
    out, seen, names = [], set(), {}
    for cname, c in ((prof or {}).get("components") or {}).items():
        c = c or {}
        if c.get("kind") not in APP_KINDS:
            continue
        for inst in (c.get("instances") or []):
            ns, w = inst.get("namespace"), inst.get("workload") or ""
            if not ns or w.count("/") != 1:
                continue
            full = "%s/%s" % (ns, w)
            if full in have_wl or full in seen:
                continue
            seen.add(full)
            n = w.rsplit("/", 1)[-1] or cname
            names[n] = names.get(n, 0) + 1
            out.append({"name": n if names[n] == 1 else "%s-%d" % (n, names[n]), "workload": full,
                        "kind": c["kind"], "namespace": ns})
    return sorted(out, key=lambda x: (x["kind"], x["namespace"], x["name"]))


def add_discovered_components(cluster, workloads):
    """웹 '클러스터에서 고르기': 고른 워크로드('ns/Kind/name')를 구성요소로 더한다. 반환: (더한 이름, check)."""
    kind_of = {}
    for cname, c in ((profile(cluster) or {}).get("components") or {}).items():
        for inst in (c or {}).get("instances") or []:
            ns, w = inst.get("namespace"), inst.get("workload") or ""
            if ns and w.count("/") == 1:
                kind_of["%s/%s" % (ns, w)] = (c or {}).get("kind") or cname
    comps = {}
    for wl in workloads:
        wl = str(wl).strip()
        if wl:
            comps[wl.rsplit("/", 1)[-1]] = {"workload": wl, "kind": kind_of.get(wl) or "app"}
    r = apply_additions(cluster, {"components": comps})
    return r["components"], r["check"]


def _version(raw):
    return hashlib.sha1(raw).hexdigest()[:12] if raw else ""


def version(cluster):
    """flows.yaml 내용의 짧은 해시 ('' = 파일 없음). 웹이 읽은 뒤 그사이 다른 곳(KOA 채팅 등)에서 바뀌었는지 본다."""
    f = cluster_dir(cluster) / "flows.yaml"
    return _version(f.read_bytes() if f.is_file() else b"")


def load_flows(cluster, notes=None, meta=None):
    """meta 를 주면 meta["version"] 에 읽은 내용의 version() 을 넣는다 (읽은 바이트 그대로로 — 따로 다시 읽으면 그사이 바뀔 수 있다)."""
    f = cluster_dir(cluster) / "flows.yaml"
    raw = f.read_bytes() if f.is_file() else b""
    data = (yaml.safe_load(raw) or {}) if raw else {}
    data.setdefault("schema", "koa.knowledge/v1")
    data.setdefault("cluster", cluster)
    for k in ("components", "flows"):
        if not isinstance(data.get(k), dict):
            data[k] = {}
    if not isinstance(data.get("normal"), list):
        data["normal"] = []
    moved = migrate(data)
    if moved and f.is_file():
        bak = f.parent / "flows.yaml.pre-names.bak"
        if not bak.exists():
            bak.write_bytes(raw)
        _write_flows(cluster, data)
        raw = f.read_bytes()
        if notes is not None:
            notes.extend(moved)
    if meta is not None:
        meta["version"] = _version(raw)
    return data


def comp_workloads(c):
    """구성요소의 워크로드 목록. workload 는 문자열 하나 또는 목록 (예: 게이트웨이 여러 개를 묶은 구성요소)."""
    w = (c or {}).get("workload")
    if isinstance(w, list):
        return [str(x) for x in w if x]
    return [str(w)] if w else []


def node_to_comps(node, comps):
    """흐름 칸이 가리키는 구성요소: 칸 = 구성요소 이름, 또는 그룹 이름 (그 그룹의 구성요소 전부). 정확히 같을 때만.
    웹 화면도 같은 규칙."""
    if not node:
        return []
    if node in (comps or {}):
        return [node]
    return groups(comps).get(node, [])


def name_to_comps(name, comps):
    """사람이 부른 이름 → 구성요소. 이름이나 별명이 (대소문자 무시) 같은 것."""
    if not name:
        return []
    if name in (comps or {}):
        return [name]
    nl = str(name).lower().strip()
    return [n for n, c in (comps or {}).items()
            if nl in [str(a).lower().strip() for a in [n] + list((c or {}).get("aliases") or [])]]


def similar_comps(node, comps):
    """이름이 아닌 노드에 대한 힌트: 별명이 같거나 이름이 부분 문자열로 겹치는 구성요소."""
    hit = name_to_comps(node, comps)
    if hit:
        return hit
    nl = str(node or "").lower()
    return [n for n in (comps or {}) if len(n) >= 3 and len(nl) >= 3 and (n.lower() in nl or nl in n.lower())]


def migrate(data):
    """예전 지식(흐름 노드가 flow_nodes·별명·대소문자 다른 이름으로 구성요소를 가리킴)을 '노드 = 구성요소 이름' 으로.
    - 노드 하나가 구성요소 하나를 가리키면 → 노드를 그 이름으로 바꾼다
    - 여러 구성요소가 같은 flow_nodes 를 공유하면 (예: Gateway) → 그 이름의 구성요소를 만들고 워크로드를 모은다
    - flow_nodes 의 나머지 이름은 aliases 로 옮긴다 (질문에서 부르는 말로 남게)
    data 를 그 자리에서 고친다. 반환: 바꾼 것 설명 목록 (없으면 [])."""
    comps = data.get("components") or {}
    flows = data.get("flows") or {}
    moved, rename = [], {}
    shared = {}
    for n, c in comps.items():
        for x in (c or {}).get("flow_nodes") or []:
            shared.setdefault(str(x).strip(), []).append(n)
    lower = {n.lower(): n for n in comps}
    for n, c in comps.items():                       # 0.2.10 의 theme → group
        if isinstance(c, dict) and "theme" in c:
            t = c.pop("theme")
            if t and not c.get("group"):
                c["group"] = t
            moved.append("구성요소 %s: theme → group" % n)

    def target(node):
        if node in comps or node in groups(comps):
            return None
        if node in rename:
            return rename[node]
        owners = shared.get(node) or []
        if len(owners) == 1:
            t = owners[0]
        elif len(owners) > 1:
            if all(not (comps[o] or {}).get("group") or comps[o]["group"] == node for o in owners):
                for o in owners:
                    comps[o]["group"] = node
                moved.append("그룹 %s 로 묶었다 (%s)" % (node, ", ".join(owners)))
            elif node not in comps:
                ws = []
                for o in owners:
                    ws += [w for w in comp_workloads(comps[o]) if w not in ws]
                kinds = {(comps[o] or {}).get("kind") for o in owners}
                g = {"workload": ws} if ws else {"external": True}
                if len(kinds) == 1 and None not in kinds:
                    g["kind"] = kinds.pop()
                comps[node] = g
                moved.append("구성요소 %s 를 만들었다 (%s 를 묶음)" % (node, ", ".join(owners)))
            return None
        elif node.lower() in lower:
            t = lower[node.lower()]
        else:
            hit = name_to_comps(node, comps)
            t = hit[0] if len(hit) == 1 else None
        if t:
            rename[node] = t
        return t

    for fname, f in flows.items():
        hops = (f or {}).get("hops") or []
        new = []
        for h in hops:
            if isinstance(h, dict):
                h = dict(h)
                for k in ("from", "to"):
                    t = target(str(h.get(k) or ""))
                    if t:
                        h[k] = t
            elif isinstance(h, str):
                h = target(h) or h
            new.append(h)
        if new != hops:
            f["hops"] = new
    for x in data.get("normal") or []:
        if isinstance(x, dict) and x.get("component"):
            t = target(str(x["component"]))
            if t:
                x["component"] = t
    for old, new in rename.items():
        moved.append("흐름 노드 '%s' → 구성요소 %s" % (old, new))
    owner = {}
    for n, c in comps.items():
        for a in [n] + list((c or {}).get("aliases") or []):
            owner.setdefault(str(a).lower(), n)
    for n, c in comps.items():
        fn = (c or {}).pop("flow_nodes", None) if isinstance(c, dict) else None
        if fn is None:
            continue
        keep = []
        for x in fn:
            x = str(x).strip()
            if not x or x in comps or len(shared.get(x) or []) > 1 or owner.get(x.lower(), n) != n:
                continue
            if x not in (c.get("aliases") or []) and x != n:
                keep.append(x)
                owner[x.lower()] = n
        if keep:
            c["aliases"] = list(c.get("aliases") or []) + keep
        moved.append("구성요소 %s: flow_nodes 를 없앴다%s" % (n, (" (별명으로: %s)" % ", ".join(keep)) if keep else ""))
    return moved


def edges(flow):
    """flow 의 hops 를 {from, to, role} 목록으로. 문자열 목록은 동기 호출 체인(call)."""
    hops = (flow or {}).get("hops") or []
    out = []
    if hops and all(isinstance(h, str) for h in hops):
        for a, b in zip(hops, hops[1:]):
            out.append({"from": a, "to": b, "role": "call"})
        return out
    for h in hops:
        if isinstance(h, dict):
            out.append({"from": str(h.get("from") or ""), "to": str(h.get("to") or ""),
                        "role": str(h.get("role") or "call")})
    return out


def flow_text(flow):
    """사람·모델이 읽을 흐름 한 줄. 곧은 사슬이면 'a → b → c', 갈래가 있으면 'a → b → c; b → d'.
    (flow_nodes() 는 노드를 한 줄로 펴서 갈래가 있으면 'b → d → c' 처럼 틀리게 읽힌다)"""
    es = edges(flow)
    if not es:
        return " → ".join(flow_nodes(flow))
    chains = []
    for e in es:
        for c in chains:
            if c[-1] == e["from"]:
                c.append(e["to"])
                break
        else:
            chains.append([e["from"], e["to"]])
    return "; ".join(" → ".join(c) for c in chains)


def flow_nodes(flow):
    seen = []
    for e in edges(flow):
        for n in (e["from"], e["to"]):
            if n and n not in seen:
                seen.append(n)
    if not seen:
        seen = [h for h in ((flow or {}).get("hops") or []) if isinstance(h, str)]
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


def incident_logs(meta):
    sig = meta.get("signature")
    return list((sig or {}).get("logs") or []) if isinstance(sig, dict) else []


# ---------- 쓰기 ----------

def _write_flows(cluster, data):
    d = cluster_dir(cluster)
    d.mkdir(parents=True, exist_ok=True)
    f = d / "flows.yaml"
    if f.is_file():
        (d / "flows.yaml.bak").write_text(f.read_text())
    f.write_text(HEADER + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=None, width=120))


def _hops(nodes, comps, kind=None):
    queue = [n for n in nodes if QUEUE_WORDS.search(n) or (comps.get(n) or {}).get("kind") == "message-queue"]
    if not queue:
        return list(nodes), kind or "http"
    return ([{"from": a, "to": b, "role": "produce" if b in queue else "consume" if a in queue else "call"}
             for a, b in zip(nodes, nodes[1:])], kind or "queue")


def _clean_comp(c):
    """구성요소에서 지식 칸만, 빈 값은 뺀다. workload 는 하나면 문자열, 여럿이면 목록."""
    comp = {}
    for k in SCALAR_FIELDS + LIST_FIELDS:
        v = (c or {}).get(k)
        if v in (None, "", []) or (k in LIST_FIELDS and not isinstance(v, list)):
            continue
        if k == "workload" and isinstance(v, list):
            v = [str(x).strip() for x in v if str(x).strip()]
            v = v[0] if len(v) == 1 else v or None
            if not v:
                continue
        comp[k] = v
    return comp


def _norm(sdata):
    """웹 화면 상태(흐름이 목록) 또는 load_flows 결과(흐름이 {이름: {kind, hops}})를 한 모양으로:
    {"components": {이름: 칸}, "flows": {이름: {"kind", "nodes"}}, "normal": [{component, note}]}.
    저장(save_shards)과 합치기(merge3)가 같은 규칙으로 보게 한다."""
    sdata = sdata if isinstance(sdata, dict) else {}
    cin = sdata.get("components") if isinstance(sdata.get("components"), dict) else {}
    comps = {str(n): _clean_comp(c) for n, c in cin.items() if n}
    fin = sdata.get("flows") or []
    if isinstance(fin, dict):
        fin = [{"name": n, "kind": (f or {}).get("kind"), "nodes": flow_nodes(f)} for n, f in fin.items()]
    flows = {}
    for fl in fin if isinstance(fin, list) else []:
        fl = fl if isinstance(fl, dict) else {}
        name = str(fl.get("name") or "").strip()
        nodes = [str(x).strip() for x in (fl.get("nodes") or []) if x and str(x).strip()]
        if name and len(nodes) >= 2:
            flows[name] = {"kind": fl.get("kind") or _hops(nodes, comps)[1], "nodes": nodes}
    normal = []
    for x in sdata.get("normal") if isinstance(sdata.get("normal"), list) else []:
        x = x if isinstance(x, dict) else {}
        c, note = str(x.get("component") or "").strip(), str(x.get("note") or "").strip()
        if c and note and {"component": c, "note": note} not in normal:
            normal.append({"component": c, "note": note})
    return {"components": comps, "flows": flows, "normal": normal}


class Conflict(ValueError):
    """저장하는 사이 다른 곳(웹 화면, KOA 채팅의 add)에서 같은 칸을 다르게 고쳤다. items: 겹친 곳."""

    def __init__(self, items):
        ValueError.__init__(self, "저장하는 사이 다른 곳에서 같은 곳을 고쳤다: %s" % ", ".join(items))
        self.items = items


def _merge_list(b, m, t):
    """목록 세 갈래: 지금 것(t)에서 내가(m) 뺀 것을 빼고, 내가 더한 것을 뒤에 붙인다. 양쪽 변경이 다 남는다."""
    gone = [x for x in b if x not in m]
    out = [x for x in t if x not in gone]
    return out + [x for x in m if x not in b and x not in out]


def _merge_one(b, m, t, fields):
    """항목 하나(없으면 None)의 세 갈래. 반환 (값, 겹친 칸). fields=True 면 구성요소처럼 칸별로 합친다."""
    if m == t or t == b:
        return m, []
    if m == b:
        return t, []
    if not fields:
        return None, [""]
    if m is None or t is None:                       # 한쪽은 지우고 다른 쪽은 고쳤다
        return None, ["지움"]
    b, out, bad = b or {}, {}, []
    for k in list(m) + [k for k in t if k not in m]:
        bv, mv, tv = b.get(k), m.get(k), t.get(k)
        if k in LIST_FIELDS:
            v = _merge_list(bv or [], mv or [], tv or [])
        elif mv == tv or tv == bv:
            v = mv
        elif mv == bv:
            v = tv
        else:
            bad.append(k)
            continue
        if v not in (None, "", []):
            out[k] = v
    return out, bad


def merge3(base, mine, theirs):
    """세 갈래 합치기 (모두 _norm 모양). base = 읽었던 것, mine = 그 위에 고친 것, theirs = 지금 파일.
    한쪽만 바꾼 것은 그쪽을 따른다. 구성요소는 칸별로 합치고, 별명·로그 문구와 정상 패턴은 양쪽에서 더하고 뺀 것을 모두 남긴다.
    같은 칸·같은 흐름을 양쪽에서 다르게 고쳤으면 Conflict. 반환: (합친 것, theirs 에서 들어온 변경 설명 목록)."""
    out, came, bad = {}, [], []
    for key, label in (("components", "구성요소"), ("flows", "흐름")):
        b, m, t = base[key], mine[key], theirs[key]
        res = {}
        for n in list(m) + [n for n in t if n not in m] + [n for n in b if n not in m and n not in t]:
            v, why = _merge_one(b.get(n), m.get(n), t.get(n), key == "components")
            if why:
                bad += ["%s %s%s" % (label, n, "(%s)" % w if w else "") for w in why]
                continue
            if v is not None:
                res[n] = v
            if t.get(n) != b.get(n) and m.get(n) != t.get(n):
                came.append("%s %s%s" % (label, n, " (지움)" if t.get(n) is None else ""))
        out[key] = res
    out["normal"] = _merge_list(base["normal"], mine["normal"], theirs["normal"])
    came += ["정상 패턴 %s" % x["component"] for x in theirs["normal"] if x not in base["normal"] and x not in mine["normal"]]
    came += ["정상 패턴 %s (지움)" % x["component"] for x in base["normal"] if x not in theirs["normal"] and x in mine["normal"]]
    if bad:
        raise Conflict(bad)
    return out, came


def save_shards(cluster, sdata, base=None):
    """웹 편집 화면의 전체 상태를 flows.yaml 로 쓴다.
    sdata: {"components": {...}, "flows": [{"name","kind","nodes":[…]}], "normal": [{component, note}]}
    base: {"version": 읽을 때의 version(), "data": 그때 상태}. 주면, 그사이 파일이 바뀌었을 때 덮어쓰지 않고 merge3 로 합쳐 쓴다
    (웹에서 저장해도 KOA 채팅이 그사이 더한 것이 지워지지 않게). 같은 곳이 겹치면 Conflict 를 내고 쓰지 않는다.
    반환의 "merged": 다른 쪽에서 들어와 함께 저장된 것 (없으면 [])."""
    s, merged = _norm(sdata), []
    if isinstance(base, dict) and base.get("version") is not None and str(base["version"]) != version(cluster):
        s, merged = merge3(_norm(base.get("data")), s, _norm(load_flows(cluster)))
    comps, flows = s["components"], {}
    for name, f in s["flows"].items():
        hops, kind = _hops(f["nodes"], comps, f["kind"])
        flows[name] = {"kind": kind, "hops": hops}
    data = {"schema": "koa.knowledge/v1", "cluster": cluster, "components": comps, "flows": flows, "normal": s["normal"]}
    _write_flows(cluster, data)
    return {"components": comps, "flows": flows, "normal": s["normal"], "check": check(cluster, data), "merged": merged}


def apply_additions(cluster, add, apply=True):
    """지식에 더한다. 기존 것은 지우거나 덮지 않는다:
    - 같은 이름 또는 같은 workload 의 구성요소 → 목록 칸(aliases·log_signatures)은 합치고, 빈 칸만 채운다
    - 기존 흐름과 같거나 그 일부인 흐름 → 건너뛴다
    반환: {"components": [새 이름], "updated": {이름: [칸]}, "flows": [(이름, 노드)], "normal": [...],
          "skipped": [(이름, 이유)], "check": [...], "saved": bool, "changed": bool}"""
    meta = {}
    data = load_flows(cluster, meta=meta)
    comps = {n: dict(c or {}) for n, c in data["components"].items()}
    by_wl = {}
    for n, c in sorted(comps.items(), key=lambda kv: len(comp_workloads(kv[1]))):   # 하나짜리가 먼저 차지한다
        for w in comp_workloads(c):
            by_wl.setdefault(w, n)
    rename, added_c, updated = {}, [], {}
    for n, c in ((add or {}).get("components") or {}).items():
        c = {k: v for k, v in dict(c or {}).items() if k in SCALAR_FIELDS + LIST_FIELDS and v not in (None, "", [])}
        tgt = n if n in comps else next((by_wl[w] for w in comp_workloads(c) if w in by_wl), None)
        if not tgt and not c.get("workload"):
            hit = name_to_comps(n, comps)
            tgt = hit[0] if len(hit) == 1 else None
        if not tgt:
            comps[n] = c
            added_c.append(n)
            rename[n] = n
            for w in comp_workloads(c):
                by_wl.setdefault(w, n)
            continue
        rename[n] = tgt
        cur, ch = comps[tgt], []
        for k in LIST_FIELDS:
            new = [x for x in (c.get(k) or []) if x not in (cur.get(k) or []) and x != tgt]
            if new:
                cur[k] = list(cur.get(k) or []) + new
                ch.append(k)
        for k in SCALAR_FIELDS:
            if c.get(k) not in (None, "") and cur.get(k) in (None, ""):
                cur[k] = c[k]
                ch.append(k)
        if ch:
            updated[tgt] = sorted(set(updated.get(tgt, []) + ch))

    def canon(chain):
        return tuple(chain)

    def inside(short, long_):
        return any(long_[i:i + len(short)] == short for i in range(len(long_) - len(short) + 1))

    flows = [{"name": n, "kind": f.get("kind"), "nodes": flow_nodes(f)} for n, f in data["flows"].items()]
    old = [canon(f["nodes"]) for f in flows]
    names = {f["name"] for f in flows}
    added_f, skipped = [], []
    for f in ((add or {}).get("flows") or []):
        nodes = [rename.get(x, x) for x in (f.get("nodes") or []) if x]
        base = f.get("name") or " → ".join(nodes)
        if len(nodes) < 2:
            skipped.append((base, "노드가 2개 미만"))
            continue
        c = canon(nodes)
        if any(inside(c, o) for o in old):
            skipped.append((base, "기존 흐름에 이미 있다"))
            continue
        name, j = base, 2
        while name in names:
            name, j = "%s (%d)" % (base, j), j + 1
        names.add(name)
        old.append(c)
        flows.append({"name": name, "kind": f.get("kind"), "nodes": nodes})
        added_f.append((name, nodes))
    normal = list(data["normal"])
    added_n = []
    for x in ((add or {}).get("normal") or []):
        item = {"component": rename.get(x.get("component"), x.get("component")), "note": (x.get("note") or "").strip()}
        if item["component"] and item["note"] and item not in normal:
            normal.append(item)
            added_n.append(item)
    changed = bool(added_c or updated or added_f or added_n)
    sdata = {"components": comps, "flows": flows, "normal": normal}
    out = {"components": added_c, "updated": updated, "flows": added_f, "normal": added_n, "skipped": skipped,
           "saved": False, "changed": changed}
    if apply and changed:
        # 읽고 쓰는 사이 웹에서 저장했어도 그 변경을 지우지 않게 읽은 버전을 같이 준다
        out["check"] = save_shards(cluster, sdata, base={"version": meta["version"], "data": data})["check"]
        out["saved"] = True
    else:
        tmp = {"components": comps, "normal": normal,
               "flows": {f["name"]: {"kind": f.get("kind") or "http", "hops": f["nodes"]} for f in flows}}
        out["check"] = check(cluster, tmp)
    return out


def additions_text(r):
    L = []
    for n in r["components"]:
        L.append("+ 구성요소 %s" % n)
    for n, ks in r["updated"].items():
        L.append("~ 구성요소 %s (%s)" % (n, ", ".join(ks)))
    for n, nodes in r["flows"]:
        L.append("+ 흐름 %s: %s" % (n, " → ".join(nodes)))
    for x in r["normal"]:
        L.append("+ 정상 패턴 %s: %s" % (x["component"], x["note"]))
    for n, why in r["skipped"]:
        L.append("= %s: %s" % (n, why))
    return "\n".join(L) or "(더할 것 없음 — 이미 있다)"


def slug(text, limit=40):
    """파일 이름용. 단어(-) 경계에서 자른다 — 'ingress-nginx' 가 'ing' 로 잘리지 않게."""
    words = [w for w in re.sub(r"[^0-9A-Za-z가-힣]+", "-", text or "").lower().split("-") if w]
    out = ""
    for w in words:
        nxt = (out + "-" + w) if out else w
        if len(nxt) > limit:
            break
        out = nxt
    return out or (words[0][:limit] if words else "incident")


def incident_text(meta, body):
    head = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=None).strip()
    return "---\n%s\n---\n%s\n" % (head, (body or "").strip())


def _date(text):
    m = re.search(r"(20\d\d)[-./년\s]+(\d{1,2})[-./월\s]+(\d{1,2})", text)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def incident_meta(text, comps):
    """이슈 자유 글 → 머리말 (date, components, signature.logs, cause). 웹 '과거 이슈' 탭."""
    meta = {}
    d = _date(text)
    if d:
        meta["date"] = d
    low = text.lower()
    found = [n for n, c in sorted((comps or {}).items(), key=lambda kv: -len(kv[0]))
             if any(a and a.lower() in low for a in [n] + list((c or {}).get("aliases") or []))]
    # 그룹 이름 ("wrapper 전체가 502") → 그 이름 그대로 (분석은 그룹의 구성요소 전부로 본다).
    # 찾은 구성요소 이름·별명 자리는 지우고 본다: 'admin-wrapper' 를 적었다고 그룹 'wrapper' 까지 붙지 않게
    rest = low
    for n in found:
        for a in sorted([n] + list((comps[n] or {}).get("aliases") or []), key=lambda x: -len(str(x))):
            if a:
                rest = rest.replace(str(a).lower(), " ")
    found += [g for g in sorted(groups(comps), key=lambda x: -len(x)) if g.lower() in rest and g not in found]
    if found:
        meta["components"] = found
    quotes = []
    for q in QUOTED.findall(text):
        if q.strip() not in quotes:
            quotes.append(q.strip())
    if quotes:
        meta["signature"] = {"logs": quotes}
    first = next((l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")), "")
    m = re.search(r"원인\s*[:：은는]?\s*(.+)", text)
    meta["cause"] = re.split(r"(?<=[^0-9])\.\s|\n", (m.group(1) if m else first).strip())[0].strip()[:120]
    return meta


def write_incident(cluster, inc):
    """과거 이슈 1건을 incidents/<날짜>-<slug>.md 로. inc: {date, components, logs, cause, body}."""
    d = cluster_dir(cluster) / "incidents"
    d.mkdir(parents=True, exist_ok=True)
    date = inc.get("date") or dt.date.today().isoformat()
    meta = {"date": date, "components": list(inc.get("components") or []),
            "signature": {"logs": list(inc.get("logs") or [])}, "cause": inc.get("cause") or ""}
    if inc.get("found_by"):
        meta["found_by"] = inc["found_by"]
    name = "%s-%s.md" % (date, slug(inc.get("cause") or ""))
    f, i = d / name, 2
    while f.exists():
        f, i = d / ("%s-%s-%d.md" % (date, slug(inc.get("cause") or ""), i)), i + 1
    f.write_text(incident_text(meta, inc.get("body") or ""))
    return f


INC_NAME_RE = re.compile(r"^[0-9A-Za-z가-힣._-]{1,120}\.md$")


def recur_incident(cluster, name, rec):
    """같은 원인이 다시 났을 때: 새 이슈 대신 기존 이슈의 머리말 recurrences 에 한 줄 더한다.
    rec: {date, note, logs?}. 새 로그 문구는 signature.logs 에 합친다. 같은 날짜·같은 메모는 다시 넣지 않는다."""
    if not INC_NAME_RE.match(name or ""):
        raise ValueError("이슈 파일 이름 형식이 아니다")
    f = cluster_dir(cluster) / "incidents" / name
    if not f.is_file():
        raise LookupError("과거 이슈 %s 가 없다" % name)
    meta, body = parse_front(f.read_text())
    item = {"date": str(rec.get("date") or dt.date.today().isoformat()), "note": str(rec.get("note") or "").strip()}
    recs = [r for r in (meta.get("recurrences") or []) if isinstance(r, dict)]
    if any(str(r.get("date")) == item["date"] and (r.get("note") or "") == item["note"] for r in recs):
        return f, False
    meta["recurrences"] = recs + [item]
    logs = incident_logs(meta)
    new = [x for x in (rec.get("logs") or []) if x and x not in logs]
    if new:
        meta["signature"] = {"logs": logs + new}
    f.write_text(incident_text(meta, body))
    return f, True


# ---------- 제안 (flowmap 이나 분석이 찾은 것, 사용자가 고르면 더한다) ----------

def _sfile(cluster):
    return cluster_dir(cluster) / "suggestions.yaml"


def _sload(cluster):
    f = _sfile(cluster)
    d = (yaml.safe_load(f.read_text()) or {}) if f.is_file() else {}
    return {"pending": list(d.get("pending") or []), "dismissed": list(d.get("dismissed") or [])}


def _ssave(cluster, s):
    cluster_dir(cluster).mkdir(parents=True, exist_ok=True)
    _sfile(cluster).write_text("# KOA 지식 제안 — 웹 '클러스터 지식' 화면이나 knowledge.py accept/dismiss 로 처리한다\n"
                               + yaml.safe_dump(s, allow_unicode=True, sort_keys=False, width=120))


def _skey(add):
    """같은 제안을 알아보는 열쇠: 흐름은 노드(워크로드) 순서, 그 밖은 내용 전체."""
    comps = (add or {}).get("components") or {}
    wl = lambda n: (comps.get(n) or {}).get("workload") or n  # noqa: E731
    parts = [" > ".join(wl(n) for n in f.get("nodes") or []) for f in (add or {}).get("flows") or []]
    if not parts:
        parts = [json.dumps(add, sort_keys=True, ensure_ascii=False, default=str)]
    return hashlib.sha1("|".join(sorted(parts)).encode()).hexdigest()[:10]


def add_suggestion(cluster, title, add, source, why=None):
    """제안 하나를 남긴다. 이미 지식에 있거나, 같은 제안이 있거나, 전에 버린 것이면 남기지 않는다 (None)."""
    s = _sload(cluster)
    key = _skey(add)
    if key in s["dismissed"] or any(p["id"] == key for p in s["pending"]):
        return None
    if not apply_additions(cluster, add, apply=False)["changed"]:
        return None
    s["pending"].append({"id": key, "title": title, "source": source, "why": why or "",
                         "created": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "add": add})
    _ssave(cluster, s)
    return key


def list_suggestions(cluster):
    """남은 제안 + 지금 더하면 무엇이 바뀌는지. 그새 지식에 들어가 더할 것이 없어진 제안은 뺀다."""
    out = []
    for p in _sload(cluster)["pending"]:
        r = apply_additions(cluster, p.get("add") or {}, apply=False)
        if r["changed"]:
            out.append(dict(p, preview=additions_text(r)))
    return out


def resolve_suggestion(cluster, sid, accept):
    s = _sload(cluster)
    hit = next((p for p in s["pending"] if p["id"] == sid), None)
    if not hit:
        raise LookupError("없는 제안: %s" % sid)
    r = apply_additions(cluster, hit.get("add") or {}) if accept else None
    s["pending"] = [p for p in s["pending"] if p["id"] != sid]
    if not accept:
        s["dismissed"].append(sid)
    _ssave(cluster, s)
    return r


def suggest_from_flowmap(cluster, shards, source):
    """flowmap 후보(파편 형식)를 흐름마다 제안 하나로. 같은 출처의 이전 flowmap 제안은 새 결과로 바꾼다.
    반환: 새로 남긴 제안 id 목록."""
    s = _sload(cluster)
    s["pending"] = [p for p in s["pending"] if not str(p.get("source", "")).startswith("flowmap")]
    _ssave(cluster, s)
    comps = shards.get("components") or {}
    ids = []
    for f in shards.get("flows") or []:
        add = {"components": {n: comps[n] for n in f.get("nodes") or [] if n in comps},
               "flows": [{"name": f.get("name"), "kind": f.get("kind"), "nodes": f.get("nodes")}]}
        sid = add_suggestion(cluster, "흐름 %s" % " → ".join(f.get("nodes") or []), add, source, f.get("why"))
        if sid:
            ids.append(sid)
    return ids


# ---------- 새 클러스터 ----------

def init(cluster):
    """빈 지식 폴더를 만든다. 구성요소는 웹 '클러스터에서 고르기'나 대화로 더한다."""
    d = cluster_dir(cluster)
    if d.exists():
        raise ValueError("이미 있다: %s" % d)
    (d / "incidents").mkdir(parents=True)
    _write_flows(cluster, {"schema": "koa.knowledge/v1", "cluster": cluster, "components": {}, "flows": {}, "normal": []})
    (d / "architecture.md").write_text(STARTER % cluster)
    return d


STARTER = """# %s

<!-- 자유 메모. 구조로 못 적는 것(팀, 운영 관행, 주의할 점)을 적는다. KOA 는 분석 때 참고로 읽는다. -->
"""


# ---------- 검사 ----------

def check(cluster, data=None):
    """지식의 문제 목록 [(level, 문구)]. level: error | warn."""
    data = data if data is not None else load_flows(cluster)
    comps = data.get("components") or {}
    out = []
    owner = {}
    for name, c in comps.items():
        c = c or {}
        if not c.get("external") and not c.get("workload"):
            out.append(("warn", "%s: workload 가 비었다 (클러스터 밖이면 external: true)" % name))
        for w in comp_workloads(c):
            if not re.match(r"^[^/\s]+/[A-Za-z]+/[^/\s]+$", w):
                out.append(("error", "%s: workload 는 <ns>/<Kind>/<이름> 형식이어야 한다 (지금 %r)" % (name, w)))
        for a in [name] + list(c.get("aliases") or []):
            k = str(a).lower()
            if k in owner and owner[k] != name:
                out.append(("warn", "'%s' 가 %s 와 %s 둘 다의 이름이다 (질문에서 못 가린다)" % (a, owner[k], name)))
            owner[k] = name
    for g in groups(comps):
        if g.lower() in owner:
            out.append(("warn", "그룹 '%s' 가 구성요소 %s 의 이름과 같다 (흐름·질문에서 못 가린다)" % (g, owner[g.lower()])))
    for fname, f in (data.get("flows") or {}).items():
        es = edges(f)
        if not es:
            out.append(("warn", "흐름 %s: hop 이 없다" % fname))
        for e in es:
            for n in (e["from"], e["to"]):
                if n and not node_to_comps(n, comps):
                    hint = similar_comps(n, comps)
                    out.append(("warn", "흐름 %s: '%s' 는 구성요소·그룹 이름이 아니다 → %s" % (
                        fname, n, ("%s 로 바꾸거나 " % " / ".join(hint[:3]) if hint else "") + "구성요소로 더한다")))
            if e["role"] not in ROLES:
                out.append(("error", "흐름 %s: role '%s' 는 produce | consume | call 중 하나" % (fname, e["role"])))
        if ";" in flow_text(f):
            out.append(("warn", "흐름 %s: 한 줄이 아니라 갈래가 있다 (%s). 웹 화면은 한 줄로만 다뤄 저장하면 순서가 바뀐다 → "
                                "갈래는 별도 흐름으로 (knowledge.py add)" % (fname, flow_text(f))))
    for n in data.get("normal") or []:
        if isinstance(n, dict) and n.get("component") and not node_to_comps(n["component"], comps):
            out.append(("warn", "정상 패턴: '%s' 는 구성요소·그룹 이름이 아니다" % n["component"]))
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


# ---------- 꺼내기: 질문에 해당하는 지식만 ----------

def groups(comps):
    """그룹 → 그 그룹의 구성요소 목록 (적힌 순서). 구성요소마다 그룹 하나 (예: admin/manager/user console wrapper → wrapper).
    그룹 이름은 흐름 칸에 쓸 수 있고, 질문에 나오면 그 구성요소 전부를 본다."""
    out = {}
    for n, c in (comps or {}).items():
        t = str((c or {}).get("group") or "").strip()
        if t:
            out.setdefault(t, []).append(n)
    return out


def mentions(text, comps, flows=None):
    """질문에 나온 구성요소·흐름 (나온 순서). 긴 이름부터 맞추고 맞춘 자리는 지워서 'shop-worker' 가 'shop' 으로 또 잡히지 않게.
    그룹 이름을 말하면 그 그룹의 구성요소 전부 ("wrapper 가 이상해")."""
    norm = lambda x: re.sub(r"[-_\s]+", " ", str(x).lower()).strip()  # noqa: E731  'manager console wrapper' = 'manager-console-wrapper'
    low = " %s " % norm(text or "")
    terms = []
    for g in groups(comps):
        terms.append((norm(g), "t", g))
    for n, c in (comps or {}).items():
        for a in [n] + list((c or {}).get("aliases") or []):
            if str(a).strip():
                terms.append((norm(a), "c", n))
    for n in (flows or {}):
        terms.append((norm(n), "f", n))
    terms.sort(key=lambda t: -len(t[0]))
    found = []
    for term, kind, name in terms:
        i = low.find(term)
        if i < 0:
            continue
        low = low[:i] + " " * len(term) + low[i + len(term):]
        found.append((i, kind, name))
    found.sort()
    comps_hit, flows_hit = [], []
    by_theme = groups(comps)
    for _, kind, name in found:
        for n in (by_theme.get(name, []) if kind == "t" else [name]):
            lst = flows_hit if kind == "f" else comps_hit
            if n not in lst:
                lst.append(n)
    return comps_hit, flows_hit


def context(cluster, question, names=None, data=None, incidents=None):
    """질문(또는 사용자가 지정한 이름들)에 해당하는 지식만: 그 구성요소, 그것을 지나는 흐름, 흐름 위 구성요소,
    관련 과거 이슈, 정상 패턴. 분석은 이것만 가지고 시작한다."""
    data = data if data is not None else load_flows(cluster)
    incidents = incidents if incidents is not None else load_incidents(cluster)
    comps, flows = data.get("components") or {}, data.get("flows") or {}
    notes = []
    if names:
        picked_c, picked_f, unknown = [], [], []
        by_theme = groups(comps)
        for n in names:
            if n not in comps and n in by_theme:
                picked_c += [x for x in by_theme[n] if x not in picked_c]
                continue
            (picked_c if n in comps else picked_f if n in flows else unknown).append(n)
        for n in unknown:
            hit = name_to_comps(n, comps)
            picked_c += [h for h in hit if h not in picked_c]
            if not hit:
                notes.append("'%s' 는 지식에 없다 → 분석하며 무엇인지 확인하고, 사용자 OK 후 지식에 더한다" % n)
    else:
        picked_c, picked_f = mentions(question, comps, flows)
    hit_f = list(picked_f)
    # 흐름 노드가 말한 구성요소이거나, 그 워크로드를 품은 구성요소(예: 게이트웨이 여럿을 묶은 것)이면 그 흐름
    picked_w = {w for n in picked_c for w in comp_workloads(comps.get(n))}
    for fn, f in flows.items():
        if fn not in hit_f and any(set(node_to_comps(n, comps)) & set(picked_c)
                                   or (picked_w & set(comp_workloads(comps.get(n))))
                                   for n in flow_nodes(f)):
            hit_f.append(fn)
    scope = list(picked_c)
    flow_view, group_view = {}, {}
    for fn in hit_f:
        nodes = flow_nodes(flows[fn])
        flow_view[fn] = flow_text(flows[fn])
        for n in nodes:
            members = node_to_comps(n, comps)
            if n not in comps and members:
                group_view[n] = members
            for c in members:
                if c not in scope:
                    scope.append(c)
    q = (question or "").lower()
    known = []
    for inc in incidents:
        m = inc["meta"]
        ic = [str(x) for x in (m.get("components") or [])]
        # 이슈의 구성요소 칸에 그룹 이름을 썼으면 그 그룹의 구성요소 전부 (흐름 칸과 같은 규칙). 그룹 하나는 한 곳으로 센다
        ics = [set(node_to_comps(x, comps) or [x]) for x in ic]
        sig = incident_logs(m)
        if any(s & set(picked_c) for s in ics) or sum(1 for s in ics if s & set(scope)) >= 2 \
                or any(s.lower() in q for s in sig):
            known.append({"file": inc["file"], "date": str(m.get("date") or ""), "cause": m.get("cause") or "",
                          "components": ic, "logs": sig,
                          "recurrences": len([r for r in (m.get("recurrences") or []) if isinstance(r, dict)])})
    # 정상 패턴도 구성요소 이름이나 그룹 이름 (그룹이면 그 구성요소 중 하나라도 범위에 있을 때)
    normal = []
    for n in data.get("normal") or []:
        members = node_to_comps(n.get("component"), comps) if isinstance(n, dict) else []
        if set(members) & set(scope):
            normal.append(n if n.get("component") in comps else dict(n, members=members))
    if not picked_c and not picked_f:
        notes.append("질문에서 지식 속 구성요소·흐름을 못 찾았다 → 어느 구성요소/흐름인지 사용자에게 묻는다 "
                     "(답을 들으면 그 말을 별명으로 더하자고 제안)")
    for n in scope:
        c = comps.get(n) or {}
        if not c.get("workload") and not c.get("external"):
            notes.append("%s: 워크로드를 모른다 → 클러스터에서 찾으면 지식에 더하자고 제안" % n)
    return {"request": question, "cluster": cluster, "asked": picked_c, "flows": flow_view, "groups": group_view,
            "components": {n: comps.get(n) or {} for n in scope}, "incidents": known, "normal": normal, "notes": notes}


def context_text(p):
    L = ["질문: %s" % p["request"], "말한 것: %s" % (", ".join(p["asked"]) or "(없음)")]
    for fn, nodes in p["flows"].items():
        L.append("흐름 %s: %s" % (fn, nodes))
    for g, members in (p.get("groups") or {}).items():
        L.append("그룹 %s: %s (흐름의 이 칸은 이 중 하나 이상을 지난다)" % (g, ", ".join(members)))
    if p["components"]:
        L.append("구성요소:")
        for n, c in p["components"].items():
            where = ", ".join(comp_workloads(c)) or ("클러스터 밖" if c.get("external") else "?")
            extra = ("  로그: %s" % ", ".join('"%s"' % s for s in c["log_signatures"])) if c.get("log_signatures") else ""
            L.append("  %s%-18s %s%s" % ("* " if n in p["asked"] else "  ", n, where, extra))
    for k in p["incidents"]:
        L.append("과거 이슈 %s %s — %s%s%s" % (k["date"], k["file"], k["cause"],
                                            (" (로그: %s)" % ", ".join(k["logs"])) if k["logs"] else "",
                                            (" [재발 %d회]" % k["recurrences"]) if k.get("recurrences") else ""))
    for n in p["normal"]:
        L.append("정상 패턴 %s%s: %s" % (n.get("component"),
                                       " (그룹: %s)" % ", ".join(n["members"]) if n.get("members") else "", n.get("note")))
    for n in p["notes"]:
        L.append("참고: %s" % n)
    return "\n".join(L)


# ---------- CLI ----------

def _json_arg(s):
    if s == "-":
        s = sys.stdin.read()
    elif s.startswith("@"):
        s = Path(s[1:]).read_text()
    return json.loads(s)


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list")
    sub.add_parser("init").add_argument("cluster")
    sub.add_parser("check").add_argument("cluster")
    cp = sub.add_parser("context")
    cp.add_argument("cluster")
    cp.add_argument("question", nargs="?", default="")
    cp.add_argument("--with", dest="names", default=None, help="이 구성요소·흐름 이름으로 (쉼표)")
    cp.add_argument("--yaml", action="store_true")
    for name in ("add", "suggest"):
        p = sub.add_parser(name)
        p.add_argument("cluster")
        p.add_argument("json", help="JSON 문자열, @파일, - (표준입력)")
        if name == "add":
            p.add_argument("--dry-run", action="store_true")
        else:
            p.add_argument("--title", required=True)
            p.add_argument("--source", default="분석")
            p.add_argument("--why", default="")
    ip = sub.add_parser("incident")
    ip.add_argument("cluster")
    ip.add_argument("json")
    rp = sub.add_parser("recur", help="같은 원인 재발: 기존 이슈에 한 줄 더한다")
    rp.add_argument("cluster")
    rp.add_argument("file", help="incidents/ 안의 파일 이름 (context 출력의 이름)")
    rp.add_argument("json", help='{"date": "...", "note": "...", "logs": [...]}')
    sub.add_parser("suggestions").add_argument("cluster")
    for name in ("accept", "dismiss"):
        p = sub.add_parser(name)
        p.add_argument("cluster")
        p.add_argument("id")
    a = ap.parse_args()
    try:
        if a.cmd == "list":
            print("\n".join(clusters()) or "(없음) — init <클러스터> 로 만든다")
        elif a.cmd == "init":
            print("만들었다: %s" % init(a.cluster))
        elif a.cmd == "check":
            probs = check(a.cluster)
            for lv, msg in probs:
                print("%s %s" % ("❌" if lv == "error" else "⚠", msg))
            print("✅ 문제 없음" if not probs else "")
            sys.exit(1 if any(lv == "error" for lv, _ in probs) else 0)
        elif a.cmd == "context":
            names = [x.strip() for x in a.names.split(",") if x.strip()] if a.names else None
            p = context(a.cluster, a.question, names)
            print(yaml.safe_dump(p, allow_unicode=True, sort_keys=False) if a.yaml else context_text(p))
        elif a.cmd == "add":
            r = apply_additions(a.cluster, _json_arg(a.json), apply=not a.dry_run)
            print(additions_text(r))
            for lv, msg in r["check"]:
                print("%s %s" % ("❌" if lv == "error" else "⚠", msg))
            print("저장했다 (이전 것은 flows.yaml.bak)" if r["saved"] else "미리 보기다 (저장 안 함)" if a.dry_run else "")
        elif a.cmd == "incident":
            print("기록했다: %s" % write_incident(a.cluster, _json_arg(a.json)))
        elif a.cmd == "recur":
            f, added = recur_incident(a.cluster, a.file, _json_arg(a.json))
            print(("재발을 더했다: %s" if added else "이미 기록된 재발이다: %s") % f)
        elif a.cmd == "suggest":
            sid = add_suggestion(a.cluster, a.title, _json_arg(a.json), a.source, a.why)
            print("제안 %s 를 남겼다" % sid if sid else "남기지 않았다 (이미 지식에 있거나, 같은 제안이 있거나, 전에 버린 것)")
        elif a.cmd == "suggestions":
            for p in list_suggestions(a.cluster):
                print("[%s] %s  (%s, %s)\n%s\n" % (p["id"], p["title"], p["source"], p["created"],
                                                 "\n".join("    " + x for x in p["preview"].splitlines())))
        elif a.cmd in ("accept", "dismiss"):
            r = resolve_suggestion(a.cluster, a.id, a.cmd == "accept")
            print(additions_text(r) if r else "버렸다 (다시 제안하지 않는다)")
        else:
            ap.print_help()
    except (ValueError, LookupError) as x:
        sys.exit(str(x))


if __name__ == "__main__":
    main()
