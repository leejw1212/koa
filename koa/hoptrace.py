#!/usr/bin/env python3
"""정상 세션 잇기 — 사용자가 말한 구성요소의 로그만 읽어 요청 하나가 지나간 홉을 잇는다.

  python3 koa/hoptrace.py <클러스터> "백엔드 정상 세션" [--with 백엔드] [--since 1h] [--json]

범위는 knowledge.context 와 같다: 말한 구성요소 + 그 구성요소를 지나는 http 흐름의 구성요소
(큐 흐름까지 보려면 --all-flows). 그 워크로드의 파드 로그를 동시에 읽어(버퍼가 짧은 홉이 밀려나기 전에)
아래를 낸다. 클러스터에는 get pods/svc 와 logs 만 보낸다 (첫 줄에 요청 수).

  - 요청 ID 잇기: req-<UUID> 와 req-<32hex> 를 하이픈 없이 같은 것으로 보고, 한 줄에 함께 찍힌 ID 는
    같은 요청 사슬로 묶는다 (global/local ID 를 같이 찍는 앱이면 저절로 이어진다).
  - 앱이 처리한 요청 줄(상태·메서드·URL), 앞 홉이 남긴 호출 줄(URL + 걸린 시간),
    nginx access log(ingress-nginx 형식: upstream 이름·주소·시간·상태·UA).
  - nginx upstream 이름은 Service 이름으로 풀고, Service selector 로 구성요소에 붙인다.
  - 시각은 kubelet 타임스탬프(--timestamps)로 맞춘다 — 컨테이너 시계가 틀려도 홉끼리 견줄 수 있다.

구성요소마다: 요청 수·상태, 바깥 host, 요청 ID 로 이어진 호출자, nginx 를 거친 줄(상태·UA),
그리고 세 홉(호출자 → nginx → 앱)이 이어진 최근 정상 세션 1건과 시간 대조(바깥 홉일수록 길어야 맞다),
워크로드별 로그 보존 깊이. 지식은 고치지 않는다.
"""
import argparse
import collections
import datetime as dt
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import knowledge as K  # noqa: E402
from discover import DEFAULT_KUBECONFIG, Kube, owner_of, selector_matches  # noqa: E402

CALLS = collections.Counter()      # 클러스터에 보낸 요청 수 (첫 줄에 보인다)

# ─── 줄 해석 (클러스터 없이 검사한다: tests/test_hoptrace.py) ─────────────────────────
RID = re.compile(r"req-(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32})(?![0-9a-f-])")
KTS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d) ")
# ingress-nginx 기본 형식: ... "GET /p HTTP/1.1" 200 12 "ref" "ua" 345 0.010 [upstream] [alt] 10.0.0.1:8080 12 0.010 200 reqid
# (재시도면 주소·길이·시간·상태가 쉼표 목록)
NGINX = re.compile(r'^(\S+) - \S+ \[[^\]]+\] "(\S+) (\S+) [^"]*" (\d{3}) \d+ "[^"]*" "([^"]*)" \d+ ([\d.]+) '
                   r'\[([^\]]*)\] \[[^\]]*\] ([^\s,]+(?:, [^\s,]+)*) [^\s,]+(?:, [^\s,]+)* ([^\s,]+(?:, [^\s,]+)*) ([^\s,]+(?:, [^\s,]+)*)')
SERVED = [
    # oslo 계열 미들웨어: "Processed request: 200 OK - GET http://host/path"
    re.compile(r"Processed request: (?P<st>\d{3})\b.*? - (?P<m>[A-Z]+) (?P<url>https?://\S+)"),
    # 일반 접근 로그: "GET /path HTTP/1.1" 200
    re.compile(r'"(?P<m>GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS) (?P<url>\S+) HTTP/[\d.]+" (?P<st>\d{3})\b'),
]
# 앞 홉이 남긴 호출 줄: URL 뒤에 걸린 시간 (time/took/elapsed/duration)
CALL = re.compile(r"(?P<url>https?://[^\s'\"]+)\S*\s+(?:time|took|elapsed|duration)[=: ]+(?P<sec>\d+(?:\.\d+)?)")


def norm_id(rid):
    return rid[4:].replace("-", "")


def ids_in(line):
    return sorted({norm_id(m) for m in RID.findall(line)})


