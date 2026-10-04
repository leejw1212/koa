#!/usr/bin/env python3
"""KOA 1단계 — 클러스터 탐색.

연결한 클러스터를 읽기 전용 kubeconfig 로 훑어서 "클러스터 프로필"(clusters/<이름>.yaml)을 만든다.
  - 무엇에 접근할 수 있나 : 권한, API, 관측 구성요소(지표·로그·트레이스·GitOps·MQ ...)와 접근 주소
  - 무엇이 비어 있나      : 원인 분석에 필요한데 없는 신호(gaps)

클러스터에는 읽기(get/list) 요청만 보낸다. --probe 를 주면 찾아낸 접근 주소에 GET 을 한 번씩 보낸다.

  python3 koa/discover.py                      # 기본: ~/.kube/hermes-readonly.yaml, 현재 컨텍스트
  python3 koa/discover.py --probe              # 접근 주소 GET 확인 포함
  python3 koa/discover.py --kubeconfig X --context Y --name prod-a
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
CATALOG = REPO / "koa" / "catalog.yaml"
DEFAULT_KUBECONFIG = Path.home() / ".kube" / "hermes-readonly.yaml"

# 원인 분석에 필요한 읽기 권한과, 읽기 전용 계정이라면 막혀 있어야 하는 권한
READ_CHECKS = [
    ("list", "pods", True),
    ("get", "pods/log", True),
    ("list", "events", True),
    ("list", "nodes", False),
    ("list", "services", True),
    ("list", "ingresses.networking.k8s.io", True),
    ("list", "deployments.apps", True),
    ("list", "configmaps", True),
    ("get", "nodes.metrics.k8s.io", False),
]
SHOULD_BE_DENIED = [
    ("list", "secrets", True),
    ("create", "pods/exec", True),
    ("create", "pods/portforward", True),
    ("get", "services/proxy", True),
    ("create", "pods", True),
    ("delete", "pods", True),
    ("patch", "deployments.apps", True),
]


class Kube:
    def __init__(self, kubeconfig, context):
        self.base = ["kubectl", "--kubeconfig", str(kubeconfig), "--request-timeout=20s"]
        if context:
            self.base += ["--context", context]

    def run(self, *args, check=True):
        p = subprocess.run(self.base + list(args), capture_output=True, text=True)
        if check and p.returncode != 0:
            raise RuntimeError("kubectl %s: %s" % (" ".join(args), p.stderr.strip()[:300]))
        return p

    def json(self, *args):
        return json.loads(self.run(*args, "-o", "json").stdout)

    def can_i(self, verb, resource, all_ns):
        # 'pods/log' 를 그대로 넘기면 kubectl 은 "이름이 log 인 pods" 로 해석한다 → --subresource 로 분리
        res, _, sub = resource.partition("/")
        args = ["auth", "can-i", verb, res] + (["--subresource", sub] if sub else []) + (["--all-namespaces"] if all_ns else [])
        return self.run(*args, check=False).stdout.strip() == "yes"


def image_name(image):
    """'docker.io/library/redis:7@sha256:..' -> 'library/redis', tag '7'"""
    ref = image.split("@", 1)[0]
    tag = ""
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        ref, tag = ref.rsplit(":", 1)
    parts = ref.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        parts = parts[1:]
    return "/".join(parts), tag


def owner_of(pod):
    refs = pod["metadata"].get("ownerReferences") or []
    if not refs:
        return "Pod", pod["metadata"]["name"]
    kind, name = refs[0]["kind"], refs[0]["name"]
    if kind == "ReplicaSet":
        return "Deployment", name.rsplit("-", 1)[0]
    if kind == "Job" and re.search(r"-\d{8,}$", name):
        return "CronJob", name.rsplit("-", 1)[0]
    return kind, name


def selector_matches(selector, labels):
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


def clean_path(path):
    """정규식 Ingress 경로 '/opensearch(/|$)(.*)' -> '/opensearch'"""
    p = re.split(r"[(\[\\$^*?+]", path or "/", 1)[0]
    return p.rstrip("/") or ""


def age_text(seconds):
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    if seconds < 86400 * 2:
        return "%.1fh" % (seconds / 3600)
    return "%.1fd" % (seconds / 86400)


def parse_ts(s):
    if not s:
        return None
    try:
        return dt.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def probe(url):
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "koa-discover"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:  # 접속 실패
        return {"url": url, "reachable": False, "error": str(e)[:120]}
    return {
        "url": url,
        "reachable": code < 500,
        "http_status": code,
        "auth_required": code in (401, 403),
    }


def discover(kube, catalog, do_probe):
    now = dt.datetime.now(dt.timezone.utc)
    warnings = []

    # ── 기본 정보 ─────────────────────────────────────────
    ctx = kube.run("config", "current-context").stdout.strip()
    ver = json.loads(kube.run("version", "-o", "json").stdout)
    who = kube.run("auth", "whoami", "-o", "json", check=False)
    identity = None
    if who.returncode == 0:
        identity = json.loads(who.stdout).get("status", {}).get("userInfo", {}).get("username")

    nodes = kube.json("get", "nodes")["items"]
    roles, providers, runtimes = {}, set(), set()
    ready = 0
    for n in nodes:
        rs = [k.split("/", 1)[1] for k in n["metadata"].get("labels", {}) if k.startswith("node-role.kubernetes.io/")] or ["worker"]
        for r in rs:
            roles[r] = roles.get(r, 0) + 1
        pid = n.get("spec", {}).get("providerID", "")
        providers.add(pid.split("://", 1)[0] if "://" in pid else "unknown")
        runtimes.add(n["status"]["nodeInfo"]["containerRuntimeVersion"].split("://", 1)[0])
        if any(c["type"] == "Ready" and c["status"] == "True" for c in n["status"].get("conditions", [])):
            ready += 1

    # ── 권한 (병렬) ─────────────────────────────────────────
    checks = READ_CHECKS + SHOULD_BE_DENIED
    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(lambda c: kube.can_i(*c), checks))
    perm = {"%s %s" % (v, r): ok for (v, r, _), ok in zip(checks, results)}
    can = [k for k, ok in perm.items() if ok]
    cannot = [k for k, ok in perm.items() if not ok]
    leaked = ["%s %s" % (v, r) for (v, r, _) in SHOULD_BE_DENIED if perm["%s %s" % (v, r)]]
    if leaked:
        warnings.append("읽기 전용이 아니다: %s 가 허용됨. admin kubeconfig 를 쓰고 있지 않은지 확인" % ", ".join(leaked))

    # ── API 그룹 ───────────────────────────────────────────
    groups = sorted({g.split("/")[0] for g in kube.run("api-versions").stdout.split() if "/" in g})
    metrics_api = kube.run("get", "--raw", "/apis/metrics.k8s.io/v1beta1/nodes", check=False).returncode == 0

    # ── 워크로드 → 구성요소 ─────────────────────────────────
    pods = kube.json("get", "pods", "--all-namespaces")["items"]
    services = kube.json("get", "services", "--all-namespaces")["items"]
    try:
        ingresses = kube.json("get", "ingresses.networking.k8s.io", "--all-namespaces")["items"]
    except RuntimeError:
        ingresses = []

    comp_rules = catalog["components"]
    compiled = {name: re.compile(r["image"]) for name, r in comp_rules.items() if r.get("image")}
    instances = {}  # (comp, ns, workload) -> dict
    for pod in pods:
        ns = pod["metadata"]["namespace"]
        labels = pod["metadata"].get("labels", {})
        kind, wl = owner_of(pod)
        statuses = {s["name"]: s for s in pod.get("status", {}).get("containerStatuses", [])}
        for c in pod["spec"].get("containers", []):
            name, tag = image_name(c["image"])
            for comp, rx in compiled.items():
                if not rx.search(name):
                    continue
                key = (comp, ns, wl)
                inst = instances.setdefault(key, {
                    "namespace": ns, "workload": "%s/%s" % (kind, wl), "image": "%s:%s" % (name, tag) if tag else name,
                    "pods": 0, "ready": 0, "_labels": [], "services": [], "access": [],
                })
                inst["pods"] += 1
                inst["ready"] += 1 if statuses.get(c["name"], {}).get("ready") else 0
                inst["_labels"].append(labels)

    # 서비스: 구성요소 파드를 선택하는 서비스
    svc_owner = {}  # (ns, svc) -> key
    for s in services:
        ns, sname = s["metadata"]["namespace"], s["metadata"]["name"]
        sel = s["spec"].get("selector") or {}
        for key, inst in instances.items():
            if inst["namespace"] == ns and any(selector_matches(sel, l) for l in inst["_labels"]):
                ports = ["%s:%s" % (p.get("name", ""), p["port"]) if p.get("name") else str(p["port"]) for p in s["spec"].get("ports", [])]
                inst["services"].append("%s (%s)" % (sname, ", ".join(ports)))
                svc_owner[(ns, sname)] = key

    # Ingress: 클러스터 밖에서 들어오는 주소
    for ing in ingresses:
        ns = ing["metadata"]["namespace"]
        tls_hosts = {h for t in ing["spec"].get("tls", []) or [] for h in t.get("hosts", [])}
        lb = (ing.get("status", {}).get("loadBalancer", {}).get("ingress") or [{}])[0]
        for rule in ing["spec"].get("rules", []) or []:
            host = rule.get("host") or lb.get("hostname") or lb.get("ip")
            if not host:
                continue
            scheme = "https" if host in tls_hosts else "http"
            for p in (rule.get("http") or {}).get("paths", []):
                svc = (p.get("backend", {}).get("service") or {}).get("name")
                key = svc_owner.get((ns, svc))
                if key:
                    instances[key]["access"].append("%s://%s%s" % (scheme, host, clean_path(p.get("path"))))

    components = {}
    for (comp, ns, wl), inst in sorted(instances.items()):
        inst.pop("_labels")
        inst["detected_by"] = "image"
        if not inst["services"]:
            inst.pop("services")
        if inst["access"] and do_probe and comp_rules[comp].get("probe"):
            inst["probe"] = [probe(u.rstrip("/") + comp_rules[comp]["probe"]) for u in inst["access"]]
        if not inst["access"]:
            inst["access"] = None  # 클러스터 밖 접근 주소 없음
        c = components.setdefault(comp, {"kind": comp_rules[comp]["kind"], "instances": []})
        c["instances"].append(inst)
    for comp, r in comp_rules.items():
        crd = r.get("crd")
        if crd and any(g == crd or g.endswith("." + crd) for g in groups):
            c = components.setdefault(comp, {"kind": r["kind"], "instances": []})
            c["crd_group"] = [g for g in groups if g == crd or g.endswith("." + crd)]

    # ── 이벤트 보존 ─────────────────────────────────────────
    ev = kube.run("get", "events", "--all-namespaces", "--no-headers",
                  "-o", "custom-columns=L:.lastTimestamp,E:.eventTime,F:.firstTimestamp").stdout.split("\n")
    stamps = []
    for line in ev:
        for f in line.split():
            t = parse_ts(f)
            if t:
                stamps.append(t)
                break
    events = {"count": len(stamps)}
    if stamps:
        events["oldest_age"] = age_text((now - min(stamps)).total_seconds())

    # ── 빈 곳 ─────────────────────────────────────────────
    kinds = {c["kind"] for c in components.values()}
    gaps = []
    if not kinds & {"metrics"}:
        gaps.append("지표 저장소 없음 → '언제부터·얼마나' 추세, 큐 적체, 처리량을 볼 수 없다")
    if not kinds & {"logs"}:
        gaps.append("로그 저장소 없음 → kubectl logs 만 가능, 재시작 전·삭제된 파드 로그는 볼 수 없다")
    elif not kinds & {"log-shipper", "telemetry-collector"}:
        gaps.append("로그 저장소는 있지만 수집기를 찾지 못함 → 어떤 로그가 들어가는지 확인 필요")
    if kinds & {"log-shipper"}:
        gaps.append("로그 수집기 자체 상태(버퍼·재시도·heartbeat)를 볼 신호가 있는지 확인 필요 → '로그 없음'과 '수집 중단'을 구분하기 위해")
    if not stamps or (now - min(stamps)).total_seconds() < 6 * 3600:
        gaps.append("k8s 이벤트 보존이 짧음(%s) → 사후 분석 때 재시작·OOM·스케줄링 이력이 사라진다" % events.get("oldest_age", "없음"))
    if not kinds & {"gitops"}:
        gaps.append("GitOps 없음 → 변경 이력은 ReplicaSet revision·생성 시각으로만 복원")
    if not metrics_api:
        gaps.append("metrics API 없음 → 지금 CPU·메모리 사용량 조회 불가")
    if not kinds & {"traces"}:
        gaps.append("트레이스 없음 → 서비스 간 어느 구간에서 느려지는지는 로그의 request_id 로만 추적")
    if not kinds & {"alerting"}:
        gaps.append("경보 시스템 없음 → 장애를 먼저 알려줄 신호가 없다")
    no_access = sorted(n for n, c in components.items()
                       if c["kind"] in ("metrics", "logs", "dashboard", "gitops", "message-queue", "alerting")
                       and c["instances"] and not any(i["access"] for i in c["instances"]))
    if no_access:
        gaps.append("클러스터 밖 접근 주소 없음: %s → MCP 를 붙이려면 Ingress 등으로 열어야 한다 (읽기 전용 SA 는 port-forward 불가)" % ", ".join(no_access))

    return {
        "schema": "koa.cluster-profile/v1",
        "discovered_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kubeconfig": {"context": ctx, "identity": identity},
        "warnings": warnings or None,
        "kubernetes": {
            "version": ver.get("serverVersion", {}).get("gitVersion"),
            "provider": sorted(providers),
            "runtime": sorted(runtimes),
            "nodes": {"total": len(nodes), "ready": ready, "roles": roles},
            "namespaces": len({p["metadata"]["namespace"] for p in pods}),
            "pods": len(pods),
        },
        "access": {"can": can, "cannot": cannot, "read_only": not leaked},
        "apis": {"metrics_api": metrics_api},
        "components": components,
        "events": events,
        "gaps": gaps,
    }


def summary(p):
    out = []
    k = p["kubernetes"]
    out.append("클러스터 %s  (%s, 노드 %d/%d Ready, 파드 %d)" % (p["kubeconfig"]["context"], k["version"], k["nodes"]["ready"], k["nodes"]["total"], k["pods"]))
    out.append("계정 %s  읽기전용=%s" % (p["kubeconfig"]["identity"], p["access"]["read_only"]))
    for w in p["warnings"] or []:
        out.append("경고: " + w)
    out.append("\n구성요소")
    for name, c in p["components"].items():
        insts = c["instances"]
        where = ", ".join("%s/%s" % (i["namespace"], i["workload"].split("/", 1)[1]) for i in insts) or "(CRD 만)"
        acc = sorted({a for i in insts for a in (i["access"] or [])})
        out.append("  %-18s %-20s %s%s" % (name, c["kind"], where, ("  → " + ", ".join(acc)) if acc else ""))
        for i in insts:
            for pr in i.get("probe", []):
                out.append("  %18s probe %s → %s" % ("", pr["url"], pr.get("http_status", pr.get("error"))))
    out.append("\n빈 곳")
    out += ["  - " + g for g in p["gaps"]]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kubeconfig", default=os.environ.get("KOA_KUBECONFIG", str(DEFAULT_KUBECONFIG)))
    ap.add_argument("--context", default=None)
    ap.add_argument("--name", default=None, help="프로필 이름 (기본: 컨텍스트 이름에서 -readonly 를 뺀 것)")
    ap.add_argument("--probe", action="store_true", help="찾아낸 접근 주소에 GET 을 보내 응답을 확인한다")
    ap.add_argument("--out", default=None, help="저장 경로 (기본: clusters/<이름>.yaml)")
    a = ap.parse_args()

    if Path(a.kubeconfig).expanduser().resolve() == (Path.home() / ".kube" / "config").resolve():
        sys.exit("~/.kube/config(관리자용)로는 실행하지 않는다. 읽기 전용 kubeconfig 를 지정하라.")

    catalog = yaml.safe_load(CATALOG.read_text())
    kube = Kube(Path(a.kubeconfig).expanduser(), a.context)
    profile = discover(kube, catalog, a.probe)

    name = a.name or re.sub(r"-readonly$", "", profile["kubeconfig"]["context"])
    profile = {"cluster": name, **profile}
    out = Path(a.out) if a.out else REPO / "clusters" / ("%s.yaml" % name)
    out.parent.mkdir(parents=True, exist_ok=True)
    header = "# KOA 클러스터 프로필 — koa/discover.py 가 만든 파일. 직접 고치지 말고 다시 실행한다.\n"
    out.write_text(header + yaml.safe_dump(profile, sort_keys=False, allow_unicode=True, width=200))
    print(summary(profile))
    print("\n저장: %s\n다음: python3 koa/plan.py %s" % (out, name))


if __name__ == "__main__":
    main()
