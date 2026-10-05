#!/usr/bin/env python3
"""MCP 계정이 정말 읽기 전용인지 서버 쪽에서 확인한다. 쓰기는 시도하지 않는다.

  python3 koa/readonly.py                  # 이 프로필에 등록된 MCP 전부
  python3 koa/readonly.py grafana argocd   # 일부만

조회 방법은 MCP 가 원래 쓰는 백엔드의 '권한 질의' API(GET 이나 *-review) 뿐이다.
  kubernetes  SelfSubjectRulesReview(네임스페이스마다) + SelfSubjectAccessReview(근거 확인)
  grafana     /api/user, /api/access-control/user/permissions
  argocd      /api/v1/account/can-i/<자원>/<동작>/*
  opensearch  /_cat/plugins → 보안 플러그인이 있으면 /_plugins/_security/authinfo 의 역할
  prometheus  /api/v1/status/flags (admin·remote-write API 꺼져 있는지)

판정
  ok    서버가 쓰기를 막는다는 것을 확인했다
  warn  서버가 계정별 쓰기 제한을 못 하거나 일부 쓰기가 열려 있다. 쓰기 차단은 MCP 계층
        (조회 도구만 노출 + 서버 읽기 전용 모드)에만 의존한다. 등록은 하고 보고서에 적는다
  fail  읽기 전용 계정을 따로 받을 수 있는데 쓰기·관리 권한이 있는 계정을 받았다. 등록하지 않는다
  skip  확인할 수 없다 (접속 정보 없음 등)
비밀 값은 출력하지 않는다.
"""
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import INSTALLED, read_env  # noqa: E402

WRITE_VERBS = {"create", "update", "patch", "delete", "deletecollection", "escalate", "bind", "impersonate", "approve", "sign", "*"}
SELF_REVIEWS = {"selfsubjectaccessreviews", "selfsubjectrulesreviews", "selfsubjectreviews"}
# Calico API 서버는 networkpolicies 를 모든 인증 사용자에게 열어 두고(calico-tiered-policy-passthrough),
# 실제 허용 여부는 tier.<자원> 권한으로 다시 판정한다 → tier 권한까지 막혀 있으면 실제로는 거부된다.
PASSTHROUGH = {("projectcalico.org", "networkpolicies"): "tier.networkpolicies",
               ("projectcalico.org", "globalnetworkpolicies"): "tier.globalnetworkpolicies",
               ("projectcalico.org", "stagednetworkpolicies"): "tier.stagednetworkpolicies",
               ("projectcalico.org", "stagedglobalnetworkpolicies"): "tier.stagedglobalnetworkpolicies"}


def result(verdict, facts, why=""):
    return {"verdict": verdict, "facts": facts, "why": why}


def _env():
    e = read_env()
    return e if INSTALLED else {**os.environ, **e}


def _get(url, headers=None, timeout=10):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as x:
        return x.code, x.read().decode(errors="replace")
    except Exception as x:  # 연결 실패 등
        return 0, str(x)


