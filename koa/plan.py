#!/usr/bin/env python3
"""KOA 2단계 — 클러스터 프로필을 보고 붙일 MCP 서버를 정한다.

  python3 koa/plan.py kind-lab                      # 계획만 보여준다 (아무것도 바꾸지 않음)
  python3 koa/plan.py kind-lab --apply              # 준비된 verified 서버를 Hermes 에 등록
  python3 koa/plan.py kind-lab --apply --with prometheus   # candidate 도 명시해서 등록

등록은 `hermes config set mcp_servers.<이름> '<json>'` 으로 한다 → config.yaml(이 저장소) 이 바뀐다.
비밀 값은 HERMES_HOME/.env 에서 "있는지"만 확인하고 내용은 읽어 출력하지 않는다.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import CATALOG, CLUSTERS, CONFIG, ENV_HINT, HERMES_BIN, INSTALLED, hermes_env, read_env  # noqa: E402


def env_keys_set():
    """값이 비어 있지 않은 변수 이름만 돌려준다.
    설치본은 프로필 .env 만 본다: 에이전트 터미널은 다른 프로필(.env)의 값을 환경변수로 물려받을 수 있어서,
    환경변수까지 보면 이 프로필에 없는 토큰을 '있다'고 착각한다."""
    keys = {k for k, v in read_env().items() if v}
    if not INSTALLED:
        keys |= {k for k, v in os.environ.items() if v}
    return keys


def access_urls(profile, comp):
    c = profile["components"].get(comp) or {}
    return sorted({u for i in c.get("instances", []) for u in (i.get("access") or [])})


def build_plan(profile, catalog, with_names):
    have_env = env_keys_set()
    current = (yaml.safe_load(CONFIG.read_text()) or {}).get("mcp_servers") or {}
    comps = profile["components"]
    k8s_read = "list pods" in profile["access"]["can"] and "get pods/log" in profile["access"]["can"]
    plan = []
    for name, m in catalog["mcp_servers"].items():
        cond = m["when"]
        if cond == "k8s_read":
            matched = k8s_read
            why = "k8s 조회 권한 있음" if matched else "k8s 조회 권한 없음"
        else:
            comp = cond.split(":", 1)[1]
            matched = comp in comps
            why = ("%s 감지" % comp) if matched else ("%s 없음" % comp)
        if not matched:
            continue
        item = {"name": name, "status": m["status"], "why": why, "note": m.get("note", ""), "server": m["server"], "todo": []}
        missing = [e for e in m.get("requires_env", []) if e not in have_env]
        if missing:
            item["todo"].append("%s 에 값 채우기: %s" % (ENV_HINT, ", ".join(missing)))
        if cond != "k8s_read":
            urls = access_urls(profile, cond.split(":", 1)[1])
            if urls and m.get("url_env") in missing:
                item["todo"].append("%s 제안값: %s" % (m["url_env"], urls[0]))
            if not urls and m.get("url_env"):
                item["todo"].append("클러스터 밖 접근 주소가 없다 → 지금 범위에서는 붙일 수 없다 (주소를 따로 알면 %s 에 직접 지정)" % m["url_env"])
            if m.get("url_must_be_root") and urls and all(urlparse(u).path.strip("/") for u in urls):
                item["todo"].append("접근 주소가 하위 경로(%s)인데 이 MCP 는 경로를 버리고 호스트 루트로 요청한다 → 지금 범위에서는 붙일 수 없다" % urls[0])
                item["blocked"] = True
        if name in current:
            item["state"] = "등록됨 (동일)" if current[name] == m["server"] else "등록됨 (카탈로그와 다름 → --apply 시 갱신)"
        else:
            item["state"] = "미등록"
        if item["state"] == "등록됨 (동일)":
            item["action"] = "유지"
        elif item.get("blocked"):
            item["action"] = "보류 (붙일 수 없음)"
        elif item["todo"] and not (name in current and not missing):
            item["action"] = "보류 (준비 필요)"
        elif m["status"] == "verified" or name in with_names:
            item["action"] = "등록"
        else:
            item["action"] = "보류 (candidate — --with %s 로 명시해야 등록)" % name
        plan.append(item)
    no_adapter = [(c, catalog.get("no_adapter", {}).get(c)) for c in comps if c in catalog.get("no_adapter", {})]
    return plan, no_adapter


def apply(item):
    cmd = [str(HERMES_BIN), "config", "set", "mcp_servers.%s" % item["name"], json.dumps(item["server"], ensure_ascii=False)]
    p = subprocess.run(cmd, capture_output=True, text=True, env=hermes_env())
    if p.returncode != 0:
        raise SystemExit("등록 실패 %s: %s" % (item["name"], (p.stderr or p.stdout).strip()[:300]))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cluster", help="clusters/<이름>.yaml 의 이름")
    ap.add_argument("--apply", action="store_true", help="'등록' 항목을 Hermes 설정에 쓴다")
    ap.add_argument("--with", dest="with_names", default="", help="함께 등록할 candidate 이름 (쉼표 구분)")
    a = ap.parse_args()

    prof_path = CLUSTERS / ("%s.yaml" % a.cluster)
    if not prof_path.exists():
        sys.exit("%s 없음 → 먼저 python3 koa/discover.py 실행" % prof_path)
    profile = yaml.safe_load(prof_path.read_text())
    catalog = yaml.safe_load(CATALOG.read_text())
    with_names = {n.strip() for n in a.with_names.split(",") if n.strip()}
    unknown = with_names - set(catalog["mcp_servers"])
    if unknown:
        sys.exit("카탈로그에 없는 이름: %s" % ", ".join(sorted(unknown)))

    if not profile["access"].get("read_only"):
        sys.exit("프로필상 계정이 읽기 전용이 아니다(%s). 읽기 전용 kubeconfig 로 discover 를 다시 돌려라." % profile["kubeconfig"]["identity"])

    plan, no_adapter = build_plan(profile, catalog, with_names)
    print("클러스터 %s (프로필 %s)\n" % (a.cluster, profile["discovered_at"]))
    for it in plan:
        print("● %-11s [%s] %s — %s" % (it["name"], it["status"], it["why"], it["state"]))
        print("    할 일: %s" % it["action"])
        for t in it["todo"]:
            print("    - %s" % t)
        if it["note"]:
            print("    참고: %s" % it["note"])
    if no_adapter:
        print("\nMCP 를 붙이지 않는 구성요소")
        for c, why in no_adapter:
            print("  %-11s %s" % (c, why))

    todo = [it for it in plan if it["action"] == "등록"]
    if not a.apply:
        print("\n(계획만 표시) 등록 대상: %s" % (", ".join(it["name"] for it in todo) or "없음"))
        if todo:
            print("적용: python3 koa/plan.py %s --apply%s" % (a.cluster, (" --with " + ",".join(sorted(with_names))) if with_names else ""))
        return
    if not todo:
        print("\n등록할 것이 없다.")
        return
    for it in todo:
        apply(it)
        print("등록: mcp_servers.%s" % it["name"])
    print("\n완료 (%s). 데스크톱 앱 재시작 (게이트웨이를 쓰면 ~/.local/bin/hermes gateway restart 도)." % CONFIG)


if __name__ == "__main__":
    main()
