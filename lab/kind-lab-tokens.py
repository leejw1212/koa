#!/usr/bin/env python3
"""kind-lab 테스트용: Argo CD·Grafana 읽기 전용 토큰을 발급해 HERMES_HOME/.env 에 넣는다. 토큰은 출력하지 않는다."""
import base64, json, os, subprocess, sys, time, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "koa"))
from paths import ENV_FILE as ENV  # noqa: E402
K = ["kubectl", "--kubeconfig", str(Path.home() / ".kube" / "config"), "--context", "kind-lab"]


def secret(ns, name, key):
    out = subprocess.run(K + ["-n", ns, "get", "secret", name, "-o", "jsonpath={.data.%s}" % key.replace(".", "\\.")],
                         capture_output=True, text=True, check=True).stdout
    return base64.b64decode(out).decode()


def call(method, url, body=None, headers=None, auth=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    if auth:
        h["Authorization"] = "Basic " + base64.b64encode(("%s:%s" % auth).encode()).decode()
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def set_env(values):
    lines = ENV.read_text().splitlines() if ENV.exists() else []
    keys = set(values)
    lines = [l for l in lines if l.split("=", 1)[0].strip() not in keys]
    lines.append("")
    lines.append("# kind-lab 테스트 (koa) — %s" % time.strftime("%Y-%m-%d"))
    lines += ["%s=%s" % (k, v) for k, v in values.items()]
    ENV.write_text("\n".join(lines).strip("\n") + "\n")
    os.chmod(ENV, 0o600)


def argocd():
    base = "http://localhost/argocd"
    pw = secret("argocd", "argocd-initial-admin-secret", "password")
    sess = call("POST", base + "/api/v1/session", {"username": "admin", "password": pw})["token"]
    tok = call("POST", base + "/api/v1/account/koa-readonly/token", {"name": "koa-mcp"},
               headers={"Authorization": "Bearer " + sess})["token"]
    return {"ARGOCD_BASE_URL": base, "ARGOCD_API_TOKEN": tok}


def grafana():
    base = "http://localhost/grafana"
    auth = (secret("monitoring", "monitoring-grafana", "admin-user"), secret("monitoring", "monitoring-grafana", "admin-password"))
    found = call("GET", base + "/api/serviceaccounts/search?query=koa-readonly", auth=auth)["serviceAccounts"]
    sa = found[0] if found else call("POST", base + "/api/serviceaccounts", {"name": "koa-readonly", "role": "Viewer"}, auth=auth)
    tok = call("POST", base + "/api/serviceaccounts/%d/tokens" % sa["id"], {"name": "koa-mcp-%d" % int(time.time())}, auth=auth)["key"]
    return {"GRAFANA_URL": base, "GRAFANA_SERVICE_ACCOUNT_TOKEN": tok}, sa.get("role")


if __name__ == "__main__":
    vals = {"PROMETHEUS_URL": "http://localhost/prometheus"}
    vals.update(argocd())
    g, role = grafana()
    vals.update(g)
    set_env(vals)
    print("written to %s: %s (grafana SA role=%s)" % (ENV, ", ".join(vals), role))