def kubelet_time(line):
    """'2026-10-13T20:01:44.198123456+09:00 rest' 또는 '...Z rest' → (UTC datetime, rest). 접두어가 없으면 (None, line).
    kubelet 은 노드 시간대로 찍기도 한다 → UTC 로 맞춰 둔다"""
    m = KTS.match(line)
    if not m:
        return None, line
    t = dt.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
    if m.group(2):
        t = t.replace(microsecond=int(m.group(2)[:6].ljust(6, "0")))
    z = m.group(3)
    if z != "Z":
        sign = 1 if z[0] == "+" else -1
        t -= sign * dt.timedelta(hours=int(z[1:3]), minutes=int(z[4:6]))
    return t, line[m.end():]


def parse_nginx(line):
    m = NGINX.match(line)
    if not m:
        return None
    ip, meth, path, st, ua, rt, up, upaddr, urt, ust = m.groups()
    # 재시도하면 upstream 칸이 'a:80, b:80' 처럼 목록이 된다 → 마지막(응답한) 것
    return {"ip": ip, "method": meth, "path": path, "status": st, "ua": ua, "rt": float(rt),
            "upstream": up, "upaddr": upaddr.split(", ")[-1], "ustatus": ust.split(", ")[-1], "tries": upaddr.count(",") + 1}


def parse_served(line):
    for rx in SERVED:
        m = rx.search(line)
        if m:
            url = m.group("url")
            host, path = "", url
            hm = re.match(r"https?://([^/]+)(.*)", url)
            if hm:
                host, path = hm.group(1), hm.group(2) or "/"
            return {"status": m.group("st"), "method": m.group("m"), "host": host, "path": path}
    return None


def parse_call(line):
    m = CALL.search(line)
    if not m:
        return None
    hm = re.match(r"https?://([^/]+)(.*)", m.group("url"))
    if not hm:
        return None
    return {"host": hm.group(1), "path": hm.group(2) or "/", "sec": float(m.group("sec"))}


def last_seg(path):
    p = (path or "").split("?", 1)[0].rstrip("/")
    return p.rsplit("/", 1)[-1]


def ua_name(ua):
    if "Synthetics" in ua:
        return "Synthetics"
    if "HeadlessChrome" in ua:
        return "HeadlessChrome"
    if ua.startswith("Mozilla"):
        return "Browser"
    return (ua.split(" ")[0] or "-")[:45]


def upstream_service(label, services):
    """nginx upstream 이름 → (ns, svc). ingress-nginx 는 '<ns>-<svc>-<port>', 다른 빌드는 '<ns>_<svc>_<port>'.
    services: {(ns, svc): ...}"""
    for (ns, svc) in services:
        for sep in ("_", "-"):
            pre = ns + sep + svc + sep
            if label.startswith(pre) and label[len(pre):].isdigit():
                return ns, svc
    return None


class UF:
    """같은 줄에 함께 찍힌 요청 ID 를 한 사슬로"""
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, ids):
        if ids:
            r = self.find(ids[0])
            for i in ids[1:]:
                self.p[self.find(i)] = r


# ─── 분석 (줄 목록 → 결과) ───────────────────────────────────────────────────────
MAX_IDS_PER_LINE = 2    # global+local 처럼 두 개까지만 한 사슬로 묶는다 (여러 요청을 한 줄에 찍는 로그가 사슬을 뭉개지 않게)


