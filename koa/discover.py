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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import CATALOG, CLUSTERS  # noqa: E402
DEFAULT_KUBECONFIG = Path.home() / ".kube" / "hermes-readonly.yaml"

# 원인 분석에 필요한 읽기 권한과, 읽기 전용 계정이라면 막혀 있어야 하는 권한
READ_CHECKS = [
    ("list", "pods", True),
    ("get", "pods/log", True),
    ("list", "events", True),
    ("list", "nodes", False),
    ("list", "services", True),
    ("list", "ingresses.networking.k8s.io", True),
    ("list", "httproutes.gateway.networking.k8s.io", True),
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


def parse_duration(s):
    """'1h30m', '2h', '45m', '90s' -> 초"""
    total, units = 0, {"h": 3600, "m": 60, "s": 1}
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)(h|m|s)", s or ""):
        total += float(num) * units[unit]
    return int(total) or None


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
    try:
        httproutes = kube.json("get", "httproutes.gateway.networking.k8s.io", "--all-namespaces")["items"]
    except RuntimeError:
        httproutes = []

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
    route_backends = set()  # (ns, svc) — 라우팅(Ingress/HTTPRoute)이 가리키는 백엔드 Service
    route_hosts = {}  # (ns, svc) -> [https://host/경로, ...] — 백엔드 워크로드에 붙일 외부 주소
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
                if svc:
                    route_backends.add((ns, svc))
                    route_hosts.setdefault((ns, svc), []).append("%s://%s%s" % (scheme, host, clean_path(p.get("path"))))
                key = svc_owner.get((ns, svc))
                if key:
                    instances[key]["access"].append("%s://%s%s" % (scheme, host, clean_path(p.get("path"))))

    # Gateway API HTTPRoute: 클러스터 밖에서 들어오는 주소 (ingress 대신 gateway/httproute 를 쓰는 클러스터)
    for hr in httproutes:
        ns = hr["metadata"]["namespace"]
        for host in hr["spec"].get("hostnames", []) or []:
            for rule in hr["spec"].get("rules", []) or []:
                path = ""
                ms = rule.get("matches") or []
                if ms and (ms[0].get("path") or {}).get("value"):
                    path = clean_path(ms[0]["path"]["value"])
                for br in rule.get("backendRefs", []) or []:
                    if br.get("group", "core") not in ("", "core"):
                        continue  # Service 가 아닌 백엔드(예: 다른 HTTPRoute)는 접근 주소 근거로 삼지 않는다
                    svc = br.get("name")
                    if not svc:
                        continue
                    route_backends.add((ns, svc))
                    route_hosts.setdefault((ns, svc), []).append("https://%s%s" % (host, path))
                    key = svc_owner.get((ns, svc))
                    if key:
                        instances[key]["access"].append("https://%s%s" % (host, path))

    # 라우팅(Ingress/HTTPRoute)이 가리키는 백엔드 Service 뒤의 애플리케이션 워크로드.
    # 카탈로그 이미지 규칙(redis 등 인프라)에 안 잡힌 Service = 외부로 열린 백엔드 앱 파드다.
    if catalog["components"].get("app-workload"):
        svc_selector = {}
        for s in services:
            sel = s["spec"].get("selector") or {}
            if sel:
                svc_selector[(s["metadata"]["namespace"], s["metadata"]["name"])] = sel
        owned_wl = {(ns, wl) for (comp, ns, wl) in instances}
        app_inst = {}  # (ns, wl) -> inst  (wl = 'Kind/name', comp='app-workload')
        for (ns, svc) in sorted(route_backends):
            if (ns, svc) in svc_owner:
                continue  # 이미 카탈로그 인프라 컴포넌트로 잡힘
            sel = svc_selector.get((ns, svc))
            if not sel:
                continue
            for pod in pods:
                if pod["metadata"]["namespace"] != ns or not selector_matches(sel, pod["metadata"].get("labels", {})):
                    continue
                kind, wl = owner_of(pod)
                if (ns, wl) in owned_wl:
                    continue
                key = (ns, wl)
                inst = app_inst.setdefault(key, {
                    "namespace": ns, "workload": "%s/%s" % (kind, wl), "image": None,
                    "pods": 0, "ready": 0, "services": [], "access": list(route_hosts.get((ns, svc), [])),
                })
                inst["pods"] += 1
                pod_ready = any(cs.get("ready") for cs in pod.get("status", {}).get("containerStatuses", []))
                inst["ready"] += 1 if pod_ready else 0
        for (ns, wl), inst in app_inst.items():
            inst["access"] = sorted(set(inst["access"])) or None
            instances[("app-workload", ns, wl)] = inst

    # Gateway API HTTPRoute 자체를 라우팅 계층으로 노출 (네임스페이스/이름/hostname + 백엔드 개수)
    if catalog["components"].get("gateway-httproute"):
        for hr in httproutes:
            ns = hr["metadata"]["namespace"]
            hname = hr["metadata"]["name"]
            hosts = hr["spec"].get("hostnames", []) or []
            nb = sum(len(r.get("backendRefs", []) or []) for r in hr["spec"].get("rules", []) or [])
            inst = {
                "namespace": ns, "workload": "HTTPRoute/%s" % hname, "image": None,
                "pods": 0, "ready": 0, "services": [], "access": sorted(set("https://%s" % h for h in hosts)) or None,
                "hostnames": hosts, "backend_refs": nb,
            }
            instances[("gateway-httproute", ns, hname)] = inst

    components = {}
    for (comp, ns, wl), inst in sorted(instances.items()):
        inst.pop("_labels", None)
        inst["detected_by"] = "routing" if comp in ("app-workload", "gateway-httproute") else "image"
        if not inst.get("image"):
            inst.pop("image", None)  # 라우팅 감지 구성요소는 이미지 규칙이 없어 image 를 표시하지 않는다
        if not inst.get("services"):
            inst.pop("services", None)
        if inst.get("access") and do_probe and comp_rules[comp].get("probe"):
            inst["probe"] = [probe(u.rstrip("/") + comp_rules[comp]["probe"]) for u in inst["access"]]
        if not inst.get("access"):
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
    events = {"count": len(stamps)}  # type: dict
    if stamps:
        events["oldest_age"] = age_text((now - min(stamps)).total_seconds())
    # 보존 기간을 남은 이벤트 나이로 추정하면 틀린다. kind-lab 은 TTL 1h(기본값)인데 11시간 된 이벤트가 남아 있었다
    # (원인 미확인 — 노트북 절전/docker 일시정지로 etcd lease 시간이 멈췄을 가능성).
    # API 서버 설정(--event-ttl, 기본 1h)을 볼 수 있으면 그 값을 쓴다. 관리형(EKS/GKE/AKS)은 API 서버 파드가 안 보인다.
    ttl_sec, events["ttl_source"] = None, "unknown"
    api = kube.run("get", "pods", "-n", "kube-system", "-l", "component=kube-apiserver", "-o", "json", check=False)
    if api.returncode == 0 and json.loads(api.stdout).get("items"):
        cmd = json.loads(api.stdout)["items"][0]["spec"]["containers"][0].get("command", [])
        flag = next((c.split("=", 1)[1] for c in cmd if c.startswith("--event-ttl=")), None)
        ttl_sec = parse_duration(flag) if flag else 3600
        events["ttl"] = flag or "1h"
        events["ttl_source"] = "kube-apiserver --event-ttl" if flag else "kube-apiserver 기본값 (--event-ttl 없음)"

    # ── Prometheus 수집 범위 (--probe 이고 인증 없이 열려 있을 때만) ─────────
    coverage = metrics_coverage(components) if do_probe else None

    # ── 빈 곳: id 는 catalog.yaml 의 gap_advice 와 짝이다 (report.py 가 제안으로 바꾼다) ──
    kinds = {c["kind"] for c in components.values()}
    gaps = []

    def gap(gid, detail):
        gaps.append({"id": gid, "detail": detail})

    if not kinds & {"metrics"}:
        gap("no-metrics", "지표 저장소 없음 → '언제부터·얼마나' 추세, 큐 적체, 처리량을 볼 수 없다")
    if not kinds & {"logs"}:
        gap("no-logs", "로그 저장소 없음 → kubectl logs 만 가능, 재시작 전·삭제된 파드 로그는 볼 수 없다")
    elif not kinds & {"log-shipper", "telemetry-collector"}:
        gap("no-shipper", "로그 저장소는 있지만 수집기를 찾지 못함 → 어떤 로그가 들어가는지 확인 필요")
    shippers = sorted(n for n, c in components.items() if c["kind"] == "log-shipper")
    if shippers:
        scraped = [n for n in shippers if any(i.get("scraped") for i in components[n]["instances"])]
        if not scraped:
            gap("shipper-health", "%s 자체 상태(버퍼·재시도·처리량)를 보는 신호 없음 → '로그 없음'과 '수집 중단'을 구분할 수 없다" % ", ".join(shippers))
    if ttl_sec is not None:
        if ttl_sec < 6 * 3600:
            gap("short-event-ttl", "k8s 이벤트 보존 %s (%s) → 사후 분석 때 재시작·OOM·스케줄링 이력이 사라진다" % (events["ttl"], events["ttl_source"]))
    elif not stamps or (now - min(stamps)).total_seconds() < 6 * 3600:
        gap("event-ttl-unknown", "k8s 이벤트 보존 기간 미확인 (API 서버 설정이 안 보임, 남은 가장 오래된 이벤트 %s). 관리형은 보통 1h" % events.get("oldest_age", "없음"))
    if not kinds & {"gitops"}:
        gap("no-gitops", "GitOps 없음 → 변경 이력은 ReplicaSet revision·생성 시각으로만 복원")
    if not metrics_api:
        gap("no-metrics-api", "metrics API(metrics-server) 없음 → kubectl top 불가")
    if not kinds & {"traces"}:
        gap("no-traces", "트레이스 없음 → 서비스 간 어느 구간에서 느려지는지는 로그의 request_id 로만 추적")
    if not kinds & {"alerting"}:
        gap("no-alerting", "경보 시스템 없음 → 장애를 먼저 알려줄 신호가 없다")
    no_access = sorted(n for n, c in components.items()
                       if c["kind"] in ("metrics", "logs", "dashboard", "gitops", "message-queue", "alerting")
                       and c["instances"] and not any(i["access"] for i in c["instances"]))
    if no_access:
        gap("no-access", "클러스터 밖 접근 주소 없음: %s" % ", ".join(no_access))
    if coverage:
        missing = sorted(n for n, c in components.items()
                         if c["instances"] and not any(i.get("scraped") for i in c["instances"]))
        coverage["not_scraped"] = missing
        if missing:
            gap("not-scraped", "Prometheus 가 수집하지 않는 구성요소: %s" % ", ".join(missing))

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
        "metrics_coverage": coverage,
        "events": events,
        "gaps": gaps,
    }


