#!/usr/bin/env python3
"""KOA 탐색 결과 → 사람이 읽는 보고서(결과 표 + 제안).

discover.py 가 실행 끝에 자동으로 만든다(clusters/<이름>.report.md). 프로필만 있으면 따로 다시 만들 수도 있다.
  python3 koa/report.py kind-lab
"""
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import plan  # noqa: E402

KIND_KO = {
    "metrics": "지표", "metrics-exporter": "지표 수집기", "metrics-api": "metrics API", "alerting": "경보",
    "dashboard": "대시보드", "logs": "로그 저장소", "log-shipper": "로그 수집기", "telemetry-collector": "텔레메트리 수집기",
    "traces": "트레이스", "gitops": "GitOps", "message-queue": "메시지 큐", "cache": "캐시", "ingress": "Ingress",
}
PRIO = {1: "🔴 높음", 2: "🟡 중간", 3: "⚪ 낮음"}
OWNER = {"koa": "KOA(우리)", "cluster": "클러스터 운영팀"}


def _cell(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def table(head, rows):
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def build(profile, catalog, probed=True):
    k, acc, comps = profile["kubernetes"], profile["access"], profile["components"]
    cov = profile.get("metrics_coverage")
    advice = catalog.get("gap_advice", {})
    comp_rules = catalog["components"]
    L = []

    # ── 요약 ─────────────────────────────────────────
    L.append("# KOA 탐색 결과 — %s" % profile["cluster"])
    L.append("")
    L.append("> %s · 계정 `%s` · 읽기 전용 %s" % (profile["discovered_at"], profile["kubeconfig"]["identity"], "✅" if acc["read_only"] else "❌"))
    L.append("")
    for w in profile.get("warnings") or []:
        L.append("> ⚠️ " + w)
    L.append(table(["항목", "값"], [
        ["Kubernetes", "%s (%s, %s)" % (k["version"], ", ".join(k["provider"]), ", ".join(k["runtime"]))],
        ["노드", "%d/%d Ready (%s)" % (k["nodes"]["ready"], k["nodes"]["total"], ", ".join("%s %d" % kv for kv in k["nodes"]["roles"].items()))],
        ["네임스페이스 / 파드", "%d / %d" % (k["namespaces"], k["pods"])],
        ["막힌 권한", ", ".join(acc["cannot"])],
        ["metrics API", "있음" if profile["apis"]["metrics_api"] else "없음"],
        ["이벤트 보존", "%s (%s)" % (profile["events"].get("ttl", "미확인"), profile["events"].get("ttl_source"))],
        ["Prometheus 수집 대상", ("%d개 중 %d개 up (%s)" % (cov["targets"], cov["up"], cov["source"])) if cov else "확인 안 함"],
    ]))

    # ── 구성요소 ─────────────────────────────────────
    L.append("\n## 1. 찾은 구성요소\n")
    rows = []
    order = sorted(comps.items(), key=lambda kv: (list(KIND_KO).index(kv[1]["kind"]) if kv[1]["kind"] in KIND_KO else 99, kv[0]))
    for name, c in order:
        insts = c["instances"]
        where = ", ".join("%s/%s" % (i["namespace"], i["workload"].split("/", 1)[1]) for i in insts) or "(CRD 만)"
        ready = "%d/%d" % (sum(i["ready"] for i in insts), sum(i["pods"] for i in insts)) if insts else "-"
        urls = sorted({a for i in insts for a in (i["access"] or [])})
        probes = [p for i in insts for p in i.get("probe", [])]
        if urls:
            st = ", ".join(str(p.get("http_status", "실패")) + (" 🔒" if p.get("auth_required") else "") for p in probes) if probes else ("-" if probed else "probe 안 함")
            access = "%s (%s)" % (", ".join(urls), st)
        else:
            access = "—"
        if cov and insts:
            scraped = "✅" if any(i.get("scraped") for i in insts) else "❌"
        else:
            scraped = "?"
        rows.append([name, KIND_KO.get(c["kind"], c["kind"]), where, ready, access, scraped])
    L.append(table(["구성요소", "종류", "위치", "Ready", "클러스터 밖 주소 (probe)", "지표 수집"], rows))
    L.append("\n🔒 = 인증 필요 · 지표 수집 = Prometheus 가 이 구성요소를 수집하는지 (`?` = 확인 못 함)")

    # ── MCP ────────────────────────────────────────
    items, no_adapter = plan.build_plan(profile, catalog, set())
    L.append("\n## 2. 붙일 수 있는 MCP\n")
    rows = []
    for it in items:
        todo = "; ".join(it["todo"]) or "-"
        rows.append([it["name"], it["status"], it["why"], it["state"], it["action"].split(" (")[0], todo])
    for c, why in no_adapter:
        rows.append([c, "-", "%s 감지" % c, "-", "붙이지 않음", why])
    L.append(table(["MCP", "검증", "근거", "현재", "판단", "필요한 것"], rows))

    # ── 빈 곳과 제안 ──────────────────────────────────
    L.append("\n## 3. 빈 곳과 제안\n")
    gaps = []
    for g in profile["gaps"]:
        a = advice.get(g["id"], {})
        gaps.append((a.get("priority", 3), g, a))
    gaps.sort(key=lambda x: x[0])
    L.append(table(["#", "우선", "빈 곳", "분석에 미치는 영향", "누가"],
                   [[n + 1, PRIO[p], g["detail"].split(" → ")[0], a.get("impact", g["detail"].split(" → ")[-1]), OWNER.get(a.get("owner"), "-")]
                    for n, (p, g, a) in enumerate(gaps)]))

    for n, (p, g, a) in enumerate(gaps):
        L.append("\n### %d. %s\n" % (n + 1, a.get("title", g["detail"].split(" → ")[0])))
        steps = list(a.get("steps", []))
        if g["id"] == "not-scraped" and cov:
            for name in cov.get("not_scraped", []):
                hint = comp_rules.get(name, {}).get("metrics_hint")
                steps.append("`%s` — %s" % (name, hint or "지표 노출 방법 확인 필요"))
        if g["id"] == "no-access":
            for name in g["detail"].split(": ", 1)[1].split(", "):
                svcs = [s for i in comps.get(name, {}).get("instances", []) for s in i.get("services", [])]
                hint = comp_rules.get(name, {}).get("access_hint")
                steps.append("`%s` — %s%s" % (name, hint or "Ingress(인증 포함)로 연다", (" (서비스: %s)" % ", ".join(svcs)) if svcs else ""))
        for s in steps:
            L.append("- " + s.strip())

    # ── 다음 단계 ────────────────────────────────────
    koa_now = [it for it in items if it["status"] == "candidate" and it["action"].startswith("보류 (준비")
               and not any("접근 주소가 없다" in t for t in it["todo"])]
    L.append("\n## 4. 다음 단계\n")
    nxt = []
    if koa_now:
        nxt.append("**KOA 가 바로 할 수 있는 것** — 접근 주소가 이미 있는 MCP 를 붙여 verified 로 올린다: %s"
                   % ", ".join("`%s`" % it["name"] for it in koa_now))
    cl = [a.get("title", g["detail"]) for p, g, a in gaps if a.get("owner") == "cluster" and p == 1]
    if cl:
        nxt.append("**클러스터 운영팀에 요청할 것 (우선 높음)** — " + " / ".join(cl))
    nxt.append("변경 후 `python3 koa/discover.py --probe` 로 다시 탐색해 이 보고서가 바뀌는지 확인한다.")
    L += ["%d. %s" % (i + 1, s) for i, s in enumerate(nxt)]
    return "\n".join(L) + "\n"


def main():
    if len(sys.argv) != 2:
        sys.exit("사용법: python3 koa/report.py <클러스터 이름>")
    prof = REPO / "clusters" / ("%s.yaml" % sys.argv[1])
    profile = yaml.safe_load(prof.read_text())
    catalog = yaml.safe_load((REPO / "koa" / "catalog.yaml").read_text())
    md = build(profile, catalog)
    out = prof.with_suffix(".report.md")
    out.write_text(md)
    print(md)
    print("---\n보고서: %s" % out)


if __name__ == "__main__":
    main()