def analyze(lines_by_pod, pod_comp, svc_comp, now=None, pod_workload=None):
    """lines_by_pod: {pod: [줄(kubelet 타임스탬프 포함)]}, pod_comp: {pod: 구성요소}, svc_comp: {(ns, svc): 구성요소},
    pod_workload: {pod: 워크로드 이름} (nginx 를 거친 곳·보존 깊이를 워크로드로 보일 때)
    → {구성요소: 결과}, {워크로드(없으면 구성요소): 보존 깊이 초 — 가장 얕은 파드 기준}"""
    pod_workload = pod_workload or {}
    uf = UF()
    served, calls, nginx = [], [], []
    first_seen = collections.defaultdict(list)       # 워크로드 → 파드별 첫 줄 시각
    id_first = {}                                     # (comp, id) → 그 구성요소에서 처음 본 시각
    rows_ids = []
    for pod, lines in lines_by_pod.items():
        comp = pod_comp[pod]
        where = pod_workload.get(pod, comp)
        first = None
        for raw in lines:
            t, line = kubelet_time(raw)
            if t and first is None:
                first = t
            ids = ids_in(line)
            if len(ids) <= MAX_IDS_PER_LINE:
                uf.union(ids)
            for i in ids:
                k = (comp, i)
                if t and (k not in id_first or t < id_first[k]):
                    id_first[k] = t
            ng = parse_nginx(line)
            if ng:
                svc = upstream_service(ng["upstream"], svc_comp)
                ng.update(t=t, pod=pod, via=where, target=svc_comp.get(svc) if svc else None)
                nginx.append(ng)
                continue
            sv = parse_served(line)
            if sv:
                sv.update(t=t, pod=pod, comp=comp, ids=ids)
                served.append(sv)
            cl = parse_call(line)
            if cl and ids:
                cl.update(t=t, pod=pod, comp=comp, ids=ids)
                calls.append(cl)
            if ids:
                rows_ids.append((comp, ids))
        if first:
            first_seen[where].append(first)
    chain_comps = collections.defaultdict(set)        # 사슬 → 지나간 구성요소
    for comp, ids in rows_ids:
        for i in ids:
            chain_comps[uf.find(i)].add(comp)
    calls_by_chain = collections.defaultdict(list)
    for c in calls:
        calls_by_chain[uf.find(c["ids"][0])].append(c)
    nginx_by_target = collections.defaultdict(list)
    for n in nginx:
        if n["target"]:
            nginx_by_target[n["target"]].append(n)

    def gap(a, b):
        return abs((a - b).total_seconds()) if a and b else 1e9

    out = {}
    for comp in sorted({s["comp"] for s in served}):
        rows = [s for s in served if s["comp"] == comp]
        st = collections.Counter(s["status"][0] + "xx" for s in rows)
        hosts = collections.Counter(s["host"].split(".")[0] if s["host"] else "-" for s in rows)
        callers = collections.Counter()
        sessions = []
        for s in rows:
            if not s["ids"]:
                callers["(요청 ID 없음)"] += 1
                continue
            r = uf.find(s["ids"][0])
            others = sorted(chain_comps[r] - {comp})
            callers[", ".join(others) or "(이어진 곳 없음)"] += 1
            if others and s["status"].startswith("2"):
                sessions.append((s, r, others))
        ng = nginx_by_target.get(comp, [])
        best, best_score = None, -1
        for s, r, others in reversed(sessions):
            seg = last_seg(s["path"])
            # 같은 사슬에서 같은 경로를 여러 번 부를 수 있다 → 앱 처리 시각에 가장 가까운 호출 줄
            cands = [c for c in calls_by_chain.get(r, []) if c["comp"] != comp and last_seg(c["path"]) == seg]
            call = min(cands, key=lambda c: gap(c["t"], s["t"])) if cands else None
            near = [n for n in ng if gap(n["t"], s["t"]) <= 3 and last_seg(n["path"]) == seg]
            hop = min(near, key=lambda n: gap(n["t"], s["t"])) if near else None
            # 앱 처리 시간: 이 처리 줄의 ID 들이 이 구성요소에 처음 찍힌 때 중 가장 늦은 것부터.
            # global ID 는 한 세션에서 여러 번 불려 일찍부터 찍혀 있고, local ID 가 이 요청의 시작이다
            begins = [id_first[(comp, i)] for i in s["ids"] if (comp, i) in id_first]
            app_sec = round((s["t"] - max(begins)).total_seconds(), 3) if begins and s["t"] else None
            score = (2 if call else 0) + (2 if hop else 0)
            if score <= best_score:
                continue
            secs = [x for x in ((call or {}).get("sec"), (hop or {}).get("rt"), app_sec) if x is not None]
            best_score = score
            best = {"time": s["t"].isoformat() if s["t"] else None, "pod": s["pod"], "status": s["status"],
                    "request": "%s %s%s" % (s["method"], s["host"], s["path"][:100]), "via": others,
                    "caller_call": ({"comp": call["comp"], "pod": call["pod"], "sec": call["sec"],
                                     "url": call["host"] + call["path"][:100]} if call else None),
                    "nginx": ({"via": hop["via"], "pod": hop["pod"], "status": hop["status"], "ustatus": hop["ustatus"],
                               "rt": hop["rt"], "upaddr": hop["upaddr"], "ua": ua_name(hop["ua"])} if hop else None),
                    "app_sec": app_sec,
                    "order_ok": len(secs) >= 2 and all(a >= b for a, b in zip(secs, secs[1:]))}
            if score == 4:
                break                                  # 세 홉이 다 이어진 가장 최근 세션
        out[comp] = {
            "requests": len(rows), "status": dict(st), "hosts": hosts.most_common(6),
            "callers": callers.most_common(8), "sessions": len(sessions),
            "nginx": {"lines": len(ng), "status": dict(collections.Counter(n["status"] for n in ng)),
                      "by": collections.Counter("%s · %s" % (n["via"], ua_name(n["ua"])) for n in ng).most_common(6)},
            "sample": best,
        }
    now = now or dt.datetime.utcnow()
    depth = {w: int((now - max(ts)).total_seconds()) for w, ts in first_seen.items()}
    return out, depth


