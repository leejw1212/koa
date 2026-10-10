#!/usr/bin/env python3
"""KOA 흐름 찾기 — 설정과 로그만 읽어서 "요청이 지나는 길" 후보를 만든다.

클러스터에는 읽기만 한다: 목록·객체 조회(get/list)와 파드 로그 읽기.
앱·Service 에 요청을 보내지 않는다(curl·헬스 GET·포트 연결 시험 없음). exec·port-forward·proxy 를 쓰지 않는다.
DNS 를 조회하지 않는다. Secret 은 읽지 않는다(env 가 Secret 에서 온다는 사실만 본다).

근거
  설정  입구: Ingress·HTTPRoute·GRPCRoute → Service → 워크로드, LoadBalancer/NodePort Service
        연결: 워크로드의 env·args·참조 ConfigMap 에 적힌 주소 → Service → 워크로드, ExternalName, Ingress 어노테이션
        허용: NetworkPolicy ingress 규칙 (허용 ≠ 사용. 표에만 쓰고 흐름은 만들지 않는다)
  로그  다른 워크로드의 파드 IP·Service 주소가 로그에 나오는가, 같은 요청 ID 가 여러 워크로드 로그에 나오는가

  python3 koa/flowmap.py                       # kubernetes MCP(이 프로필에 등록된 것)로 읽는다
  python3 koa/flowmap.py --source kubectl      # 읽기 전용 kubeconfig 로 kubectl 직접 (KOA_KUBECONFIG / --kubeconfig)
  python3 koa/flowmap.py --ns shop,payment     # 이 네임스페이스 워크로드에서 출발하는 것만
  python3 koa/flowmap.py --no-logs             # 설정만
  python3 koa/flowmap.py --shards cand.json    # 클러스터 지식 파편 형식으로 후보 저장 → knowledge.py save 로 반영
  python3 koa/flowmap.py --json                # 전체 결과 JSON

전체 결과는 <결과 폴더>/<이름>.flowmap/<시각>.json. 클러스터 지식(local/knowledge/)은 바꾸지 않는다.
판정 규칙: docs/koa-flowmap.md
"""
import argparse
import datetime as dt
import ipaddress
import json
import os
import re
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import knowledge as K  # noqa: E402
from discover import DEFAULT_KUBECONFIG, clean_path, image_name, owner_of, selector_matches  # noqa: E402
from paths import CATALOG, CLUSTERS  # noqa: E402
from triage import KubectlSource, McpSource, YLoader  # noqa: E402

UTC = dt.timezone.utc
SKIP_NS = "kube-system,kube-public,kube-node-lease"  # 쿠버네티스 기본 시스템 네임스페이스

# ─── 주소 해석의 일반 규칙 (잘 알려진 기본값. 구성요소 종류는 탐색 프로필이 있으면 그쪽이 우선) ───
PROTO_KIND = {
    "amqp": "message-queue", "amqps": "message-queue", "kafka": "message-queue", "nats": "message-queue",
    "mqtt": "message-queue", "pulsar": "message-queue", "stomp": "message-queue",
    "redis": "cache", "rediss": "cache", "redis-sentinel": "cache", "memcached": "cache",
    "postgres": "database", "postgresql": "database", "mysql": "database", "mariadb": "database",
    "mongodb": "database", "mongodb+srv": "database", "sqlserver": "database", "oracle": "database",
    "cassandra": "database",
}
PORT_PROTO = {5672: "amqp", 5671: "amqps", 9092: "kafka", 4222: "nats", 1883: "mqtt", 6650: "pulsar",
              6379: "redis", 26379: "redis-sentinel", 11211: "memcached", 5432: "postgres", 3306: "mysql",
              27017: "mongodb", 1433: "sqlserver", 1521: "oracle", 9042: "cassandra"}
SYNC_PROTOS = {"http", "https", "grpc", "grpcs", "ws", "wss", "h2c"}
# 다른 워크로드의 로그를 실어 나르거나 긁어 가는 종류: 그 로그의 IP·요청 ID 는 "통신"의 근거가 아니다
CARRIER_KINDS = {"log-shipper", "telemetry-collector", "logs", "metrics", "traces", "metrics-exporter"}

# 주소를 뜻하는 이름(env 이름·설정 키의 마지막 낱말)
HOST_WORDS = {"host", "hosts", "hostname", "addr", "address", "server", "servers", "service", "endpoint",
              "endpoints", "url", "uri", "urls", "dsn", "broker", "brokers", "bootstrap", "upstream", "target",
              "targets", "backend", "backends"}
SECRETISH = re.compile(r"(?i)pass|secret|token|apikey|api_key|credential|private")
# 파일 이름 끝(app.log, config.yaml)을 도메인으로 보지 않는다
FILE_EXT = {"log", "txt", "json", "yaml", "yml", "conf", "cfg", "ini", "toml", "xml", "html", "htm", "css", "js",
            "ts", "py", "sh", "md", "csv", "gz", "tar", "tgz", "zip", "jar", "war", "pid", "sock", "lock", "tmp",
            "pem", "crt", "key", "properties", "java", "go", "rb", "php", "so", "class", "bak", "err", "out", "sql",
            "db", "png", "jpg", "svg", "ico", "env", "service", "socket", "d"}

SPLIT = re.compile(r"[\s\"'`<>;()\[\]{}|\\]+|,(?=[A-Za-z][A-Za-z0-9+.-]*://)")
SCHEME_URL = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*)://([^/?#]*)")
HOSTPORT = re.compile(r"^(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                      r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*)\.?(?::(\d{2,5}))?$")
LINE_KEY = re.compile(r"^\s*(?:-\s+)?[\"']?([A-Za-z0-9_.\-]+)[\"']?\s*(?:[:=]\s*|\s+)(.+)$")
LOG_KEY = re.compile(r"(?i)\b(?:host|hostname|server|addr|address|endpoint|upstream|peer|remote|broker)"
                     r"[\"']?\s*[=:]\s*[\"']?([A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9])")
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
RID_KEY = re.compile(r"(?i)\b(?:x[-_])?(?:request|req|trace|correlation|transaction)[-_]?id[\"']?\s*[:=]\s*"
                     r"[\"']?([A-Za-z0-9][A-Za-z0-9._:-]{5,63})")
UUID = re.compile(r"(?<![0-9a-fA-F-])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![0-9a-fA-F-])")
TRACEPARENT = re.compile(r"\b00-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}\b")
HEX32 = re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{32}(?![0-9a-fA-F])")


def words(name):
    return [w.lower() for w in re.split(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])", name or "") if w]


# 문서 링크·자기 주소를 뜻하는 키 (runbook_url, homepage, external-url, root_url, advertised.listeners, redirect_uri)
DOC_WORDS = {"runbook", "doc", "docs", "documentation", "help", "homepage", "website", "wiki", "license", "icon",
             "logo", "image", "avatar", "schema", "xmlns", "repo", "repository", "issues", "tracker", "dashboard",
             "generator", "external", "public", "root", "advertise", "advertised", "self", "redirect", "link"}


def docish(name):
    return bool(set(words(name)) & DOC_WORDS)


def hostish(name):
    """env 이름·설정 키가 주소를 뜻하나 (REDIS_URL, spring.redis.host, Host, bootstrap.servers, proxy_pass)."""
    w = words(name)
    if not w or docish(name):
        return False
    return (w[-1] in HOST_WORDS or any(x in ("url", "uri", "dsn", "host", "endpoint") for x in w)
            or w[-2:] in (["proxy", "pass"], ["connection", "string"]))


def redact(s, limit=140):
    s = re.sub(r"(://)[^/@\s]*@", r"\1***@", str(s))
    s = re.sub(r"(?i)(password|passwd|pwd|secret|token|apikey|api_key)([\"']?\s*[:=]\s*)[^\s,;&\"']+", r"\1\2***", s)
    s = re.sub(r"(?i)(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}", r"\1 ***", s)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[:limit - 1] + "…"


