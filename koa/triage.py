#!/usr/bin/env python3
"""KOA 트리아지 — kubernetes MCP 만으로 클러스터 전체를 한 번에 훑어 "이상 징후 요약"을 만든다.

장애 분석의 첫 단계. LLM 이 파드·이벤트를 하나씩 조회하는 대신 이 스크립트가 한 번에 모아
점수순 이상 징후 표 + 변경 타임라인 + 다음에 볼 조회를 40줄 안팎으로 낸다.

  python3 koa/triage.py                          # 최근 1시간, kubernetes MCP (등록된 것, 없으면 카탈로그 정의)
  python3 koa/triage.py --since 3h               # 최근 3시간
  python3 koa/triage.py --at 2026-10-06T05:10Z   # 그 시각까지 (사후 분석; 이벤트 보존 밖이면 파드 status 로만)
  python3 koa/triage.py --ns shop,payment        # 이 네임스페이스만 상세 (노드·변경은 전체)
  python3 koa/triage.py --source kubectl         # MCP 대신 읽기 전용 kubeconfig 로 kubectl 직접 (MCP 등록 전)
  python3 koa/triage.py --json                   # 전체 결과 JSON 을 표준출력으로

전체 결과는 <결과 폴더>/<이름>.triage/<시각>.json 에 저장한다 (다음 분석 단계가 다시 조회하지 않고 참조).
클러스터에는 get/list 만 보낸다. Secret 은 읽지 않는다.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from discover import DEFAULT_KUBECONFIG, Kube, age_text, parse_duration  # noqa: E402
from paths import CATALOG, CLUSTERS  # noqa: E402

try:
    YLoader = yaml.CSafeLoader
except AttributeError:  # libyaml 없는 환경
    YLoader = yaml.SafeLoader

KST = dt.timezone(dt.timedelta(hours=9))
UTC = dt.timezone.utc

# 컨테이너 대기 사유 → (점수, 한글 설명)
BAD_WAIT = {
    "CrashLoopBackOff": (90, "CrashLoopBackOff"),
    "ImagePullBackOff": (85, "이미지 못 받음"),
    "ErrImagePull": (85, "이미지 못 받음"),
    "InvalidImageName": (85, "이미지 이름 오류"),
    "CreateContainerConfigError": (85, "컨테이너 설정 오류(ConfigMap·Secret 참조)"),
    "CreateContainerError": (80, "컨테이너 생성 실패"),
    "RunContainerError": (80, "컨테이너 실행 실패"),
    "ContainerCannotRun": (80, "컨테이너 실행 불가"),
}
# Warning 이벤트 사유 → 점수 (다른 신호에 묶이지 않은 이벤트만 따로 올린다)
EVENT_SCORE = {
    "NodeNotReady": 90, "OOMKilling": 80, "SystemOOM": 80, "Evicted": 70, "FailedScheduling": 70,
    "FailedMount": 70, "FailedAttachVolume": 70, "FailedCreate": 70, "FailedCreatePodSandBox": 70,
    "Unhealthy": 50, "BackOff": 50, "ProbeWarning": 30, "FailedKillPod": 50, "NetworkNotReady": 80,
    "FailedToUpdateEndpoint": 50, "FailedComputeMetricsReplicas": 30, "FailedGetResourceMetric": 20,
}
# 자주 갱신돼 "변경" 으로 보면 안 되는 ConfigMap
NOISY_CM = re.compile(r"(^kube-root-ca\.crt$|lock$|leader|election|-status$|^cluster-autoscaler-status$)")


# ─── 시간 ─────────────────────────────────────────────────────────
def ts(s):
    if not s:
        return None
    try:
        return dt.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def hm(t):
    return t.strftime("%H:%M") if t else "–"


def parse_at(s):
    if not s:
        return dt.datetime.now(UTC)
    s = s.strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d", s):
        s += ":00+00:00"
    t = dt.datetime.fromisoformat(s)
    return (t if t.tzinfo else t.replace(tzinfo=UTC)).astimezone(UTC)


# ─── 수집: kubernetes MCP 또는 kubectl ────────────────────────────
class McpSource:
    """kubernetes MCP 의 kubectl_get 으로 목록을 받는다.
    output=json 은 MCP 가 이름·상태만 남기고 줄이므로(mcp-server-kubernetes 4.1.9) yaml 로 받아 전체 필드를 쓴다.
    MCP 기본 출력 한도(SPAWN_MAX_BUFFER 1MB)는 큰 클러스터에서 넘으므로 이 프로세스에서만 올린다 (등록 설정은 그대로)."""
    label = "kubernetes MCP"

    def __init__(self, timeout):
        from mcp_client import Server, registered
        spec = registered().get("kubernetes") if _hermes_config_exists() else None
        self.registered = bool(spec)
        if not spec:
            spec = yaml.safe_load(CATALOG.read_text())["mcp_servers"]["kubernetes"]["server"]
        spec = dict(spec)
        spec["env"] = dict(spec.get("env") or {}, SPAWN_MAX_BUFFER=str(256 * 1024 * 1024))
        self.kubeconfig = os.path.expanduser(str(spec["env"].get("KUBECONFIG_PATH", DEFAULT_KUBECONFIG))
                                             .replace("${userHome}", str(Path.home())))
        self.server = Server("kubernetes", spec, timeout=timeout)
        self.timeout = timeout

    def __enter__(self):
        self.server.__enter__()
        return self

    def __exit__(self, *exc):
        return self.server.__exit__(*exc)

    def list(self, resource, ns=None):
        args = {"resourceType": resource, "output": "yaml"}
        if ns:
            args["namespace"] = ns
        elif resource != "events":
            args["allNamespaces"] = True
        err, text = self.server.call("kubectl_get", args, timeout=self.timeout)
        if err:
            raise RuntimeError(text.strip()[:200])
        doc = yaml.load(text, Loader=YLoader) or {}
        return doc.get("items") or []


class KubectlSource:
    label = "kubectl (읽기 전용 kubeconfig)"
    registered = False

    def __init__(self, kubeconfig, context):
        if Path(kubeconfig).expanduser().resolve() == (Path.home() / ".kube" / "config").resolve():
            raise SystemExit("~/.kube/config(관리자)로는 실행하지 않는다. 읽기 전용 kubeconfig 를 쓴다.")
        self.kubeconfig = kubeconfig
        self.kube = Kube(kubeconfig, context)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def list(self, resource, ns=None):
        extra = ["--show-managed-fields"] if resource == "configmaps" else []
        return self.kube.json("get", resource, *(["-n", ns] if ns else ["-A"]), *extra).get("items") or []


def _hermes_config_exists():
    from paths import CONFIG
    return CONFIG.is_file()


def collect(src, namespaces):
    """리소스별 목록. 권한·API 가 없으면 limits 에 적고 빈 목록."""
    data, limits = {}, []

    def get(res, ns_list=None, optional=False):
        try:
            if ns_list:
                out = []
                for ns in ns_list:
                    out += src.list(res, ns)
                return out
            return src.list(res)
        except Exception as x:  # noqa: BLE001 — 권한 없음·API 없음·출력 한도 등
            if not optional:
                limits.append("%s 조회 실패: %s" % (res, str(x).splitlines()[0][:160]))
            return None

    data["nodes"] = get("nodes") or []
    for res in ("pods", "events", "deployments.apps", "replicasets.apps", "statefulsets.apps",
                "daemonsets.apps", "jobs.batch", "services"):
        data[res] = get(res, namespaces) or []
    data["controllerrevisions.apps"] = get("controllerrevisions.apps", namespaces, optional=True) or []
    eps = get("endpointslices.discovery.k8s.io", namespaces, optional=True)
    if eps is None:
        eps = get("endpoints", namespaces)
        data["endpoints"] = eps or []
    else:
        data["endpointslices"] = eps
    return data, limits


# ─── 분석 ─────────────────────────────────────────────────────────
class Finding:
    def __init__(self, key, kind):
        self.key, self.kind = key, kind      # key: "ns/Kind/name" 또는 "node/<이름>"
        self.score = 0
        self.status = []                     # 상태 요약 조각
        self.evidence = []                   # 근거 조각
        self.onset = None
        self.pods = []                       # 다음 조회 제안용 (ns, pod, container, restarted)
        self.change = None                   # 직전 변경
        self.on_down_node = set()            # NotReady 노드 위 파드라서 생긴 이상이면 그 노드

    def bump(self, score, status=None, evidence=None, at=None):
        self.score = max(self.score, score) + (5 if self.score and score and status else 0)
        if status and status not in self.status:
            self.status.append(status)
        if evidence and evidence not in self.evidence:
            self.evidence.append(evidence)
        if at and (self.onset is None or at < self.onset):
            self.onset = at


def workload_of(obj, rs_owner):
    """파드·RS·Job → (Kind, name). RS 는 실제 owner(Deployment)로 올린다."""
    refs = obj["metadata"].get("ownerReferences") or []
    if not refs:
        return obj.get("kind") or "Pod", obj["metadata"]["name"]
    kind, name = refs[0]["kind"], refs[0]["name"]
    ns = obj["metadata"].get("namespace")
    if kind == "ReplicaSet":
        return rs_owner.get((ns, name), ("ReplicaSet", name))
    if kind == "Job" and re.search(r"-\d{8,}$", name):
        return "CronJob", name.rsplit("-", 1)[0]
    return kind, name


def analyze(data, at, since, lookback):
    start, change_start = at - since, at - lookback
    found = {}

    def F(key, kind):
        if key not in found:
            found[key] = Finding(key, kind)
        return found[key]

    rs_owner, rs_by_owner = {}, defaultdict(list)
    for rs in data["replicasets.apps"]:
        m = rs["metadata"]
        refs = m.get("ownerReferences") or []
        if refs and refs[0]["kind"] == "Deployment":
            rs_owner[(m["namespace"], m["name"])] = ("Deployment", refs[0]["name"])
            rs_by_owner[(m["namespace"], refs[0]["name"])].append(rs)
    pod_wl = {}

    # 노드
    node_down = set()
    for n in data["nodes"]:
        name = n["metadata"]["name"]
        for c in (n.get("status") or {}).get("conditions") or []:
            t = ts(c.get("lastTransitionTime"))
            if c["type"] == "Ready":
                if c["status"] != "True":
                    node_down.add(name)
                    F("node/" + name, "node").bump(100, "NotReady", "%s: %s" % (c.get("reason", ""), (c.get("message") or "")[:80]), t)
                elif t and start <= t <= at:
                    F("node/" + name, "node").bump(50, "Ready 로 복귀 %s" % hm(t), "그 전까지 NotReady 였을 수 있음", t)
            elif c["type"].endswith("Pressure") and c["status"] == "True":
                F("node/" + name, "node").bump(80, c["type"], (c.get("message") or "")[:80], t)
            elif c["type"] == "NetworkUnavailable" and c["status"] == "True":
                F("node/" + name, "node").bump(90, "NetworkUnavailable", c.get("message", "")[:80], t)
        if (n.get("spec") or {}).get("unschedulable"):
            F("node/" + name, "node").bump(30, "cordon 됨")

    # 파드
    restarts = []   # (finishedAt, ns/kind/name, node, reason, exitCode)
    for p in data["pods"]:
        m, st = p["metadata"], p.get("status") or {}
        ns = m["namespace"]
        kind, wname = workload_of(p, rs_owner)
        key = "%s/%s/%s" % (ns, kind, wname)
        pod_wl[(ns, m["name"])] = key
        node = (p.get("spec") or {}).get("nodeName", "")
        phase = st.get("phase")
        if phase == "Succeeded":
            continue
        f = None

        def ff():
            return F(key, "workload")
        if phase == "Failed":
            ff().bump(60, "파드 %s" % (st.get("reason") or "Failed"), (st.get("message") or "")[:100])
        if phase == "Pending":
            for c in st.get("conditions") or []:
                if c["type"] == "PodScheduled" and c["status"] == "False":
                    f = ff()
                    f.bump(80, "스케줄 안 됨", "%s" % (c.get("message") or "")[:120], ts(c.get("lastTransitionTime")))
        cstats = (st.get("initContainerStatuses") or []) + (st.get("containerStatuses") or [])
        pod_restarted = False
        for cs in cstats:
            w = (cs.get("state") or {}).get("waiting") or {}
            if w.get("reason") in BAD_WAIT:
                sc, desc = BAD_WAIT[w["reason"]]
                f = ff()
                f.bump(sc, desc, (w.get("message") or "")[:120] or None)
            lt = (cs.get("lastState") or {}).get("terminated") or {}
            fin = ts(lt.get("finishedAt"))
            if fin and start <= fin <= at:
                pod_restarted = True
                reason, code = lt.get("reason") or "?", lt.get("exitCode")
                restarts.append((fin, key, node, reason, code))
                f = ff()
                sc = 85 if reason == "OOMKilled" else 50
                f.bump(sc, "OOMKilled" if reason == "OOMKilled" else None, None, fin)
            cur = (cs.get("state") or {}).get("terminated") or {}
            if cur and cur.get("exitCode") not in (0, None) and phase != "Succeeded":
                f = ff()
                f.bump(60, "종료됨 (%s, exit %s)" % (cur.get("reason"), cur.get("exitCode")), None, ts(cur.get("finishedAt")))
        if phase == "Running":
            for c in st.get("conditions") or []:
                t = ts(c.get("lastTransitionTime"))
                if c["type"] == "Ready" and c["status"] == "False" and t and (at - t).total_seconds() > 60:
                    f = ff()
                    f.bump(60, "NotReady", (c.get("message") or c.get("reason") or "")[:100], t)
        if f is not None and node in node_down:
            f.on_down_node.add(node)
        if f is not None or pod_restarted:
            wait = next((((cs.get("state") or {}).get("waiting") or {}).get("reason") for cs in cstats
                         if ((cs.get("state") or {}).get("waiting") or {}).get("reason")), None)
            ctr = next((cs["name"] for cs in cstats if (cs.get("lastState") or {}).get("terminated")
                        or ((cs.get("state") or {}).get("waiting") or {}).get("reason") in BAD_WAIT), None)
            F(key, "workload").pods.append({"ns": ns, "pod": m["name"], "container": ctr, "node": node,
                                            "restarted": pod_restarted, "waiting": wait,
                                            "multi": len(st.get("containerStatuses") or []) > 1})

    # 재시작 근거를 워크로드별로 묶기
    by_wl = defaultdict(list)
    for r in restarts:
        by_wl[r[1]].append(r)
    for key, rs_ in by_wl.items():
        reasons = defaultdict(int)
        for _, _, _, reason, code in rs_:
            reasons["%s exit %s" % (reason, code)] += 1
        found[key].bump(50, None, "기간 안 재시작 %d건 (%s)" % (len(rs_), ", ".join("%s×%d" % kv for kv in reasons.items())))

    # 동시 재시작 = 노드·호스트 단위 사건 의심
    buckets = defaultdict(list)
    for r in restarts:
        buckets[r[0].replace(second=0)].append(r)
    for minute in sorted(buckets):
        rs_ = buckets[minute] + buckets.get(minute + dt.timedelta(minutes=1), [])
        wls = {r[1] for r in rs_}
        if len(wls) >= 3:
            nodes = defaultdict(int)
            for r in rs_:
                nodes[r[2]] += 1
            key = "cluster/동시재시작/%s" % hm(minute)
            if any(k.startswith("cluster/동시재시작/") and abs((found[k].onset - minute).total_seconds()) <= 120 for k in found
                   if found[k].onset):
                continue
            only = [n for n, c in nodes.items() if c == len(rs_)]
            c = F(key, "cluster")
            c.bump(95, "워크로드 %d개 동시 재시작" % len(wls),
                   ("모두 노드 %s → 노드·호스트 사건 의심" % only[0]) if only else
                   "노드 %s → 클러스터 단위 사건 의심" % ", ".join("%s×%d" % kv for kv in nodes.items()), minute)
            # 재시작 말고 다른 이상이 없는 워크로드는 이 한 줄로 접는다
            folded = []
            for wl in sorted(wls):
                w = found.get(wl)
                if w and not w.status and len(by_wl[wl]) == len([r for r in rs_ if r[1] == wl]):
                    folded.append(wl)
                    del found[wl]
            if folded:
                c.evidence.append("대상: %s" % ", ".join(folded))

    # 워크로드 가용성
    def wl_avail(kind, obj, want, ready):
        m = obj["metadata"]
        if want and ready < want:
            key = "%s/%s/%s" % (m["namespace"], kind, m["name"])
            sc = 75 if ready == 0 else 45
            F(key, "workload").bump(sc, "가용 %d/%d" % (ready, want))
    for d in data["deployments.apps"]:
        s = d.get("status") or {}
        wl_avail("Deployment", d, (d.get("spec") or {}).get("replicas", 1), s.get("availableReplicas", 0))
        for c in s.get("conditions") or []:
            if c["type"] == "Progressing" and c.get("reason") == "ProgressDeadlineExceeded":
                key = "%s/Deployment/%s" % (d["metadata"]["namespace"], d["metadata"]["name"])
                F(key, "workload").bump(70, "롤아웃 멈춤", c.get("message", "")[:100], ts(c.get("lastTransitionTime")))
    for s_ in data["statefulsets.apps"]:
        wl_avail("StatefulSet", s_, (s_.get("spec") or {}).get("replicas", 1), (s_.get("status") or {}).get("readyReplicas", 0))
    for d in data["daemonsets.apps"]:
        s = d.get("status") or {}
        want = s.get("desiredNumberScheduled", 0)
        wl_avail("DaemonSet", d, want, want - s.get("numberUnavailable", 0))
    for j in data["jobs.batch"]:
        for c in (j.get("status") or {}).get("conditions") or []:
            t = ts(c.get("lastTransitionTime"))
            if c["type"] == "Failed" and c["status"] == "True" and t and start <= t <= at:
                kind, name = workload_of(j, rs_owner)
                key = "%s/%s/%s" % (j["metadata"]["namespace"], kind, name if kind == "CronJob" else j["metadata"]["name"])
                F(key, "workload").bump(55, "Job 실패", "%s: %s" % (c.get("reason", ""), (c.get("message") or "")[:80]), t)

    # 서비스: 준비된 엔드포인트 0
    ready = defaultdict(int)
    seen_ep = set()
    for e in data.get("endpointslices", []):
        svc = (e["metadata"].get("labels") or {}).get("kubernetes.io/service-name")
        if not svc:
            continue
        k = (e["metadata"]["namespace"], svc)
        seen_ep.add(k)
        for ep in e.get("endpoints") or []:
            if (ep.get("conditions") or {}).get("ready", True):
                ready[k] += 1
    for e in data.get("endpoints", []):
        k = (e["metadata"]["namespace"], e["metadata"]["name"])
        seen_ep.add(k)
        for sub in e.get("subsets") or []:
            ready[k] += len(sub.get("addresses") or [])
    pods_by_ns = defaultdict(list)
    for p in data["pods"]:
        pods_by_ns[p["metadata"]["namespace"]].append(p)
    for s in data["services"]:
        spec, m = s.get("spec") or {}, s["metadata"]
        sel = spec.get("selector")
        k = (m["namespace"], m["name"])
        if not sel or spec.get("type") == "ExternalName" or k not in seen_ep or ready[k]:
            continue
        backing = {pod_wl.get((m["namespace"], p["metadata"]["name"])) for p in pods_by_ns[m["namespace"]]
                   if all((p["metadata"].get("labels") or {}).get(a) == b for a, b in sel.items())}
        backing.discard(None)
        msg = "Service %s 준비된 엔드포인트 0 (트래픽 끊김)" % m["name"]
        if backing:
            for key in backing:
                F(key, "workload").bump(80, "엔드포인트 0", msg)
        else:
            F("%s/Service/%s" % k, "service").bump(65, "엔드포인트 0", "셀렉터 %s 에 맞는 파드 없음" %
                                                  ",".join("%s=%s" % kv for kv in sel.items()))

    # Warning 이벤트: 대상별로 묶어 근거로 붙이고, 묶일 곳 없으면 따로 올린다
    groups = {}
    for e in data["events"]:
        if e.get("type") != "Warning":
            continue
        last = ts(e.get("lastTimestamp")) or ts(e.get("eventTime")) or ts((e.get("series") or {}).get("lastObservedTime"))
        if not last or not (start <= last <= at):
            continue
        io = e.get("involvedObject") or e.get("regarding") or {}
        ns = io.get("namespace") or e["metadata"].get("namespace", "")
        if io.get("kind") == "Pod":
            key = pod_wl.get((ns, io.get("name")), "%s/Pod/%s" % (ns, io.get("name")))
        elif io.get("kind") == "Node":
            key = "node/" + io.get("name", "")
        elif io.get("kind") == "ReplicaSet":
            key = "%s/%s/%s" % ((ns,) + rs_owner.get((ns, io.get("name")), ("ReplicaSet", io.get("name"))))
        else:
            key = "%s/%s/%s" % (ns, io.get("kind"), io.get("name"))
        g = groups.setdefault((key, e.get("reason")), {"count": 0, "first": None, "msg": e.get("message", "")})
        g["count"] += e.get("count") or (e.get("series") or {}).get("count") or 1
        first = ts(e.get("firstTimestamp")) or ts(e.get("eventTime")) or last
        g["first"] = min(g["first"], first) if g["first"] else first
    for (key, reason), g in groups.items():
        ev = "이벤트 %s×%d: %s" % (reason, g["count"], re.sub(r"\s+", " ", g["msg"])[:90])
        if key in found:
            found[key].bump(0, None, ev, g["first"])
        else:
            kind = "node" if key.startswith("node/") else "workload"
            F(key, kind).bump(EVENT_SCORE.get(reason, 35), reason, ev, g["first"])

    # NotReady 노드 위라서 생긴 이상은 노드 사건의 결과 → 노드 쪽 근거로 돌리고 점수를 낮춘다
    for f in found.values():
        if f.on_down_node:
            f.score = min(f.score, 40)
            f.evidence.insert(0, "NotReady 노드 %s 위 파드 (노드 사건의 결과일 가능성)" % ", ".join(sorted(f.on_down_node)))
            for n in f.on_down_node:
                if "node/" + n in found:
                    found["node/" + n].bump(0, None, "영향: %s" % f.key)
        if any(desc in f.status for _, desc in BAD_WAIT.values()):
            f.status = [x for x in f.status if x != "NotReady"]

    # 변경 타임라인
    changes = []
    for (ns, dname), rss in rs_by_owner.items():
        rss = sorted(rss, key=lambda r: int((r["metadata"].get("annotations") or {}).get("deployment.kubernetes.io/revision", 0)))
        for i, rs in enumerate(rss):
            t = ts(rs["metadata"].get("creationTimestamp"))
            rev = (rs["metadata"].get("annotations") or {}).get("deployment.kubernetes.io/revision", "?")
            if not t or not (change_start <= t <= at) or i == 0 and rev == "1":
                continue
            what = "새 ReplicaSet rev %s" % rev
            if i > 0:
                diff = image_diff(rss[i - 1], rs)
                what += " (%s)" % diff if diff else " (이미지 같음 → 설정·env 변경)"
            changes.append({"at": t, "ns": ns, "target": "Deployment/%s" % dname, "what": what})
    for cr in data["controllerrevisions.apps"]:
        t = ts(cr["metadata"].get("creationTimestamp"))
        refs = cr["metadata"].get("ownerReferences") or []
        if t and change_start <= t <= at and refs and cr.get("revision", 1) > 1:
            changes.append({"at": t, "ns": cr["metadata"]["namespace"], "target": "%s/%s" % (refs[0]["kind"], refs[0]["name"]),
                            "what": "새 revision %s" % cr.get("revision")})
    for n in data["nodes"]:
        t = ts(n["metadata"].get("creationTimestamp"))
        if t and change_start <= t <= at:
            changes.append({"at": t, "ns": "", "target": "Node/%s" % n["metadata"]["name"], "what": "노드 추가"})
    for c in data.get("configmaps", []):
        m = c["metadata"]
        if NOISY_CM.search(m["name"]):
            continue
        # 수정 시각은 managedFields 에만 있다. kubernetes MCP 는 kubectl 기본값대로 managedFields 를 빼고 주므로
        # 그때는 생성 시각만 본다 (확인 못 한 것에 적는다)
        times = [x for x in (ts(f_.get("time")) for f_ in m.get("managedFields") or []) if x]
        t, what = (max(times), "수정") if times else (ts(m.get("creationTimestamp")), "생성")
        if t and change_start <= t <= at:
            changes.append({"at": t, "ns": m["namespace"], "target": "ConfigMap/%s" % m["name"], "what": what})
    changes.sort(key=lambda c: c["at"])

    # 이상 징후마다 같은 네임스페이스의 직전 변경을 붙인다 (자기 자신 우선)
    for f in found.values():
        if f.kind != "workload":
            continue
        ns, kind, name = f.key.split("/", 2)
        onset = f.onset or at
        cands = [c for c in changes if c["ns"] == ns and c["at"] <= onset + dt.timedelta(minutes=2)]
        own = [c for c in cands if c["target"] == "%s/%s" % (kind, name)]
        pick = (own or cands)[-1:] if (own or cands) else []
        if pick:
            f.change = pick[0]
            f.score += 10 if own else 5

    return sorted(found.values(), key=lambda f: (-f.score, f.onset or at)), changes, node_down


def image_diff(old, new):
    def imgs(rs):
        return {c["name"]: c.get("image", "") for c in (((rs.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []}
    a, b = imgs(old), imgs(new)
    out = []
    for k in b:
        if a.get(k) and a[k] != b[k]:
            out.append("%s %s→%s" % (k, a[k].rsplit("/", 1)[-1], b[k].rsplit("/", 1)[-1]))
    return ", ".join(out)


def next_steps(findings, limit=4):
    """상위 이상 징후마다 kubernetes MCP 로 바로 볼 조회."""
    out = []
    for f in findings:
        if len(out) >= limit:
            break
        if f.kind == "node":
            out.append("노드 %s: `python3 koa/query.py kubernetes describe kind=nodes name=%s`" % (f.key[5:], f.key[5:]))
        elif f.kind == "workload" and f.pods:
            p = sorted(f.pods, key=lambda x: (not x["restarted"], x["pod"]))[0]
            args = {"resourceType": "pod", "name": p["pod"], "namespace": p["ns"], "tail": 50}
            if p["container"] and p["multi"]:
                args["container"] = p["container"]
            if p["restarted"] or p["waiting"] == "CrashLoopBackOff":
                args["previous"] = True
                out.append("%s 이전 컨테이너 로그: `python3 koa/query.py --call kubernetes kubectl_logs '%s'`"
                           % (f.key, json.dumps(args, ensure_ascii=False)))
            elif p["waiting"] in ("ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError", None):
                out.append("%s 파드 상세: `python3 koa/query.py kubernetes describe name=%s ns=%s`" % (f.key, p["pod"], p["ns"]))
            else:
                out.append("%s 로그: `python3 koa/query.py --call kubernetes kubectl_logs '%s'`" % (f.key, json.dumps(args, ensure_ascii=False)))
        elif f.kind == "service":
            ns, _, name = f.key.split("/", 2)
            out.append("%s: `python3 koa/query.py kubernetes describe kind=services name=%s ns=%s`" % (f.key, name, ns))
    return out


# ─── 출력 ─────────────────────────────────────────────────────────
def render(cluster, at, since, lookback, src, secs, findings, changes, limits, top):
    def cell(s):
        return s.replace("|", "\\|").replace("\n", " ")
    lines = ["# 트리아지 %s · %sZ (%s KST) · 최근 %s · %s · %.1f초" % (
        cluster, at.strftime("%Y-%m-%d %H:%M"), at.astimezone(KST).strftime("%H:%M"), age_text(since.total_seconds()),
        src, secs)]
    n_kind = defaultdict(int)
    for f in findings:
        n_kind[f.kind] += 1
    lines.append("이상 %d건 (%s) · 최근 %s 변경 %d건" % (
        len(findings), ", ".join("%s %d" % (dict(workload="워크로드", node="노드", service="서비스", cluster="클러스터")[k], v)
                                 for k, v in n_kind.items()) or "없음", age_text(lookback.total_seconds()), len(changes)))
    if findings:
        lines += ["", "## 이상 징후 (점수순, 시각 UTC)", "| # | 대상 | 상태 | 시작 | 근거 | 직전 변경 |", "|---|---|---|---|---|---|"]
        for i, f in enumerate(findings[:top], 1):
            ch = "%s %s %s" % (hm(f.change["at"]), f.change["target"], f.change["what"]) if f.change else "–"
            lines.append("| %d | %s | %s | %s | %s | %s |" % (
                i, cell(f.key), cell(", ".join(f.status) or "–"), hm(f.onset), cell("; ".join(f.evidence[:2]) or "–"), cell(ch)))
        if len(findings) > top:
            lines.append("(그 외 %d건은 JSON 에)" % (len(findings) - top))
    else:
        lines += ["", "이상 징후 없음: 비정상 파드·노드·엔드포인트 0 서비스·Warning 이벤트가 이 기간에 없다."]
    if changes:
        lines += ["", "## 변경 타임라인 (최근 %s)" % age_text(lookback.total_seconds())]
        for c in changes[-10:]:
            lines.append("- %s %s %s %s" % (hm(c["at"]), c["ns"] or "-", c["target"], c["what"]))
        if len(changes) > 10:
            lines.append("- (앞의 %d건은 JSON 에)" % (len(changes) - 10))
    steps = next_steps(findings)
    if steps:
        lines += ["", "## 다음 확인"] + ["%d. %s" % (i, s) for i, s in enumerate(steps, 1)]
    lines += ["", "## 확인 못 한 것"] + ["- %s" % x for x in limits]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default="1h", help="이 기간의 이상을 본다 (기본 1h)")
    ap.add_argument("--at", default=None, help="기간의 끝 시각 (UTC, 기본 지금). 예: 2026-10-06T05:10Z")
    ap.add_argument("--lookback", default="6h", help="변경 타임라인 기간 (기본 6h, --since 보다 짧으면 --since)")
    ap.add_argument("--ns", default=None, help="상세 조회할 네임스페이스 (쉼표). 기본 전체")
    ap.add_argument("--source", choices=["mcp", "kubectl"], default="mcp")
    ap.add_argument("--kubeconfig", default=os.environ.get("KOA_KUBECONFIG", str(DEFAULT_KUBECONFIG)))
    ap.add_argument("--context", default=None)
    ap.add_argument("--name", default=None, help="클러스터 이름 (기본: 컨텍스트에서 -readonly 를 뺀 것)")
    ap.add_argument("--top", type=int, default=12, help="표에 보일 이상 징후 수 (기본 12)")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--json", action="store_true", help="전체 결과 JSON 을 표준출력으로")
    ap.add_argument("--no-save", action="store_true")
    a = ap.parse_args()

    at = parse_at(a.at)
    since = dt.timedelta(seconds=parse_duration(a.since) or 3600)
    lookback = max(since, dt.timedelta(seconds=parse_duration(a.lookback) or 21600))
    namespaces = [x for x in (a.ns or "").split(",") if x] or None

    t0 = time.time()
    src = McpSource(a.timeout) if a.source == "mcp" else KubectlSource(a.kubeconfig, a.context)
    try:
        with src:
            data, limits = collect(src, namespaces)
            findings, changes, _ = analyze(data, at, since, lookback)
            # 설정 회귀를 보려고, 이상 있는 네임스페이스의 ConfigMap 만 추가로 읽는다 (전체는 크다)
            hot = sorted({f.key.split("/", 1)[0] for f in findings if f.kind in ("workload", "service")})
            if hot:
                cms = []
                for ns in hot:
                    try:
                        cms += src.list("configmaps", ns)
                    except Exception as x:  # noqa: BLE001
                        limits.append("configmaps(%s) 조회 실패: %s" % (ns, str(x).splitlines()[0][:120]))
                        break
                data["configmaps"] = cms
                findings, changes, _ = analyze(data, at, since, lookback)
    except Exception as x:  # noqa: BLE001
        sys.exit("트리아지 실패 (%s): %s" % (src.label, x))
    secs = time.time() - t0

    try:
        ctx = a.context or (yaml.safe_load(Path(src.kubeconfig).expanduser().read_text()) or {}).get("current-context", "")
    except OSError:
        ctx = ""
    cluster = a.name or re.sub(r"-readonly$", "", ctx or "cluster")
    if a.source == "mcp" and not src.registered:
        limits.insert(0, "kubernetes MCP 가 이 프로필에 등록돼 있지 않아 카탈로그 정의로 띄웠다")
    if (at - dt.datetime.now(UTC)).total_seconds() < -3600:
        limits.append("과거 시각 분석: k8s 이벤트는 보존 기간(보통 1h) 밖이면 없다. 재시작 이력은 파드별 마지막 1회만 남는다")
    else:
        limits.append("k8s 이벤트는 보존 기간(보통 1h) 안의 것만, 재시작 이력은 파드별 마지막 1회만 남는다")
    if "configmaps" not in data:
        limits.append("ConfigMap 변경은 이상 있는 네임스페이스가 없어 보지 않았다")
    elif a.source == "mcp":
        limits.append("ConfigMap 은 생성 시각만 봤다 (kubernetes MCP 출력에 수정 시각(managedFields)이 없다. --source kubectl 이면 수정도 본다)")
    limits.append("지표·로그 저장소 없이 k8s 상태만 봤다: 지연·오류율 증가처럼 파드가 멀쩡한 장애는 여기 안 보인다")

    result = {
        "schema": "koa.triage/v1", "cluster": cluster, "at": at.isoformat(), "since_seconds": since.total_seconds(),
        "source": src.label, "seconds": round(secs, 1),
        "counts": {k: len(v) for k, v in data.items()},
        "findings": [{"key": f.key, "kind": f.kind, "score": f.score, "status": f.status, "evidence": f.evidence,
                      "onset": f.onset.isoformat() if f.onset else None, "pods": f.pods,
                      "change": dict(f.change, at=f.change["at"].isoformat()) if f.change else None} for f in findings],
        "changes": [dict(c, at=c["at"].isoformat()) for c in changes],
        "limits": limits,
    }
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(render(cluster, at, since, lookback, src.label, secs, findings, changes, limits, a.top))
    if not a.no_save:
        out = CLUSTERS / ("%s.triage" % cluster) / ("%s.json" % at.strftime("%Y%m%dT%H%M%SZ"))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=1))
        if not a.json:
            print("\n전체 결과: %s" % str(out).replace(str(Path.home()), "~", 1))


if __name__ == "__main__":
    main()