# ─── 클러스터 읽기 ──────────────────────────────────────────────────────────────
def scope(cluster, question, names, all_flows):
    p = K.context(cluster, question, names=names)
    data = K.load_flows(cluster)
    flows = data.get("flows") or {}
    keep = list(p["asked"])
    for fn in p["flows"]:
        f = flows.get(fn) or {}
        if all_flows or (f.get("kind") or "http") == "http":
            for n in K.flow_nodes(f):
                for c in K.node_to_comps(n, p["components"]) or ([n] if n in p["components"] else []):
                    if c not in keep:
                        keep.append(c)
    comps = {c: p["components"][c] for c in keep if c in p["components"]}
    return p, comps


def collect(kube, comps, since, max_pods, workers=8):
    """구성요소 워크로드의 파드 + 그 네임스페이스 Service → 로그(동시에)"""
    want = {}                                          # (ns, kind, name) → comp
    for c, spec in comps.items():
        for w in K.comp_workloads(spec):
            parts = w.split("/")
            if len(parts) == 3:
                want[tuple(parts)] = c
    nss = sorted({ns for ns, _, _ in want})
    pods, pod_comp, pod_workload, skipped = [], {}, {}, {}
    services = {}
    for ns in nss:
        CALLS["get pods"] += 1
        items = kube.json("get", "pods", "-n", ns).get("items") or []
        by_w = collections.defaultdict(list)
        for it in items:
            if (it.get("status") or {}).get("phase") != "Running":
                continue
            kind, name = owner_of(it)
            if (ns, kind, name) in want:
                by_w[(ns, kind, name)].append(it)
        for w, its in by_w.items():
            its.sort(key=lambda i: i["metadata"]["name"])
            if len(its) > max_pods:
                skipped[w] = len(its) - max_pods
            for it in its[:max_pods]:
                pods.append((ns, it))
                pod_comp[it["metadata"]["name"]] = want[w]
                pod_workload[it["metadata"]["name"]] = w[2]
        CALLS["get svc"] += 1
        for s in kube.json("get", "services", "-n", ns).get("items") or []:
            sel = (s.get("spec") or {}).get("selector") or {}
            hit = {pod_comp[i["metadata"]["name"]] for _, i in pods
                   if i["metadata"]["namespace"] == ns and selector_matches(sel, i["metadata"].get("labels") or {})}
            if len(hit) == 1:
                services[(ns, s["metadata"]["name"])] = hit.pop()

    def read(arg):
        ns, it = arg
        name = it["metadata"]["name"]
        ctr = it["spec"]["containers"][0]["name"]
        p = kube.run("logs", name, "-n", ns, "-c", ctr, "--since=%s" % since, "--timestamps", check=False)
        return name, (p.stdout.splitlines() if p.returncode == 0 else [])

    CALLS["logs"] += len(pods)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        logs = dict(ex.map(read, pods))
    return logs, pod_comp, pod_workload, services, skipped