def addresses(text, bare=False):
    """text 에 적힌 주소 → [(scheme, host, port)]. bare=True 면 포트 없는 낱말도 후보 (주소를 뜻하는 키의 값일 때)."""
    out = []
    for tok in SPLIT.split(text or ""):
        if not tok or "$" in tok or "%(" in tok:
            continue
        m = SCHEME_URL.search(tok)
        if m:
            scheme, auth = m.group(1).lower(), m.group(2).rsplit("@", 1)[-1]
            for part in auth.split(","):
                h = HOSTPORT.match(part)
                if h:
                    out.append((scheme, h.group(1).lower(), int(h.group(2)) if h.group(2) else None))
            continue
        tok = tok.rsplit("=", 1)[-1].strip(",.")
        for part in tok.split(","):
            part = part.split("/", 1)[0]
            h = HOSTPORT.match(part)
            if h and (h.group(2) or bare):
                out.append(("", h.group(1).lower(), int(h.group(2)) if h.group(2) else None))
    return out


def parse_ts(line):
    """kubectl logs --timestamps 접두어 '2026-10-05T03:37:12.967Z ...' → datetime"""
    if len(line) >= 20 and line[4] == "-" and line[10] == "T":
        try:
            return dt.datetime.strptime(line[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


# ─── 읽기 (kubernetes MCP 또는 kubectl — 둘 다 get/list/logs 만) ─────────────────
CALLS = {"list": 0, "get": 0, "logs": 0}  # 클러스터에 보낸 요청 수 (출력에 그대로 보인다)
_CALLS_LOCK = threading.Lock()


def _count(kind):
    with _CALLS_LOCK:
        CALLS[kind] += 1


def errtext(x):
    """MCP·kubectl 오류를 한 줄로 ('{\n "error": "... not found"' → '... not found')."""
    s = " ".join(str(x).split()) or repr(x)
    m = re.search(r'"(?:error|message)"\s*:\s*"([^"]+)"', s)
    return (m.group(1) if m else s)[:160]


def list_res(src, res, limits, optional=False):
    _count("list")
    try:
        return src.list(res)
    except Exception as x:  # noqa: BLE001
        msg = errtext(x)
        if not optional or "forbidden" in msg.lower():
            limits.append("%s 를 못 읽었다 (%s) — 그 근거는 빠졌다" % (res, msg))
        return None


def get_object(src, resource, ns, name):
    _count("get")
    if isinstance(src, McpSource):
        err, text = src.server.call("kubectl_get", {"resourceType": resource, "name": name, "namespace": ns,
                                                    "output": "yaml"}, timeout=src.timeout)
        if err:
            raise RuntimeError(errtext(text))
        return yaml.load(text, Loader=YLoader) or {}
    return src.kube.json("get", resource, name, "-n", ns)


def read_logs(src, ns, pod, container, tail):
    _count("logs")
    if isinstance(src, McpSource):
        args = {"resourceType": "pod", "name": pod, "namespace": ns, "tail": tail, "timestamps": True}
        if container:
            args["container"] = container
        err, text = src.server.call("kubectl_logs", args, timeout=src.timeout)
        if err:
            raise RuntimeError(errtext(text))
        if text.lstrip().startswith("{"):
            try:
                d = json.loads(text)
                if isinstance(d, dict) and isinstance(d.get("logs"), str):
                    text = d["logs"]
            except ValueError:
                pass
        return text
    p = src.kube.run("logs", pod, "-n", ns, "--tail=%d" % tail, "--timestamps",
                     *(["-c", container] if container else []), check=False)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip()[:160])
    return p.stdout


def fetch_all(src, jobs, fn):
    """jobs 를 fn 으로 읽는다. MCP 는 stdio 하나라 차례로, kubectl 은 병렬로."""
    def safe(j):
        try:
            return j, fn(*j), None
        except Exception as x:  # noqa: BLE001
            return j, None, errtext(x)
    if isinstance(src, McpSource):
        return [safe(j) for j in jobs]
    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(safe, jobs))


# ─── 주소 → 클러스터 안 대상 ───────────────────────────────────────────────
class Resolver:
    def __init__(self, services, namespaces):
        self.svc, self.cip, self.pod_ip, self.cache = {}, {}, {}, {}
        self.ns = set(namespaces)
        for s in services:
            ns, name = s["metadata"]["namespace"], s["metadata"]["name"]
            self.svc[(ns, name)] = s
            self.ns.add(ns)
            spec = s.get("spec") or {}
            for ip in [spec.get("clusterIP")] + list(spec.get("clusterIPs") or []):
                if ip and ip != "None":
                    self.cip[ip] = (ns, name)

    def resolve(self, host, ns):
        k = (host, ns)
        if k not in self.cache:
            self.cache[k] = self._resolve(host, ns)
        return self.cache[k]

    def _resolve(self, host, ns):
        """('svc', ns, name) | ('pod', 워크로드) | ('ext', host) | ('unknown', host) | None"""
        h = host.strip("[]").rstrip(".").lower()
        if not h or h in ("localhost", "0.0.0.0", "::", "::1") or h.startswith("127."):
            return None
        try:
            ip = ipaddress.ip_address(h)
        except ValueError:
            ip = None
        if ip is not None:
            if h in self.pod_ip:
                return ("pod", self.pod_ip[h])
            if h in self.cip:
                return ("svc",) + self.cip[h]
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
                return None  # 노드·다른 망 주소 — 누구인지 모른다
            return ("ext", h)
        labels = h.split(".")
        if "svc" in labels:
            i = labels.index("svc")
            if i >= 2 and (labels[i - 1], labels[i - 2]) in self.svc:
                return ("svc", labels[i - 1], labels[i - 2])
            return ("unknown", h)  # 클러스터 안 주소 형식인데 그런 Service 가 없다
        if len(labels) == 1:
            return ("svc", ns, h) if (ns, h) in self.svc else None
        if len(labels) == 2:
            if (labels[1], labels[0]) in self.svc:
                return ("svc", labels[1], labels[0])  # <Service>.<네임스페이스>
            if (ns, labels[1]) in self.svc:
                return ("svc", ns, labels[1])  # <파드>.<헤드리스 Service>
            if labels[1] in self.ns:
                return ("unknown", h)
        if len(labels) == 3 and (labels[2], labels[1]) in self.svc:
            return ("svc", labels[2], labels[1])  # <파드>.<Service>.<네임스페이스>
        tld = labels[-1]
        if tld.isalpha() and len(tld) >= 2 and tld not in self.ns and tld not in FILE_EXT:
            return ("ext", h)
        return None


class Edges:
    """(출발, 도착) → 근거. 근거 종류: entry·config(설정), netpol(허용 규칙), log-ip·log-name·log-id(로그)"""

    def __init__(self):
        self.e = {}

    def add(self, a, b, etype, where, value=None, proto=None, **extra):
        if not a or not b or a == b:
            return
        d = self.e.setdefault((a, b), {"protos": set(), "evidence": []})
        if proto:
            d["protos"].add(proto)
        ev = {"type": etype, "where": where}
        if value:
            ev["value"] = value
        ev.update({k: v for k, v in extra.items() if v is not None})
        if ev not in d["evidence"]:
            d["evidence"].append(ev)


def verdict(d):
    """설정+로그 > 설정 > 로그(주소·IP) > 요청ID(순서만, 사이에 다른 곳이 있을 수 있다) > 허용규칙"""
    types = {ev["type"] for ev in d["evidence"]}
    cfg = types & {"entry", "config"}
    if cfg and types & {"log-ip", "log-name", "log-id"}:
        return "설정+로그"
    if cfg:
        return "설정"
    if "log-name" in types:
        return "로그"
    if "netpol" in types and "log-ip" in types:
        return "허용규칙+로그"  # 방향은 허용 규칙, 실제로 오간 건 로그의 IP
    if "log-ip" in types:
        return "로그"
    if "log-id" in types:
        return "요청ID"
    return "허용규칙" if "netpol" in types else "?"


RANK = {"설정+로그": 0, "설정": 1, "로그": 2, "허용규칙+로그": 3, "요청ID": 4, "허용규칙": 5, "?": 6}