def metrics_coverage(components):
    """인증 없이 열린 Prometheus 가 있으면 수집 대상 목록을 읽어, 구성요소마다 수집되는지(scraped) 표시한다."""
    prom = components.get("prometheus") or {}
    for inst in prom.get("instances", []):
        for pr in inst.get("probe", []):
            if not pr.get("reachable") or pr.get("auth_required") or pr.get("http_status") != 200:
                continue
            base = pr["url"].rsplit("/-/", 1)[0]
            try:
                req = urllib.request.Request(base + "/api/v1/targets?state=active", headers={"User-Agent": "koa-discover"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    targets = json.load(r)["data"]["activeTargets"]
            except Exception:
                continue
            labels = [t.get("labels", {}) for t in targets]
            for c in components.values():
                for i in c["instances"]:
                    svcs = {s.split(" ", 1)[0] for s in i.get("services", [])}
                    wl = i["workload"].split("/", 1)[1]
                    i["scraped"] = any(
                        l.get("namespace") == i["namespace"]
                        and (l.get("service") in svcs or (l.get("pod") or "").startswith(wl + "-"))
                        for l in labels)
            return {"source": base, "targets": len(targets),
                    "up": sum(1 for t in targets if t.get("health") == "up")}
    return None


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
    out = Path(a.out) if a.out else CLUSTERS / ("%s.yaml" % name)
    out.parent.mkdir(parents=True, exist_ok=True)
    header = "# KOA 클러스터 프로필 — koa/discover.py 가 만든 파일. 직접 고치지 말고 다시 실행한다.\n"
    out.write_text(header + yaml.safe_dump(profile, sort_keys=False, allow_unicode=True, width=200))

    # 결과 표 + 제안 보고서
    import report
    md = report.build(profile, catalog, probed=a.probe)
    rep = out.with_suffix(".report.md")
    rep.write_text(md)
    print(md)
    print("\n---\n프로필: %s\n보고서: %s" % (out, rep))


if __name__ == "__main__":
    main()