def report(p, comps, res, depth, skipped, since):
    L = ["클러스터 요청: %s (읽기만)" % " · ".join("%s %d" % kv for kv in sorted(CALLS.items())),
         "범위: %s (최근 %s)" % (", ".join(comps), since)]
    if skipped:
        L.append("파드가 많아 일부만 읽음: " + ", ".join("%s +%d" % ("/".join(w), n) for w, n in skipped.items()))
    L.append("로그 보존 깊이(가장 얕은 파드): " + " · ".join(
        "%s %dm" % (c, s // 60) for c, s in sorted(depth.items(), key=lambda kv: kv[1])))
    for c, r in res.items():
        L.append("")
        L.append("## %s  요청 %d %s · 이어진 세션 %d · nginx %d %s" % (
            c, r["requests"], r["status"], r["sessions"], r["nginx"]["lines"], r["nginx"]["status"] or ""))
        L.append("  바깥 host: " + ", ".join("%s %d" % kv for kv in r["hosts"]))
        L.append("  같은 요청 사슬에 찍힌 곳: " + ", ".join("%s %d" % kv for kv in r["callers"]))
        if r["nginx"]["by"]:
            L.append("  nginx(거친 곳 · UA): " + ", ".join("%s %d" % kv for kv in r["nginx"]["by"]))
        s = r["sample"]
        if s:
            cc, ng = s["caller_call"], s["nginx"]
            L.append("  세션 %s %s %s" % (s["time"], s["status"], s["request"]))
            L.append("    호출자 %s" % ("%s %s %.3fs" % (cc["comp"], cc["pod"], cc["sec"]) if cc else "(호출 줄 없음: %s)" % ", ".join(s["via"])))
            L.append("    nginx  %s" % ("%s %s %s/%s %.3fs → %s (UA %s)" % (ng["via"], ng["pod"], ng["status"], ng["ustatus"],
                                                                         ng["rt"], ng["upaddr"], ng["ua"]) if ng else "(버퍼 밖이거나 nginx 를 안 거침)"))
            L.append("    앱     %s %s" % (s["pod"], "%.3fs" % s["app_sec"] if s["app_sec"] is not None else ""))
            L.append("    시간 대조(바깥 ≥ 안쪽): %s" % ("맞음" if s["order_ok"] else "판단 불가/어긋남"))
        else:
            L.append("  세션: 이어진 정상(2xx) 요청 없음")
    silent = [c for c in comps if c not in res]
    if silent:
        L.append("")
        L.append("요청 줄을 못 찾은 구성요소 (요청 없음, 형식 모름, nginx·큐 등): " + ", ".join(silent))
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("cluster")
    ap.add_argument("question", nargs="?", default="")
    ap.add_argument("--with", dest="names", default="", help="구성요소·그룹·흐름 이름 (쉼표)")
    ap.add_argument("--since", default="1h")
    ap.add_argument("--all-flows", action="store_true", help="http 가 아닌 흐름(큐 등)의 구성요소도")
    ap.add_argument("--max-pods", type=int, default=30, help="워크로드당 읽을 파드 수 상한")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--kubeconfig", default=os.environ.get("KOA_KUBECONFIG", str(DEFAULT_KUBECONFIG)))
    ap.add_argument("--context", default=None)
    a = ap.parse_args()
    if Path(a.kubeconfig).expanduser().resolve() == (Path.home() / ".kube" / "config").resolve():
        raise SystemExit("~/.kube/config(관리자)로는 실행하지 않는다. 읽기 전용 kubeconfig 를 쓴다.")
    names = [x.strip() for x in a.names.split(",") if x.strip()] or None
    p, comps = scope(a.cluster, a.question, names, a.all_flows)
    if not comps:
        raise SystemExit("범위에 워크로드가 있는 구성요소가 없다 → --with 로 이름을 준다")
    kube = Kube(Path(a.kubeconfig).expanduser(), a.context)
    logs, pod_comp, pod_workload, services, skipped = collect(kube, comps, a.since, a.max_pods)
    res, depth = analyze(logs, pod_comp, services, pod_workload=pod_workload)
    if a.json:
        print(json.dumps({"calls": dict(CALLS), "scope": list(comps), "depth_sec": depth, "components": res,
                          "skipped": {"/".join(k): v for k, v in skipped.items()}}, ensure_ascii=False, indent=1, default=str))
    else:
        print(report(p, comps, res, depth, skipped, a.since))


if __name__ == "__main__":
    main()