# ─── 본체 ──────────────────────────────────────────────────────────────
def run(src, a, limits):
    scope = set(x for x in (a.ns or "").split(",") if x) or None
    skip = set(x for x in (a.skip_ns or "").split(",") if x)
    notes = {"secret_env": [], "secret_envfrom": [], "unknown": [], "skipped_logs": 0, "ip_broadcasters": []}
    counts = {}

    pods = list_res(src, "pods", limits) or []
    services = list_res(src, "services", limits) or []
    ingresses = list_res(src, "ingresses.networking.k8s.io", limits, True) or []
    routes = []
    for res in ("httproutes.gateway.networking.k8s.io", "grpcroutes.gateway.networking.k8s.io"):
        routes += [(res.split(".")[0], r) for r in (list_res(src, res, limits, True) or [])]
    ingclasses = list_res(src, "ingressclasses.networking.k8s.io", limits, True) or []
    netpols = list_res(src, "networkpolicies.networking.k8s.io", limits, True) or []
    namespaces = list_res(src, "namespaces", limits, True) or []
    cronjobs = list_res(src, "cronjobs.batch", limits, True) or []
    ns_labels = {n["metadata"]["name"]: n["metadata"].get("labels") or {} for n in namespaces}

    # 워크로드 = 파드의 소유자 (Deployment·StatefulSet·DaemonSet·CronJob·Job·Pod)
    wl, pod_wl, pods_by_ns = {}, {}, defaultdict(list)
    for p in pods:
        ns = p["metadata"]["namespace"]
        kind, name = owner_of(p)
        key = "%s/%s/%s" % (ns, kind, name)
        w = wl.setdefault(key, {"ns": ns, "kind": kind, "name": name, "pods": [], "spec": p.get("spec") or {}})
        w["pods"].append(p)
        pod_wl[(ns, p["metadata"]["name"])] = key
        pods_by_ns[ns].append(p)
    for cj in cronjobs:
        ns, name = cj["metadata"]["namespace"], cj["metadata"]["name"]
        key = "%s/CronJob/%s" % (ns, name)
        if key not in wl:
            spec = ((((cj.get("spec") or {}).get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec") or {}
            wl[key] = {"ns": ns, "kind": "CronJob", "name": name, "pods": [], "spec": spec}

    def in_scope(key, as_source=True):
        ns = key.split("/", 1)[0] if "/" in key and not key.startswith(("ext:", "svc:", "ingress:", "gateway:", "entry:")) else None
        if ns is None:
            return True
        if ns in skip:
            return False
        return not (as_source and scope and ns not in scope)

    R = Resolver(services, ns_labels.keys())
    host_net = 0
    for p in pods:
        st = p.get("status") or {}
        if (p.get("spec") or {}).get("hostNetwork"):
            host_net += 1
            continue
        if st.get("phase") not in ("Running", "Pending"):
            continue
        for ip in [x.get("ip") for x in st.get("podIPs") or []] or [st.get("podIP")]:
            if ip:
                R.pod_ip[ip] = pod_wl[(p["metadata"]["namespace"], p["metadata"]["name"])]

    svc_wls = {}
    for s in services:
        ns, name = s["metadata"]["namespace"], s["metadata"]["name"]
        sel = (s.get("spec") or {}).get("selector") or {}
        if sel:
            ks = sorted({pod_wl[(ns, p["metadata"]["name"])] for p in pods_by_ns[ns]
                         if selector_matches(sel, p["metadata"].get("labels") or {})})
            if ks:
                svc_wls[(ns, name)] = ks

    def nodes_for(r):
        if not r or r[0] in ("unknown",):
            return []
        if r[0] == "pod":
            return [r[1]]
        if r[0] == "ext":
            return ["ext:" + r[1]]
        ns, name = r[1], r[2]
        if (ns, name) == ("default", "kubernetes"):
            return []  # API 서버
        spec = R.svc[(ns, name)].get("spec") or {}
        if spec.get("type") == "ExternalName" and spec.get("externalName"):
            return ["ext:" + spec["externalName"].lower().rstrip(".")]
        return svc_wls.get((ns, name)) or ["svc:%s/%s" % (ns, name)]

    prof = K.profile(a.cluster) or {}
    prof_kind = {}
    for comp, c in (prof.get("components") or {}).items():
        for inst in c.get("instances") or []:
            if inst.get("namespace") and str(inst.get("workload", "")).count("/") == 1:
                prof_kind["%s/%s" % (inst["namespace"], inst["workload"])] = c.get("kind")
    rules = [(re.compile(r["image"]), r.get("kind")) for r in
             (yaml.safe_load(CATALOG.read_text()).get("components") or {}).values() if r.get("image")]
    for key, w in wl.items():  # 탐색 프로필이 없거나 낡았을 때: 카탈로그 이미지 규칙
        if key not in prof_kind:
            for c in w["spec"].get("containers") or []:
                img = image_name(c.get("image") or "")[0]
                kd = next((kd for rx, kd in rules if rx.search(img)), None)
                if kd:
                    prof_kind[key] = kd
                    break

    E = Edges()

    def add_refs(src_key, text, bare, where, show):
        ns = wl[src_key]["ns"]
        for scheme, host, port in addresses(text, bare):
            r = R.resolve(host, ns)
            if r and r[0] == "unknown":
                notes["unknown"].append({"from": src_key, "address": host, "where": where})
                continue
            if r and r[0] == "ext" and not bare and port is None:
                continue  # 주소 키가 아닌 곳의 포트 없는 외부 URL = 안내문·문서 링크일 때가 많다
            for node in nodes_for(r):
                if node != src_key and in_scope(node, False):
                    E.add(src_key, node, "config", where, value=show, proto=scheme or PORT_PROTO.get(port), port=port)

    # ── 설정: ConfigMap 참조 모으기 → 한 번에 읽기 ──
    cm_refs = {}
    for key, w in wl.items():
        if not in_scope(key):
            continue
        spec = w["spec"]
        names = set()
        for c in (spec.get("initContainers") or []) + (spec.get("containers") or []):
            for e in c.get("env") or []:
                ref = (e.get("valueFrom") or {}).get("configMapKeyRef")
                if ref and ref.get("name"):
                    names.add(ref["name"])
            for ef in c.get("envFrom") or []:
                if (ef.get("configMapRef") or {}).get("name"):
                    names.add(ef["configMapRef"]["name"])
        for v in spec.get("volumes") or []:
            if (v.get("configMap") or {}).get("name"):
                names.add(v["configMap"]["name"])
            for s in (v.get("projected") or {}).get("sources") or []:
                if (s.get("configMap") or {}).get("name"):
                    names.add(s["configMap"]["name"])
        for n in names:
            cm_refs[(w["ns"], n)] = None
    for (ns, n), obj, err in fetch_all(src, sorted(cm_refs), lambda ns, n: get_object(src, "configmaps", ns, n)):
        if err:
            if "not found" not in err.lower():
                limits.append("ConfigMap %s/%s 를 못 읽었다 (%s)" % (ns, n, err))
            cm_refs[(ns, n)] = {}
        else:
            cm_refs[(ns, n)] = {k: v for k, v in ((obj or {}).get("data") or {}).items() if isinstance(v, str)}
    counts["configmaps"] = sum(1 for v in cm_refs.values() if v)

    def scan_file(src_key, cm, fname, text):
        for line in text[:512 * 1024].splitlines():
            s = line.strip()
            if not s or s.startswith(("#", "//", ";")):
                continue
            s = s[:4000]
            m = LINE_KEY.match(s)
            if m and docish(m.group(1)):
                continue
            keyed = bool(m and hostish(m.group(1)))
            show = "(값 숨김)" if SECRETISH.search(s) else redact(s)
            add_refs(src_key, m.group(2) if keyed else s, keyed, "ConfigMap %s · %s" % (cm, fname), show)

    for key, w in sorted(wl.items()):
        if not in_scope(key):
            continue
        ns, spec = w["ns"], w["spec"]
        for init, cs in ((True, spec.get("initContainers") or []), (False, spec.get("containers") or [])):
            for c in cs:
                tag = "%s%s" % ("initContainer " if init else "", c.get("name"))
                for e in c.get("env") or []:
                    name = e.get("name") or ""
                    vf = e.get("valueFrom") or {}
                    if docish(name):
                        continue
                    if e.get("value") is not None:
                        show = "(값 숨김)" if SECRETISH.search(name) else redact(e["value"])
                        add_refs(key, str(e["value"]), hostish(name), "env %s (%s)" % (name, tag), show)
                    elif vf.get("configMapKeyRef"):
                        ref = vf["configMapKeyRef"]
                        val = (cm_refs.get((ns, ref.get("name"))) or {}).get(ref.get("key"))
                        if val:
                            show = "(값 숨김)" if SECRETISH.search(name) else redact(val)
                            add_refs(key, val, hostish(name) or hostish(ref.get("key")),
                                     "env %s ← ConfigMap %s (%s)" % (name, ref.get("name"), tag), show)
                    elif vf.get("secretKeyRef") and hostish(name):
                        notes["secret_env"].append({"workload": key, "env": name,
                                                    "secret": (vf["secretKeyRef"] or {}).get("name")})
                for ef in c.get("envFrom") or []:
                    if (ef.get("configMapRef") or {}).get("name"):
                        cm = ef["configMapRef"]["name"]
                        for k, v in (cm_refs.get((ns, cm)) or {}).items():
                            show = "(값 숨김)" if SECRETISH.search(k) else redact(v)
                            add_refs(key, v, hostish(k), "envFrom ConfigMap %s · %s (%s)" % (cm, k, tag), show)
                    if (ef.get("secretRef") or {}).get("name"):
                        notes["secret_envfrom"].append({"workload": key, "secret": ef["secretRef"]["name"]})
                argv = [str(x) for x in (c.get("command") or []) + (c.get("args") or [])]
                for arg in argv:
                    if not docish(arg.split("=", 1)[0]):
                        add_refs(key, arg, False, "args (%s)" % tag, redact(arg))
                if init:  # 의존 대기 (until nslookup db; nc -z db 5432 ...) — 같은 네임스페이스 Service 이름과 똑같은 낱말
                    for tok in set(SPLIT.split(" ".join(argv))):
                        if tok and (ns, tok.lower()) in R.svc:
                            for node in nodes_for(("svc", ns, tok.lower())):
                                if node != key and in_scope(node, False):
                                    E.add(key, node, "config", "initContainer 대기 (%s)" % c.get("name"), value=redact(tok))
        for v in spec.get("volumes") or []:
            cms = [(v.get("configMap") or {}).get("name")]
            cms += [(s.get("configMap") or {}).get("name") for s in (v.get("projected") or {}).get("sources") or []]
            for cm in [x for x in cms if x]:
                for fname, text in sorted((cm_refs.get((ns, cm)) or {}).items()):
                    scan_file(key, cm, fname, text)

    # ── 입구: Ingress·Route → Service → 워크로드 ──
    lb_index = {}
    for s in services:
        for lb in ((s.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []:
            for v in (lb.get("ip"), lb.get("hostname")):
                if v:
                    lb_index.setdefault(v, set()).update(svc_wls.get((s["metadata"]["namespace"], s["metadata"]["name"]), []))
    class_ctrl = {c["metadata"]["name"]: (c.get("spec") or {}).get("controller", "") for c in ingclasses}
    default_class = next((c["metadata"]["name"] for c in ingclasses
                          if (c["metadata"].get("annotations") or {}).get("ingressclass.kubernetes.io/is-default-class") == "true"), None)
    running = {k for k, w in wl.items() if w["kind"] in ("Deployment", "DaemonSet", "StatefulSet")
               and any((p.get("status") or {}).get("phase") == "Running" for p in w["pods"])}
    prof_ingress = sorted(k for k, kd in prof_kind.items() if kd in ("ingress", "gateway") and k in wl)

    def ingress_controller(ing, cls):
        for lb in ((ing.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []:
            for v in (lb.get("ip"), lb.get("hostname")):
                if v in lb_index and len(lb_index[v]) == 1:
                    return next(iter(lb_index[v]))
        token = (class_ctrl.get(cls or default_class or "") or "").rsplit("/", 1)[-1].lower()
        if token:
            hits = sorted(k for k in running if token in wl[k]["name"].lower())
            hits = [k for k in hits if k in prof_ingress] or hits
            if len(hits) == 1:
                return hits[0]
        if len(prof_ingress) == 1:
            return prof_ingress[0]
        if cls:  # 클래스 이름이 들어간 컨트롤러형 워크로드가 하나뿐이면 그것
            hits = [k for k in running if cls.lower() in wl[k]["name"].lower()
                    and any(t in wl[k]["name"].lower() for t in ("ingress", "controller", "gateway", "proxy"))]
            if len(hits) == 1:
                return hits[0]
        return "ingress:%s" % (cls or default_class or "기본")

    entries = []

    def add_entry(via, name, ns, host, path, svc, port, ctrl):
        if ns in skip or (scope and ns not in scope):
            return
        backends = nodes_for(("svc", ns, svc)) if (ns, svc) in R.svc else []
        raw, path = path, (clean_path(path) or "/") if path else ""  # 정규식 경로 '/x(/|$)(.*)' → '/x'
        entries.append({"via": via, "name": "%s/%s" % (ns, name), "host": host, "path": path, "path_raw": raw,
                        "service": "%s/%s%s" % (ns, svc, ":%s" % port if port else ""), "controller": ctrl,
                        "backends": backends})
        for b in backends:
            E.add(ctrl, b, "entry", "%s %s/%s %s%s" % (via, ns, name, host, path), proto="http")

    for ing in ingresses:
        ns, name = ing["metadata"]["namespace"], ing["metadata"]["name"]
        spec, ann = ing.get("spec") or {}, ing["metadata"].get("annotations") or {}
        cls = spec.get("ingressClassName") or ann.get("kubernetes.io/ingress.class")
        ctrl = ingress_controller(ing, cls)
        db = (spec.get("defaultBackend") or {}).get("service") or {}
        if db.get("name"):
            add_entry("Ingress", name, ns, "*", "/", db["name"], (db.get("port") or {}).get("number"), ctrl)
        for rule in spec.get("rules") or []:
            for p in (rule.get("http") or {}).get("paths") or []:
                svc = (p.get("backend") or {}).get("service") or {}
                if svc.get("name"):
                    port = (svc.get("port") or {}).get("number") or (svc.get("port") or {}).get("name")
                    add_entry("Ingress", name, ns, rule.get("host") or "*", p.get("path") or "/", svc["name"], port, ctrl)
        for k, v in ann.items():  # auth-url 같은 외부 인증·미러 주소
            if k == "kubectl.kubernetes.io/last-applied-configuration" or not isinstance(v, str):
                continue
            for scheme, host, port in addresses(v):
                for node in nodes_for(R.resolve(host, ns)):
                    if in_scope(node, False) and not (ns in skip or (scope and ns not in scope)):
                        E.add(ctrl, node, "config", "Ingress %s/%s 어노테이션 %s" % (ns, name, k.rsplit("/", 1)[-1]),
                              value=redact(v), proto=scheme or PORT_PROTO.get(port))

    gw_pods = defaultdict(set)  # (게이트웨이 이름) → 워크로드 (구현체가 붙이는 *gateway-name 라벨)
    for p in pods:
        for k, v in (p["metadata"].get("labels") or {}).items():
            if k.endswith("gateway-name"):
                gw_pods[v].add(pod_wl[(p["metadata"]["namespace"], p["metadata"]["name"])])
    for kind, rt in routes:
        ns, name = rt["metadata"]["namespace"], rt["metadata"]["name"]
        spec = rt.get("spec") or {}
        parents = [pr.get("name") for pr in spec.get("parentRefs") or [] if pr.get("name")]
        gws = sorted(set().union(*[gw_pods.get(g, set()) for g in parents])) if parents else []
        ctrl = gws[0] if len(gws) == 1 else "gateway:%s" % (",".join(parents) or "?")
        hosts = spec.get("hostnames") or ["*"]
        for rule in spec.get("rules") or []:
            path = ""
            for mt in rule.get("matches") or []:
                path = ((mt.get("path") or {}).get("value")) or path
            for br in rule.get("backendRefs") or []:
                if br.get("group", "") not in ("", "core") or br.get("kind", "Service") != "Service" or not br.get("name"):
                    continue
                for h in hosts:
                    add_entry(kind, name, br.get("namespace") or ns, h, path or "/", br["name"], br.get("port"), ctrl)

    ctrl_nodes = {e["controller"] for e in entries}
    for s in services:
        ns, name = s["metadata"]["namespace"], s["metadata"]["name"]
        typ = (s.get("spec") or {}).get("type")
        if typ in ("LoadBalancer", "NodePort"):
            backs = svc_wls.get((ns, name)) or []
            if backs and not (set(backs) & ctrl_nodes):
                add_entry("Service(%s)" % typ, name, ns, "*", "", name, None, "entry:%s" % typ)

    # ── 허용 규칙: NetworkPolicy ingress (podSelector 만 — 네임스페이스 통째·ipBlock 은 너무 넓어 뺀다) ──
    np_skipped = 0
    for npo in netpols:
        ns, name = npo["metadata"]["namespace"], npo["metadata"]["name"]
        spec = npo.get("spec") or {}
        tsel = spec.get("podSelector") or {}
        if tsel.get("matchExpressions"):
            np_skipped += 1
            continue
        targets = sorted({pod_wl[(ns, p["metadata"]["name"])] for p in pods_by_ns[ns]
                          if not tsel.get("matchLabels") or selector_matches(tsel["matchLabels"], p["metadata"].get("labels") or {})})
        for rule in spec.get("ingress") or []:
            for frm in rule.get("from") or []:
                psel, nsel = frm.get("podSelector"), frm.get("namespaceSelector")
                if psel is None or (psel or {}).get("matchExpressions") or (nsel or {}).get("matchExpressions"):
                    np_skipped += 1
                    continue
                nss = [ns] if nsel is None else [n for n, lb in ns_labels.items()
                                                 if not nsel.get("matchLabels") or selector_matches(nsel["matchLabels"], lb)]
                sources = sorted({pod_wl[(n, p["metadata"]["name"])] for n in nss for p in pods_by_ns[n]
                                  if not psel.get("matchLabels") or selector_matches(psel["matchLabels"], p["metadata"].get("labels") or {})})
                if len(sources) * len(targets) > 60:
                    np_skipped += 1
                    continue
                for sk in sources:
                    for tk in targets:
                        if in_scope(sk) and in_scope(tk, False):
                            E.add(sk, tk, "netpol", "NetworkPolicy %s/%s" % (ns, name))

    # ── 로그: 다른 워크로드의 파드 IP·Service 주소·같은 요청 ID ──
    log_stat = {"pods": 0, "lines": 0, "oldest": None, "failed": 0, "per": {}}
    ip_pairs = {}
    ext_logs = {}  # (워크로드, 외부 host) → 줄 수·시각 — 흐름에 넣지 않고 표로만 (배너·문서 링크가 섞인다)
    if not a.no_logs:
        graph_nodes = {n for ab in E.e for n in ab}
        secret_wls = {x["workload"] for x in notes["secret_env"] + notes["secret_envfrom"]}

        def prio(k):
            return (0 if k in graph_nodes else 1 if k in secret_wls else 2, k)
        jobs = []
        for key in sorted((k for k in wl if in_scope(k)), key=prio):
            ps = [p for p in wl[key]["pods"] if (p.get("status") or {}).get("phase") == "Running"]
            ps.sort(key=lambda p: (p.get("status") or {}).get("startTime") or "")
            for p in ps[:max(1, a.pods)]:
                for c in (p.get("spec") or {}).get("containers") or []:
                    jobs.append((key, wl[key]["ns"], p["metadata"]["name"], c.get("name")))
        node_agents = {k for k, w in wl.items() if (w["spec"] or {}).get("hostNetwork")}
        if len(jobs) > a.max_logs:
            notes["skipped_logs"] = len(jobs) - a.max_logs
            jobs = jobs[:a.max_logs]
        token_seen = defaultdict(dict)  # 요청 ID → {워크로드: 처음 본 시각}
        results = fetch_all(src, [(ns, pod, c) for (_, ns, pod, c) in jobs],
                            lambda ns, pod, c: read_logs(src, ns, pod, c, a.tail))
        for (key, ns, pod, c), (_, text, err) in zip(jobs, results):
            if err:
                log_stat["failed"] += 1
                continue
            carrier = prof_kind.get(key) in CARRIER_KINDS or key in node_agents
            log_stat["pods"] += 1
            per = log_stat["per"].setdefault(key, {"containers": 0, "lines": 0, "oldest": None})
            per["containers"] += 1
            for line in (text or "").splitlines():
                if not line.strip():
                    continue
                log_stat["lines"] += 1
                per["lines"] += 1
                ts = parse_ts(line)
                if ts and (per["oldest"] is None or ts < per["oldest"]):
                    per["oldest"] = ts
                if "kube-probe/" in line:
                    continue  # kubelet 헬스 체크
                if not carrier:
                    for ip in set(IPV4.findall(line)):
                        other = R.pod_ip.get(ip)
                        if other is None and ip in R.cip:
                            other = (nodes_for(("svc",) + R.cip[ip]) or [None])[0]
                        if other and other != key and in_scope(other, False):
                            pr = ip_pairs.setdefault((key, other), {"lines": 0, "ips": set(), "first": None, "last": None, "sample": None})
                            pr["lines"] += 1
                            pr["ips"].add(ip)
                            if ts:
                                pr["first"] = min(pr["first"] or ts, ts)
                                pr["last"] = max(pr["last"] or ts, ts)
                            if pr["sample"] is None:
                                pr["sample"] = redact(line, 200)
                    toks = set(RID_KEY.findall(line)) | set(UUID.findall(line)) | set(TRACEPARENT.findall(line)) | set(HEX32.findall(line))
                    for tok in toks:
                        if len(set(tok.replace("-", ""))) > 2 and ts:  # 0000… 같은 상수 제외
                            seen = token_seen[tok]
                            if key not in seen or ts < seen[key]:
                                seen[key] = ts
                hosts = [h for _, h, _ in addresses(line)] + LOG_KEY.findall(line)
                for host in set(hosts):
                    if IPV4.fullmatch(host) or ":" in host:
                        continue  # IP 는 위의 IP 근거로만 (서버 로그의 클라이언트 IP 는 방향이 반대다)
                    r = R.resolve(host, ns)
                    if r and r[0] == "ext":
                        x = ext_logs.setdefault((key, r[1]), {"lines": 0, "first": None, "last": None})
                        x["lines"] += 1
                        if ts:
                            x["first"] = min(x["first"] or ts, ts)
                            x["last"] = max(x["last"] or ts, ts)
                        continue
                    for node in nodes_for(r):
                        if node == key or not in_scope(node, False):
                            continue
                        d = E.e.setdefault((key, node), {"protos": set(), "evidence": []})
                        ev = next((x for x in d["evidence"] if x["type"] == "log-name" and x.get("value") == host), None)
                        if ev is None:
                            ev = {"type": "log-name", "where": "%s 로그" % wl[key]["name"], "value": host, "lines": 0}
                            d["evidence"].append(ev)
                        ev["lines"] += 1
                        if ts:
                            ev["first"] = min(ev.get("first") or iso(ts), iso(ts))
                            ev["last"] = max(ev.get("last") or iso(ts), iso(ts))
            if per["oldest"] and (log_stat["oldest"] is None or per["oldest"] < log_stat["oldest"]):
                log_stat["oldest"] = per["oldest"]

        # IP: 설정·허용규칙 연결이 있으면 그 연결의 확인, 없으면 "방향 미상".
        # 한 로그에 남의 파드 IP 가 많이 나오면(관측·네트워크 도구) 방향 미상 목록에 늘어놓지 않고 한 줄로 접는다.
        loose = defaultdict(int)
        for (x, y) in ip_pairs:
            if not any(ab in E.e and verdict(E.e[ab]) in ("설정", "설정+로그", "로그", "허용규칙") for ab in ((x, y), (y, x))):
                loose[x] += 1
        for x, n in loose.items():
            if n > 5:
                notes["ip_broadcasters"].append({"workload": x, "others": n,
                                                 "lines": sum(pr["lines"] for (s, _), pr in ip_pairs.items() if s == x)})
        broad = {b["workload"] for b in notes["ip_broadcasters"]}
        for (x, y), pr in ip_pairs.items():
            ev = dict(where="%s 로그에 %s 파드 IP" % (wl[x]["name"] if x in wl else x, label_raw(y, wl)), lines=pr["lines"],
                      ips=len(pr["ips"]), first=iso(pr["first"]), last=iso(pr["last"]), sample=pr["sample"])
            hit = False
            for ab in ((x, y), (y, x)):
                if ab in E.e and verdict(E.e[ab]) in ("설정", "설정+로그", "로그", "허용규칙", "허용규칙+로그"):
                    E.add(ab[0], ab[1], "log-ip", **ev)
                    hit = True
            if not hit and x not in broad:
                pair = tuple(sorted((x, y)))
                cur = E.e.get(("?",) + pair)
                if cur is None:
                    E.e[("?",) + pair] = {"protos": set(), "evidence": [dict(type="log-ip", **ev)]}
                else:
                    cur["evidence"].append(dict(type="log-ip", **ev))
        # 요청 ID: 두 워크로드 로그에 같은 ID → 먼저 찍힌 쪽 → 나중 쪽
        pair_ids = defaultdict(lambda: [0, 0])
        for tok, seen in token_seen.items():
            if 2 <= len(seen) <= 8:
                ks = sorted(seen)
                for i in range(len(ks)):
                    for j in range(i + 1, len(ks)):
                        c = pair_ids[(ks[i], ks[j])]
                        c[0 if seen[ks[i]] <= seen[ks[j]] else 1] += 1
        for (x, y), (xy, yx) in pair_ids.items():
            n = xy + yx
            if n < 2:
                continue
            a_, b_ = (x, y) if xy >= yx else (y, x)
            E.add(a_, b_, "log-id", "같은 요청 ID %d개가 두 로그에 (%s 쪽이 먼저 %d개)" % (n, label_raw(a_, wl), max(xy, yx)), ids=n)
    counts.update(pods=len(pods), workloads=len(wl), services=len(services), ingresses=len(ingresses),
                  routes=len(routes), netpols=len(netpols))
    return dict(wl=wl, E=E, entries=entries, notes=notes, prof_kind=prof_kind, log_stat=log_stat, ext_logs=ext_logs,
                counts=counts, host_net=host_net, np_skipped=np_skipped, scope=scope, skip=skip, R=R, svc_wls=svc_wls)


def label_raw(node, wl):
    if node in wl:
        return wl[node]["name"]
    for p in ("ext:", "svc:"):
        if node.startswith(p):
            return node[len(p):]
    if node.startswith("entry:"):
        return "외부(%s)" % node[6:]
    return node


# ─── 흐름 만들기 ─────────────────────────────────────────────────────────
MAX_HOPS, MAX_PATHS = 8, 200


def assemble(res):
    wl, E, prof_kind = res["wl"], res["E"], res["prof_kind"]
    real = {ab: d for ab, d in E.e.items() if ab[0] != "?"}

    def kind_of(node):
        if node in prof_kind and prof_kind[node]:
            return prof_kind[node]
        if node.startswith("ext:"):
            return "external"
        if node.startswith(("ingress:", "entry:")) or any(e["controller"] == node for e in res["entries"]):
            return "ingress"
        if node.startswith("gateway:"):
            return "gateway"
        kinds = [PROTO_KIND.get(p) for (x, y), d in real.items() if y == node for p in d["protos"]]
        for k in ("message-queue", "database", "cache"):
            if k in kinds:
                return k
        return "app"

    # 흐름에 쓰는 연결: 설정(또는 설정+로그), 방향이 있는 로그(주소·요청 ID). 허용 규칙만·IP만은 표에만.
    usable = {ab: d for ab, d in real.items() if verdict(d) in ("설정+로그", "설정", "로그", "허용규칙+로그")}
    entry_backs = {b for e in res["entries"] for b in e["backends"]}
    served = set(entry_backs)
    queue_edge = {}
    for (x, y), d in usable.items():
        q = any(PROTO_KIND.get(p) == "message-queue" for p in d["protos"]) or \
            (kind_of(y) == "message-queue" and not (d["protos"] & SYNC_PROTOS))
        queue_edge[(x, y)] = q
    for (x, y), d in usable.items():
        if not queue_edge[(x, y)] and not any(ev["type"] == "entry" for ev in d["evidence"]):
            served.add(y)
    role = {}
    for (x, y) in usable:
        if queue_edge[(x, y)]:
            role[(x, y)] = "produce" if x in served else "consume"
        else:
            role[(x, y)] = "call"

    succ = defaultdict(list)
    for (x, y), r in role.items():
        if r == "consume":
            succ[y].append((x, "consume", (x, y)))
        else:
            succ[x].append((y, r, (x, y)))
    for k in succ:
        succ[k].sort()

    flows, covered, on_flow = [], set(), set()

    def dfs(node, path, roles, eks, visited, only_new, out):
        nexts = [(n, r, ek) for (n, r, ek) in succ.get(node, [])
                 if n not in visited and (not only_new or ek not in covered)]
        if len(path) > 1 and node in on_flow:
            nexts = []  # 이미 다른 흐름에 있는 곳에 닿으면 거기서 멈춘다 (그 뒤는 그 흐름에 있다)
        if not nexts or len(path) >= MAX_HOPS or len(out) >= MAX_PATHS:
            out.append((path, roles, eks))
            return
        for n, r, ek in nexts:
            dfs(n, path + [n], roles + [r], eks + [ek], visited | {n}, only_new, out)

    def emit(path, roles, eks, origin, label=None):
        if len(path) < 2:
            return
        flows.append({"path": path, "roles": roles, "edges": eks, "origin": origin, "label": label})
        covered.update(eks)
        on_flow.update(path)

    seen_entry = set()
    for e in sorted(res["entries"], key=lambda e: (e["controller"], e["name"], e["host"], e["path"])):
        for b in e["backends"]:
            if (e["controller"], b) in seen_entry:
                continue
            seen_entry.add((e["controller"], b))
            out = []
            dfs(b, [e["controller"], b], ["call"], [(e["controller"], b)], {e["controller"], b}, False, out)
            for path, roles, eks in out:
                emit(path, roles, eks, "entry", "%s%s" % (e["host"] if e["host"] != "*" else "", e["path"] or ""))
    indeg = defaultdict(int)
    for k, outs in succ.items():
        for n, _, _ in outs:
            indeg[n] += 1
    for node in sorted(succ, key=lambda n: (indeg[n] > 0, label_raw(n, wl))):
        for _ in range(len(succ[node])):
            if all(ek in covered for _, _, ek in succ[node]):
                break
            out = []
            dfs(node, [node], [], [], {node}, True, out)
            if not any(len(p) > 1 for p, _, _ in out):
                break
            for path, roles, eks in out:
                emit(path, roles, eks, "inside")

    # 표시 이름: 워크로드 이름 (네임스페이스가 달라 겹치면 ns/이름)
    nodes = {n for f in flows for n in f["path"]} | {n for ab in E.e for n in ab if n != "?"}
    base = defaultdict(int)
    for n in nodes:
        base[label_raw(n, wl)] += 1

    def disp(n):
        lb = label_raw(n, wl)
        if n in wl and base[lb] > 1:
            return "%s/%s" % (wl[n]["ns"], lb)
        return lb

    id_pairs = [ab for ab, d in real.items() if any(ev["type"] == "log-id" for ev in d["evidence"])]
    names = set()
    for f in flows:
        pos = {n: i for i, n in enumerate(f["path"])}
        f["ids_confirm"] = [(x, y) for (x, y) in id_pairs if x in pos and y in pos and pos[x] < pos[y]]
        f["nodes"] = [disp(n) for n in f["path"]]
        f["kind"] = "queue" if any(r in ("produce", "consume") for r in f["roles"]) else "http"
        f["confidence"] = max((verdict(E.e[ek]) for ek in f["edges"] if ek in E.e), key=lambda v: RANK[v], default="설정")
        nm = " → ".join([f["nodes"][0], f["nodes"][-1]]) if f["origin"] == "inside" else \
            "%s → %s" % (f["label"] or f["nodes"][1], f["nodes"][-1])
        b, i = nm, 2
        while nm in names:
            nm, i = "%s (%d)" % (b, i), i + 1
        names.add(nm)
        f["name"] = nm
    return dict(flows=flows, role=role, kind_of=kind_of, disp=disp, usable=usable)


# ─── 출력 ──────────────────────────────────────────────────────────────
ROLE_KO = {"call": "호출", "produce": "넣기", "consume": "꺼내기(추정)"}


def to_shards(res, asm):
    comps = {}
    for f in asm["flows"]:
        for n in f["path"]:
            d = asm["disp"](n)
            if d in comps:
                continue
            if n in res["wl"]:
                comps[d] = {"workload": n, "kind": asm["kind_of"](n)}
            elif n.startswith("ext:"):
                comps[d] = {"external": True}
    return {"components": comps,
            "flows": [{"name": f["name"], "kind": f["kind"], "nodes": f["nodes"]} for f in asm["flows"]],
            "normal": []}


def knowledge_data(shards, asm):
    flows = {}
    for f, s in zip(asm["flows"], shards["flows"]):
        hops = [{"from": a_, "to": b_, "role": r} for a_, b_, r in zip(s["nodes"], s["nodes"][1:], f["roles"])]
        flows[s["name"]] = {"kind": s["kind"], "hops": hops}
    return {"schema": "koa.knowledge/v1", "components": shards["components"], "flows": flows, "normal": []}


def cell(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def ev_text(d, types):
    out = []
    for ev in d["evidence"]:
        if ev["type"] not in types:
            continue
        if ev["type"] == "log-ip":
            out.append("%s %d줄" % (ev["where"], ev.get("lines", 0)))
        elif ev["type"] == "log-name":
            out.append("%s에 `%s` %d줄" % (ev["where"], ev.get("value"), ev.get("lines", 0)))
        elif ev["type"] == "config" and ev.get("value"):
            out.append("%s `%s`" % (ev["where"], ev["value"]))
        else:
            out.append(ev["where"])
    return " · ".join(out[:3]) + (" 외 %d" % (len(out) - 3) if len(out) > 3 else "")


def render(cluster, res, asm, secs, src_label, limits, check):
    wl, E, ls, c = res["wl"], res["E"], res["log_stat"], res["counts"]
    disp = asm["disp"]
    L = []
    L.append("KOA 흐름 찾기 — %s · %s · %.1f초" % (cluster, src_label, secs))
    L.append("클러스터에 보낸 것: 목록 조회 %d · 객체 조회 %d · 로그 읽기 %d (모두 읽기). 앱 요청·접속 시험·exec·port-forward·Secret 읽기 없음."
             % (CALLS["list"], CALLS["get"], CALLS["logs"]))
    lg = ("파드 로그 %d개 (%s줄, 가장 오래된 줄 %s)" % (ls["pods"], format(ls["lines"], ","),
                                               ls["oldest"].strftime("%m-%d %H:%M UTC") if ls["oldest"] else "-")) if ls["pods"] else "파드 로그 안 읽음"
    L.append("읽은 것: 워크로드 %d · Service %d · Ingress %d · Route %d · ConfigMap %d · NetworkPolicy %d · %s"
             % (c["workloads"], c["services"], c["ingresses"], c["routes"], c.get("configmaps", 0), c["netpols"], lg))

    L.append("\n## 흐름 후보 (%d)" % len(asm["flows"]))
    if asm["flows"]:
        L.append("| # | 흐름 | 종류 | 근거 |")
        L.append("|---|---|---|---|")
        for i, f in enumerate(asm["flows"][:40], 1):
            chain = f["nodes"][0]
            for n, r in zip(f["nodes"][1:], f["roles"]):
                chain += " %s %s" % ({"produce": "─넣기→", "consume": "─꺼내기→"}.get(r, "→"), n)
            conf = f["confidence"] + (" · 요청ID 순서 확인(%s)" % ", ".join("%s→%s" % (disp(x), disp(y)) for x, y in f["ids_confirm"][:2])
                                      if f["ids_confirm"] else "")
            L.append("| %d | %s | %s%s | %s |" % (i, cell(chain), f["kind"], "" if f["origin"] == "entry" else " · 내부", cell(conf)))
        if len(asm["flows"]) > 40:
            L.append("(나머지 %d개는 JSON. --ns 로 좁힌다)" % (len(asm["flows"]) - 40))
    else:
        L.append("(없음)")

    rows = sorted(((ab, d) for ab, d in E.e.items() if ab[0] != "?"),
                  key=lambda x: (RANK[verdict(x[1])], disp(x[0][0]), disp(x[0][1])))
    L.append("\n## 연결 근거 (%d)" % len(rows))
    L.append("| 출발 → 도착 | 프로토콜·역할 | 설정 근거 | 로그 근거 | 판정 |")
    L.append("|---|---|---|---|---|")
    for (x, y), d in rows[:60]:
        r = asm["role"].get((x, y))
        proto = ",".join(sorted(d["protos"])) or "-"
        L.append("| %s → %s | %s%s | %s | %s | %s |" % (
            cell(disp(x)), cell(disp(y)), proto, " · %s" % ROLE_KO[r] if r else "",
            cell(ev_text(d, {"entry", "config", "netpol"}) or "-"), cell(ev_text(d, {"log-ip", "log-name", "log-id"}) or "-"),
            verdict(d)))
    if len(rows) > 60:
        L.append("(나머지 %d개는 JSON)" % (len(rows) - 60))

    ip_only = [(ab, d) for ab, d in E.e.items() if ab[0] == "?"]
    if ip_only:
        L.append("\n## 로그에서만 보인 연결 — 방향 미상 (%d)" % len(ip_only))
        for (_, x, y), d in sorted(ip_only, key=lambda t: (disp(t[0][1]), disp(t[0][2])))[:20]:
            L.append("- %s ↔ %s: %s" % (disp(x), disp(y), ev_text(d, {"log-ip"})))

    if res["ext_logs"]:
        L.append("\n## 로그에 나온 클러스터 밖 주소 — 흐름에 안 넣음, 확인용 (%d)" % len(res["ext_logs"]))
        L.append("| 워크로드 | 주소 | 줄 수 | 처음 ~ 마지막 (UTC) |")
        L.append("|---|---|---|---|")
        for (k, h), x in sorted(res["ext_logs"].items(), key=lambda t: -t[1]["lines"])[:20]:
            L.append("| %s | %s | %d | %s ~ %s |" % (cell(disp(k)), cell(h), x["lines"], iso(x["first"]) or "-", iso(x["last"]) or "-"))

    if res["entries"]:
        L.append("\n## 입구 (%d)" % len(res["entries"]))
        L.append("| 입구 | 주소 | Service | 워크로드 |")
        L.append("|---|---|---|---|")
        for e in res["entries"][:40]:
            L.append("| %s %s | %s%s | %s | %s |" % (e["via"], cell(e["name"]), cell(e["host"]), cell(e["path"]),
                                                  cell(e["service"]), cell(", ".join(disp(b) for b in e["backends"]) or "(없음)")))

    if res["notes"]["unknown"]:
        L.append("\n## 주소는 적혀 있는데 그런 Service 가 없음")
        seen = set()
        for u in res["notes"]["unknown"]:
            k = (u["from"], u["address"])
            if k not in seen:
                seen.add(k)
                L.append("- %s: `%s` (%s)" % (disp(u["from"]), u["address"], u["where"]))

    L.append("\n## 확인 못 한 것")
    for x in limits:
        L.append("- " + x)
    L.append("\n## 지식 형식 검사 (후보 그대로 저장한다면)")
    L.append("✅ 문제 없음" if not check else "\n".join("%s %s" % ("❌" if lv == "error" else "⚠", m) for lv, m in check[:12]))
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["mcp", "kubectl"], default="mcp")
    ap.add_argument("--kubeconfig", default=os.environ.get("KOA_KUBECONFIG", str(DEFAULT_KUBECONFIG)))
    ap.add_argument("--context", default=None)
    ap.add_argument("--name", default=None, help="클러스터 이름 (기본: 컨텍스트에서 -readonly 를 뺀 것)")
    ap.add_argument("--ns", default=None, help="이 네임스페이스의 워크로드·입구에서 출발하는 것만 (쉼표)")
    ap.add_argument("--skip-ns", default=SKIP_NS, help="뺄 네임스페이스 (쉼표, 기본 %s)" % SKIP_NS)
    ap.add_argument("--no-logs", action="store_true", help="파드 로그를 읽지 않는다 (설정만)")
    ap.add_argument("--tail", type=int, default=2000, help="컨테이너마다 읽을 마지막 줄 수 (기본 2000)")
    ap.add_argument("--pods", type=int, default=2, help="워크로드마다 로그를 읽을 파드 수 (기본 2)")
    ap.add_argument("--max-logs", type=int, default=150, help="로그를 읽을 컨테이너 최대 수 (기본 150)")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--shards", default=None, help="클러스터 지식 파편 형식으로 후보를 이 파일에 쓴다")
    ap.add_argument("--json", action="store_true", help="전체 결과 JSON 을 표준출력으로")
    ap.add_argument("--no-save", action="store_true")
    a = ap.parse_args()

    t0 = time.time()
    src = McpSource(a.timeout) if a.source == "mcp" else KubectlSource(a.kubeconfig, a.context)
    try:
        ctx = a.context or (yaml.safe_load(Path(src.kubeconfig).expanduser().read_text()) or {}).get("current-context", "")
    except OSError:
        ctx = ""
    a.cluster = a.name or re.sub(r"-readonly$", "", ctx or "cluster")
    limits = []
    try:
        with src:
            res = run(src, a, limits)
    except Exception as x:  # noqa: BLE001
        sys.exit("흐름 찾기 실패 (%s): %s" % (src.label, x))
    asm = assemble(res)
    secs = time.time() - t0

    n = res["notes"]
    for b in n["ip_broadcasters"]:
        limits.append("%s 로그에는 다른 워크로드 %d개의 파드 IP 가 나온다 (%d줄) — 관측·네트워크 도구로 보고 방향 미상 연결로 늘어놓지 않았다"
                      % (asm["disp"](b["workload"]), b["others"], b["lines"]))
    if a.source == "mcp" and not src.registered:
        limits.insert(0, "kubernetes MCP 가 이 프로필에 등록돼 있지 않아 카탈로그 정의로 띄웠다")
    if n["secret_env"]:
        xs = ["%s `%s`" % (asm["disp"](x["workload"]), x["env"]) for x in n["secret_env"]]
        limits.append("접속 주소로 보이는 env 가 Secret 에서 온다 — 값을 안 읽어 연결 대상을 모른다: %s%s"
                      % (", ".join(xs[:8]), " 외 %d" % (len(xs) - 8) if len(xs) > 8 else ""))
    if n["secret_envfrom"]:
        limits.append("envFrom Secret 을 쓰는 워크로드 %d개는 키 이름도 모른다" % len({x["workload"] for x in n["secret_envfrom"]}))
    ls = res["log_stat"]
    if a.no_logs:
        limits.append("로그를 읽지 않았다 (--no-logs): 모든 연결이 설정 근거뿐이다")
    else:
        limits.append("파드 로그는 컨테이너마다 마지막 %d줄, 워크로드마다 파드 %d개만 봤다. 재시작 전 로그·로그 저장소(OpenSearch·Loki 등)는 안 봤다"
                      % (a.tail, a.pods))
        limits.append("파드 IP 는 지금 떠 있는 파드 기준이다 — 재시작 전 IP 가 찍힌 줄은 누구인지 못 맞춘다. IPv6 는 아직 안 맞춘다")
        if res["host_net"]:
            limits.append("hostNetwork 파드 %d개는 노드 IP 를 써서 IP 근거에서 뺐다" % res["host_net"])
        if n["skipped_logs"]:
            limits.append("로그를 읽을 컨테이너가 많아 %d개를 건너뛰었다 (--max-logs)" % n["skipped_logs"])
        if ls["failed"]:
            limits.append("로그 읽기 실패 %d건" % ls["failed"])
        limits.append("로그 수집기·관측 도구(탐색 프로필·카탈로그 기준)와 hostNetwork 파드(노드 에이전트)의 로그는 남의 파드 IP·요청 ID 를 담고 있어 IP·ID 근거에서 뺐다")
        limits.append("입구 컨트롤러 로그의 관측에는 사람·도구(KOA 의 MCP 조회 포함)가 보낸 요청이 섞인다")
    limits.append("요청 내용으로 정해지는 대상(사용자가 넘긴 URL 등)은 설정에 없다 — 로그에 남아 있을 때만 보인다")
    if any(r == "consume" for r in asm["role"].values()):
        limits.append("큐의 넣기/꺼내기는 추정이다: 입구·호출을 받는 워크로드 = 넣는 쪽, 아니면 꺼내는 쪽")
    limits.append("파드가 없는 워크로드(0 replica, 끝난 Job)는 설정을 못 봤다. 사이드카가 라벨로 읽는 ConfigMap 은 안 봤다")
    if res["np_skipped"]:
        limits.append("NetworkPolicy 규칙 %d개는 matchExpressions·범위가 넓어 해석하지 않았다" % res["np_skipped"])

    shards = to_shards(res, asm)
    check = K.check(a.cluster, knowledge_data(shards, asm))
    E = res["E"]
    result = {
        "schema": "koa.flowmap/v1", "cluster": a.cluster, "at": iso(dt.datetime.now(UTC)), "source": src.label,
        "seconds": round(secs, 1), "scope": {"ns": sorted(res["scope"]) if res["scope"] else "all", "skip_ns": sorted(res["skip"])},
        "counts": res["counts"],
        "logs": {"containers": ls["pods"], "lines": ls["lines"], "oldest": iso(ls["oldest"]), "failed": ls["failed"],
                 "per_workload": {k: dict(v, oldest=iso(v["oldest"])) for k, v in ls["per"].items()}},
        "entries": res["entries"],
        "edges": [{"from": ab[0] if ab[0] != "?" else ab[1], "to": ab[1] if ab[0] != "?" else ab[2],
                   "directed": ab[0] != "?", "protos": sorted(d["protos"]), "role": asm["role"].get(ab),
                   "verdict": verdict(d), "evidence": d["evidence"]} for ab, d in E.e.items()],
        "flows": [{"name": f["name"], "kind": f["kind"], "nodes": f["nodes"], "path": f["path"], "roles": f["roles"],
                   "origin": f["origin"], "confidence": f["confidence"],
                   "request_id_order": [list(p) for p in f["ids_confirm"]]} for f in asm["flows"]],
        "unknown_addresses": n["unknown"], "secret_env": n["secret_env"], "secret_envfrom": n["secret_envfrom"],
        "external_in_logs": [{"workload": k, "host": h, "lines": x["lines"], "first": iso(x["first"]), "last": iso(x["last"])}
                             for (k, h), x in res["ext_logs"].items()],
        "cluster_calls": dict(CALLS),
        "limits": limits, "knowledge_check": [{"level": lv, "msg": m} for lv, m in check], "shards": shards,
    }
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    else:
        print(render(a.cluster, res, asm, secs, src.label, limits, check))
    if a.shards:
        Path(a.shards).write_text(json.dumps(shards, ensure_ascii=False, indent=1))
        if not a.json:
            print("\n후보 파편: %s → `python3 koa/knowledge.py merge %s %s` 로 더할 것을 미리 보고, --apply 로 지식에 더한다"
                  % (a.shards, a.cluster, a.shards))
    if not a.no_save:
        out = CLUSTERS / ("%s.flowmap" % a.cluster) / ("%s.json" % dt.datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str))
        if not a.json:
            print("전체 결과: %s" % str(out).replace(str(Path.home()), "~", 1))


if __name__ == "__main__":
    main()
