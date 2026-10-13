#!/usr/bin/env python3
"""같은 장애 질문을 KOA 프로필과 일반(plain) 프로필에 동시에 던지고 결과를 나란히 비교한다 (lab 전용).

  python3 lab/chaos/compare.py                     # 지금 걸린 장애의 질문으로 koa · plain 동시 실행
  python3 lab/chaos/compare.py --profiles koa,plain --budget 900
  python3 lab/chaos/compare.py --repeat 3          # 같은 질문을 세 번씩 (한 판은 운이 섞인다 → 중앙값으로 견준다)
  python3 lab/chaos/compare.py --score <run 폴더>   # 이미 돈 결과만 다시 채점

결과: ~/.koa-chaos/runs/<시각>-<시나리오>/
  <profile>.jsonl   hermes stream-json 원본 (도구 호출 포함). --repeat 이면 <profile>.<회>.jsonl
  <profile>.md      최종 답
  runs.json         실행마다 걸린 시간·종료 코드 (--score 로 다시 채점할 때 쓴다)
  summary.md        비교표 (시간·토큰·도구 호출·탐색·원인 지목 여부) + 정답. 여러 번 돌렸으면 프로필별 중앙값도
채점은 키워드 그룹(chaos.py 시나리오의 keywords)마다 하나 이상 나오면 '맞춤'. 거친 기준이라 최종 판단은 사람이
summary.md 의 답을 읽고 한다.
"""
import argparse
import collections
import datetime as dt
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chaos  # noqa: E402

HERMES = os.path.expanduser("~/.local/bin/hermes")
RUNS = os.path.join(chaos.STATE, "runs")
# 인덱스 목록·매핑을 다시 더듬은 호출 (koa/stats.py 의 '탐색' 과 같은 기준)
EXPLORE = re.compile(r"ListIndexTool|IndexMappingTool|opensearch\s+indices|_cat/indices", re.I)


def run_name(profile, i, repeat):
    return profile if repeat == 1 else "%s.%d" % (profile, i)


def run_profile(profile, name, question, outdir, budget, max_turns, res):
    env = {k: v for k, v in os.environ.items() if k != "HERMES_HOME"}
    cmd = [HERMES, "-p", profile, "chat", "-q", question, "--format", "stream-json",
           "--max-turns", str(max_turns), "--run-budget", str(budget), "--source", "tool"]
    # 에이전트 cwd 는 실행마다 빈 폴더: 앞 회차의 답(<p>.1.jsonl 등)이 눈에 띄지 않게
    cwd = os.path.join(outdir, "cwd", name)
    os.makedirs(cwd)
    t0 = time.time()
    with open(os.path.join(outdir, name + ".jsonl"), "w") as out, \
         open(os.path.join(outdir, name + ".stderr"), "w") as err:
        p = subprocess.run(cmd, stdout=out, stderr=err, env=env, cwd=cwd, timeout=budget + 300)
    res[name] = {"wall": round(time.time() - t0), "rc": p.returncode}


def runs_of(outdir, profile):
    """폴더 안의 이 프로필 실행 이름: <p>.jsonl (한 번) 또는 <p>.<회>.jsonl (--repeat), 회 순서대로."""
    found = []
    for f in os.listdir(outdir):
        if f == profile + ".jsonl":
            found.append((0, profile))
        m = re.match(r"^%s\.(\d+)\.jsonl$" % re.escape(profile), f)
        if m:
            found.append((int(m.group(1)), f[:-len(".jsonl")]))
    return [n for _, n in sorted(found)]


def parse(path):
    """stream-json 에서 최종 답·토큰·도구 호출 목록을 뽑는다. 이벤트 모양이 버전마다 조금 달라 넉넉하게 읽는다."""
    final, tokens, tools, sid = "", {}, collections.Counter(), ""
    calls = []
    for line in open(path):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        t = e.get("type", "")
        sid = sid or e.get("session_id") or ""
        if t == "result":
            final, tokens = e.get("text", ""), e.get("tokens", {})
            if e.get("error"):
                final = final or "(실패) " + e["error"]
        if t == "tool_use":
            name = e.get("name") or "?"
            tools[name] += 1
            args = e.get("input") or ""
            calls.append("%s %s" % (name, (args if isinstance(args, str) else json.dumps(args, ensure_ascii=False))[:160]))
    return final, tokens, tools, calls, sid


def score(text, keywords):
    low = text.lower()
    hits = [next((k for k in grp if k.lower() in low), None) for grp in keywords]
    return all(hits), hits


