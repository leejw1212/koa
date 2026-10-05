#!/usr/bin/env python3
"""HERMES_HOME/.env 의 토큰으로 읽기는 되고 쓰기는 거부되는지 확인한다. 토큰은 출력하지 않는다.
Argo CD 쓰기는 실제로 시도하지 않고 can-i API 로 묻는다 (권한이 새고 있어도 아무것도 바뀌지 않게)."""
import json, sys, urllib.request, urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "koa"))
from paths import read_env  # noqa: E402

env = read_env()


def req(method, url, token=None, body=None):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    r = urllib.request.Request(url, method=method, headers=h, data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None


a, at = env["ARGOCD_BASE_URL"], env["ARGOCD_API_TOKEN"]
s, d = req("GET", a + "/api/v1/applications", at)
print("argocd  읽기  GET applications            ->", s, "(%d개)" % len((d or {}).get("items") or []))
for res, act in [("applications", "get"), ("applications", "sync"), ("applications", "delete"),
                 ("applications", "create"), ("applications", "update"), ("clusters", "get"), ("repositories", "get"), ("projects", "delete")]:
    s, d = req("GET", "%s/api/v1/account/can-i/%s/%s/*" % (a, res, act), at)
    print("argocd  can-i %-28s -> %s" % ("%s %s" % (act, res), (d or {}).get("value", s)))

g, gt = env["GRAFANA_URL"], env["GRAFANA_SERVICE_ACCOUNT_TOKEN"]
print("grafana 읽기  GET search                  ->", req("GET", g + "/api/search", gt)[0])
print("grafana 읽기  GET datasources             ->", req("GET", g + "/api/datasources", gt)[0])
s, d = req("POST", g + "/api/dashboards/db", gt, {"dashboard": {"title": "koa-write-test"}, "overwrite": False})
print("grafana 쓰기  POST dashboards/db          ->", s)
if s == 200 and d:  # 권한이 샜다면 만든 것을 지운다
    req("DELETE", g + "/api/dashboards/uid/" + d["uid"], gt)
s, d = req("POST", g + "/api/folders", gt, {"title": "koa-write-test"})
print("grafana 쓰기  POST folders                ->", s)
if s == 200 and d:
    req("DELETE", g + "/api/folders/" + d["uid"], gt)

p = env["PROMETHEUS_URL"]
print("prom    읽기  GET query?query=up          ->", req("GET", p + "/api/v1/query?query=up")[0])
print("prom    관리  POST admin/tsdb/snapshot    ->", req("POST", p + "/api/v1/admin/tsdb/snapshot")[0])