# ── kubernetes ──────────────────────────────────────────────
def _kubectl(kubeconfig, *args, stdin=None):
    p = subprocess.run(["kubectl", "--kubeconfig", kubeconfig] + list(args), input=stdin,
                       capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout, p.stderr


def _review(kubeconfig, kind, spec):
    body = json.dumps({"apiVersion": "authorization.k8s.io/v1", "kind": kind, "spec": spec})
    plural = kind.lower() + "s"
    rc, out, err = _kubectl(kubeconfig, "create", "--raw", "/apis/authorization.k8s.io/v1/" + plural, "-f", "-", stdin=body)
    if rc:
        raise RuntimeError(err.strip()[:200])
    return json.loads(out)["status"]


def _ssar(kubeconfig, ns, verb, group, resource):
    attrs = {"verb": verb, "group": group, "resource": resource}
    if ns:
        attrs["namespace"] = ns
    return _review(kubeconfig, "SelfSubjectAccessReview", {"resourceAttributes": attrs})


def check_kubernetes(spec, env):
    kc = (spec.get("env") or {}).get("KUBECONFIG_PATH", "").replace("${userHome}", str(Path.home()))
    kc = kc or str(Path.home() / ".kube" / "hermes-readonly.yaml")
    if Path(kc).expanduser().resolve() == (Path.home() / ".kube" / "config").resolve():
        return result("fail", ["kubeconfig 가 관리자용 ~/.kube/config"], "읽기 전용 kubeconfig 로 바꿔야 한다")
    rc, out, err = _kubectl(kc, "get", "namespaces", "-o", "jsonpath={.items[*].metadata.name}")
    if rc:
        return result("skip", ["네임스페이스 조회 실패: %s" % err.strip()[:150]])
    namespaces = out.split()
    # 관리자급 권한: 하나라도 있으면 fail (discover 의 SHOULD_BE_DENIED 와 같은 기준)
    admin = []
    for verb, group, res in [("list", "", "secrets"), ("create", "", "pods/exec"), ("delete", "", "pods"),
                             ("patch", "apps", "deployments"), ("create", "rbac.authorization.k8s.io", "clusterrolebindings")]:
        r, _, sub = res.partition("/")
        attrs = {"verb": verb, "group": group, "resource": r}
        if sub:
            attrs["subresource"] = sub
        if _review(kc, "SelfSubjectAccessReview", {"resourceAttributes": attrs}).get("allowed"):
            admin.append("%s %s" % (verb, res))
    if admin:
        return result("fail", ["허용됨: " + ", ".join(admin)], "읽기 전용 ServiceAccount(k8s/hermes-readonly.yaml) kubeconfig 를 받아야 한다")
    # 그 밖의 쓰기 규칙: 네임스페이스마다 규칙 목록을 받아 쓰기 동사를 찾는다
    writes = {}
    incomplete = False
    for ns in namespaces:
        st = _review(kc, "SelfSubjectRulesReview", {"namespace": ns})
        incomplete |= bool(st.get("incomplete"))
        for rule in st.get("resourceRules", []):
            verbs = WRITE_VERBS & set(rule.get("verbs", []))
            res = [r for r in rule.get("resources", []) if r not in SELF_REVIEWS]
            if not verbs or not res:
                continue
            for g in rule.get("apiGroups", [""]):
                for r in res:
                    writes.setdefault((g, r), {"verbs": set(), "ns": set()})
                    writes[(g, r)]["verbs"] |= verbs
                    writes[(g, r)]["ns"].add(ns)
    facts = ["관리자급 권한(secrets·exec·삭제·RBAC) 없음, 네임스페이스 %d개 규칙 확인" % len(namespaces)]
    open_writes = []
    for (g, r), w in sorted(writes.items()):
        name = "%s.%s" % (r, g) if g else r
        ns = "default" if "default" in w["ns"] else sorted(w["ns"])[0]
        why = _ssar(kc, ns, "create", g, r).get("reason", "")
        m = re.search(r'(ClusterRoleBinding|RoleBinding) "([^"]+)"', why)
        via = "%s %s" % (m.group(1), m.group(2)) if m else "RBAC"
        tier = PASSTHROUGH.get((g, r))
        if tier and not _ssar(kc, ns, "create", g, tier).get("allowed"):
            facts.append("%s 쓰기 규칙(%s)은 Calico 통과용 — %s 권한이 없어 실제로는 거부" % (name, via, tier))
            continue
        open_writes.append("%s %s (%s)" % (name, "/".join(sorted(w["verbs"])), via))
    if incomplete:
        facts.append("규칙 목록이 불완전하다고 응답(웹훅 인가 등) — 일부는 확인하지 못함")
    if open_writes:
        return result("warn", facts + ["쓰기 열림: " + "; ".join(open_writes)],
                      "클러스터 RBAC 가 모든 계정에 준 쓰기다. MCP 는 조회 도구 4개 + 비파괴 모드라 쓸 수 없다")
    return result("ok", facts)


# ── grafana ─────────────────────────────────────────────────
READ_ACTIONS = re.compile(r":(read|list|get|query|access|explore)$")


def check_grafana(spec, env):
    url, tok = env.get("GRAFANA_URL"), env.get("GRAFANA_SERVICE_ACCOUNT_TOKEN")
    if not (url and tok):
        return result("skip", ["GRAFANA_URL·토큰 없음"])
    h = {"Authorization": "Bearer " + tok}
    s, b = _get(url.rstrip("/") + "/api/user", h)
    if s != 200:
        return result("skip", ["/api/user 응답 %s" % s])
    u = json.loads(b)
    if u.get("isGrafanaAdmin"):
        return result("fail", ["서버 관리자(isGrafanaAdmin) 계정"], "Viewer 서비스 계정 토큰을 받아야 한다")
    s, b = _get(url.rstrip("/") + "/api/access-control/user/permissions", h)
    if s != 200:
        return result("warn", ["계정 %s, 권한 목록 조회 불가(%s)" % (u.get("login"), s)], "쓰기 차단은 --disable-write + 조회 도구 목록에 의존")
    acts = sorted(json.loads(b))
    writes = [a for a in acts if not READ_ACTIONS.search(a)]
    if writes:
        return result("fail", ["계정 %s, 쓰기 권한 %d개: %s" % (u.get("login"), len(writes), ", ".join(writes[:6]))],
                      "Viewer 역할 서비스 계정 토큰을 받아야 한다")
    return result("ok", ["계정 %s, 권한 %d개 모두 조회용(read/list/get/query)" % (u.get("login"), len(acts))])


# ── argocd ──────────────────────────────────────────────────
ARGO_WRITES = [("applications", "sync"), ("applications", "update"), ("applications", "delete"), ("applications", "create"),
               ("applications", "override"), ("applications", "action"), ("exec", "create"), ("projects", "update"),
               ("clusters", "update"), ("repositories", "create"), ("accounts", "update"), ("gpgkeys", "create")]


def check_argocd(spec, env):
    url, tok = env.get("ARGOCD_BASE_URL"), env.get("ARGOCD_API_TOKEN")
    if not (url and tok):
        return result("skip", ["ARGOCD_BASE_URL·토큰 없음"])
    h = {"Authorization": "Bearer " + tok}
    allowed, readable = [], None
    for res, act in [("applications", "get")] + ARGO_WRITES:
        obj = "*" if res in ("projects", "clusters", "repositories", "accounts", "gpgkeys") else "*/*"
        s, b = _get("%s/api/v1/account/can-i/%s/%s/%s" % (url.rstrip("/"), res, act, obj), h)
        if s != 200:
            return result("skip", ["can-i API 응답 %s" % s])
        yes = json.loads(b).get("value") == "yes"
        if act == "get":
            readable = yes
        elif yes:
            allowed.append("%s %s" % (res, act))
    if allowed:
        return result("fail", ["허용됨: " + ", ".join(allowed)], "role:readonly 계정 토큰을 받아야 한다")
    return result("ok", ["can-i: 앱 조회 %s, 쓰기·실행 %d종 모두 no" % ("yes" if readable else "no", len(ARGO_WRITES))])


# ── opensearch ──────────────────────────────────────────────
READONLY_ROLES = {"readall", "readall_and_monitor", "opensearch_dashboards_read_only", "kibana_read_only"}
ADMIN_ROLES = {"all_access", "security_manager", "manage_snapshots"}


def check_opensearch(spec, env):
    url = env.get("OPENSEARCH_URL")
    if not url:
        return result("skip", ["OPENSEARCH_URL 없음"])
    h = {}
    if env.get("OPENSEARCH_USERNAME"):
        cred = "%s:%s" % (env["OPENSEARCH_USERNAME"], env.get("OPENSEARCH_PASSWORD", ""))
        h["Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()
    s, b = _get(url.rstrip("/") + "/_cat/plugins?format=json", h)
    if s != 200:
        return result("skip", ["/_cat/plugins 응답 %s" % s])
    plugins = {p.get("component") for p in json.loads(b)}
    no_sec = "opensearch-security" not in plugins
    if not no_sec:
        s, b = _get(url.rstrip("/") + "/_plugins/_security/authinfo", h)
        # 플러그인은 깔려 있어도 DISABLE_SECURITY_PLUGIN=true 면 보안 API 자체가 없다(400 no handler)
        no_sec = s == 400 and "no handler" in b
    if no_sec:
        return result("warn", ["보안 플러그인이 없거나 꺼져 있다 → OpenSearch 자체에 계정·권한이 없다 (앞단 Ingress 인증만)"],
                      "이 계정은 서버에서 쓰기가 가능하다. 쓰기 차단은 MCP 계층(ALLOW_WRITE=false + 조회 도구 5개)에만 의존")
    if s != 200:
        return result("skip", ["authinfo 응답 %s" % s])
    roles = set(json.loads(b).get("roles", []))
    if roles & ADMIN_ROLES:
        return result("fail", ["역할: " + ", ".join(sorted(roles))], "readall 등 읽기 전용 역할 계정을 받아야 한다")
    if roles and roles <= READONLY_ROLES:
        return result("ok", ["역할: " + ", ".join(sorted(roles))])
    return result("warn", ["역할: " + (", ".join(sorted(roles)) or "없음")],
                  "알려진 읽기 전용 역할이 아니라 세부 권한은 확인하지 못함 (역할 정의 조회는 관리자 권한 필요)")


# ── prometheus ──────────────────────────────────────────────
def check_prometheus(spec, env):
    url = env.get("PROMETHEUS_URL")
    if not url:
        return result("skip", ["PROMETHEUS_URL 없음"])
    s, b = _get(url.rstrip("/") + "/api/v1/status/flags")
    if s != 200:
        return result("skip", ["/api/v1/status/flags 응답 %s" % s])
    f = json.loads(b).get("data", {})
    on = [k for k in ("web.enable-admin-api", "web.enable-remote-write-receiver", "web.enable-otlp-receiver") if f.get(k) == "true"]
    facts = ["PromQL 은 쓰기를 표현할 수 없다"]
    if f.get("web.enable-lifecycle") == "true":
        facts.append("lifecycle API(reload/quit) 켜짐 — MCP 에는 이를 부르는 도구가 없다")
    if on:
        return result("warn", facts + ["켜져 있음: " + ", ".join(on)], "MCP 도구로는 부를 수 없지만 서버에 쓰기 경로가 열려 있다")
    return result("ok", facts + ["admin·remote-write·OTLP 수신 API 꺼짐"])


CHECKS = {"kubernetes": check_kubernetes, "grafana": check_grafana, "argocd": check_argocd,
          "opensearch": check_opensearch, "prometheus": check_prometheus}


def check(name, spec):
    fn = CHECKS.get(name)
    if not fn:
        return result("skip", ["이 MCP 용 권한 확인이 없다"])
    try:
        return fn(spec or {}, _env())
    except Exception as x:
        return result("skip", ["확인 중 오류: %s" % str(x)[:200]])


def main():
    from mcp_client import registered
    servers = registered()
    names = sys.argv[1:] or list(servers)
    failed = False
    for n in names:
        r = check(n, servers.get(n))
        failed |= r["verdict"] == "fail"
        print("## %s: %s" % (n, r["verdict"]))
        for f in r["facts"]:
            print("   - %s" % f)
        if r["why"]:
            print("   → %s" % r["why"])
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