def _median(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return ("%g" % statistics.median(xs)) if xs else ""


def summarize(outdir, profiles, answer, res=None):
    if res is None and os.path.exists(os.path.join(outdir, "runs.json")):
        res = json.load(open(os.path.join(outdir, "runs.json")))
    rows, bodies, agg = [], [], []
    for p in profiles:
        names, got = runs_of(outdir, p), []
        for name in names:
            final, tok, tools, calls, sid = parse(os.path.join(outdir, name + ".jsonl"))
            with open(os.path.join(outdir, name + ".md"), "w") as f:
                f.write(final)
            ok, hits = score(final, answer["keywords"])
            wall = (res or {}).get(name, {}).get("wall", "")
            explore = sum(1 for c in calls if EXPLORE.search(c))
            got.append({"ok": ok, "wall": wall, "tools": sum(tools.values()), "explore": explore,
                        "total": tok.get("total", ""), "output": tok.get("output", "")})
            rows.append("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
                name, "✅" if ok else "❌", ", ".join(h or "—" for h in hits), wall,
                sum(tools.values()), explore, tok.get("total", ""), tok.get("output", "")))
            bodies.append("## %s\n\n세션: `%s`\n\n도구 호출 %d회: %s\n\n<details><summary>호출 순서</summary>\n\n```\n%s\n```\n</details>\n\n%s\n" % (
                name, sid, sum(tools.values()), ", ".join("%s×%d" % kv for kv in tools.most_common()),
                "\n".join(calls), final))
        if len(got) > 1:
            agg.append("| %s | %d/%d | %s | %s | %s | %s | %s |" % (
                p, sum(1 for g in got if g["ok"]), len(got), *[_median([g[k] for g in got])
                                                               for k in ("wall", "tools", "explore", "total", "output")]))
    md = ["# 장애 분석 비교 — %s" % answer["scenario"], "",
          "- 주입: %s UTC" % answer["injected_at"],
          "- 질문: %s" % answer["symptom"], "",
          "| 실행 | 원인 지목 | 맞은 키워드 | 걸린 시간(s) | 도구 호출 | 탐색 | 토큰 합계 | 출력 토큰 |",
          "|---|---|---|---|---|---|---|---|", *rows, ""]
    if agg:
        md += ["## 프로필별 (중앙값)", "",
               "| 프로필 | 맞힘 | 걸린 시간(s) | 도구 호출 | 탐색 | 토큰 합계 | 출력 토큰 |",
               "|---|---|---|---|---|---|---|", *agg, "",
               "같은 질문을 여러 번 돌려도 서로 완전히 독립은 아니다: KOA 는 분석 중 배운 것을 koa-learned 스킬에 바로 "
               "적으므로 뒤 회차가 앞 회차의 교훈을 읽을 수 있다.", ""]
    md += ["## 정답", "", "- 원인: " + answer["cause"], "- 어디서 보이나: " + answer["where"],
           "- 조치: " + answer["fix"], "", *bodies]
    with open(os.path.join(outdir, "summary.md"), "w") as f:
        f.write("\n".join(md))
    return os.path.join(outdir, "summary.md")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profiles", default="koa,plain")
    ap.add_argument("--budget", type=int, default=900, help="프로필당 제한 시간(초)")
    ap.add_argument("--max-turns", type=int, default=80)
    ap.add_argument("--repeat", type=int, default=1, help="같은 질문을 프로필마다 몇 번 (회차는 차례로, 프로필은 동시에)")
    ap.add_argument("--score", metavar="RUN_DIR")
    a = ap.parse_args()
    profiles = a.profiles.split(",")
    if a.repeat < 1:
        sys.exit("--repeat 은 1 이상")

    if a.score:
        answer = json.load(open(os.path.join(a.score, "answer.json")))
        print(summarize(a.score, profiles, answer))
        return

    answer = chaos.load_answer()
    if not answer:
        sys.exit("걸린 장애가 없다. 먼저 chaos.py inject")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = os.path.join(RUNS, "%s-%s" % (stamp, answer["scenario"]))
    os.makedirs(outdir)
    print("질문: %s\n실행: %s × %d회 (한 번에 최대 %ds) → %s" % (answer["symptom"], ", ".join(profiles), a.repeat,
                                                             a.budget, outdir))
    res = {}
    for i in range(1, a.repeat + 1):
        th = [threading.Thread(target=run_profile, args=(p, run_name(p, i, a.repeat), answer["symptom"], outdir,
                                                         a.budget, a.max_turns, res)) for p in profiles]
        for t in th:
            t.start()
        for t in th:
            t.join()
        with open(os.path.join(outdir, "runs.json"), "w") as f:
            json.dump(res, f, indent=1)
        if a.repeat > 1:
            print("%d/%d 회 끝" % (i, a.repeat))
    # 정답은 실행이 다 끝난 뒤에만 run 폴더에 둔다 (에이전트 cwd 가 이 폴더 아래다).
    with open(os.path.join(outdir, "answer.json"), "w") as f:
        json.dump(answer, f, ensure_ascii=False, indent=2)
    print(summarize(outdir, profiles, answer, res))


if __name__ == "__main__":
    main()
