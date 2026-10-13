#!/usr/bin/env python3
"""KOA 분석 재기 — Hermes 세션 기록(state.db)을 읽기 전용으로 열어, 분석 한 건에 든 시간·모델 응답·도구 호출·토큰을 본다.

지식 정리나 확인 순서를 바꾼 뒤 분석이 정말 빨라지고 가벼워졌는지 같은 잣대로 견주려고 쓴다.
state.db 는 읽기만 한다 (Hermes 가 쓰는 중이어도 된다). 클러스터는 건드리지 않는다.

  python3 koa/stats.py                         # 최근 14일 분석 대화 (knowledge.py context 를 부른 대화)
  python3 koa/stats.py --since 2026-10-14      # 이날부터. 개선 전후는 --until 2026-10-14 와 견준다
  python3 koa/stats.py --all                   # 분석이 아닌 대화도
  python3 koa/stats.py --session <id>          # 대화 하나: 질문마다 걸린 시간과 도구 호출 순서
  python3 koa/stats.py --profile koa           # ~/.hermes/profiles/koa/state.db (기본: 이 설치본의 state.db)
  python3 koa/stats.py --db <경로> --json

한 줄 = 대화 하나. 압축으로 이어진 세션과 하위 에이전트 실행은 처음 세션에 합친다.
  작업   질문마다 '질문 → 그 질문의 마지막 기록' 을 더한 시간 (사용자를 기다린 시간은 뺀다)
  질문   사용자 메시지 수     응답   모델 응답 수     도구   도구 호출 수
  탐색   인덱스 목록·매핑 보기 호출 수 (OpenSearch 를 처음부터 더듬은 횟수)
  입력·캐시·출력   토큰 (Hermes 가 센 그대로, 캐시 = 캐시에서 읽은 입력)     비용   Hermes 추정 비용(USD)
"""
import argparse
import datetime as dt
import json
import re
import sqlite3
import statistics
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import HERMES_HOME  # noqa: E402

ANALYSIS = re.compile(r"knowledge\.py['\"]?\s+context\b")      # 분석 절차의 '지식 꺼내기' (cluster-incident-analysis)
EXPLORE = re.compile(r"ListIndexTool|IndexMappingTool|opensearch\s+indices|_cat/indices", re.I)
SECRET = re.compile(r"((?:password|passwd|token|secret|authorization|api[_-]?key)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+", re.I)
# 부모가 이렇게 끝났고 같은 대화 열쇠면 리셋으로 새로 시작한 대화다 (Hermes 의 리셋 규칙)
RESET = ("session_reset", "session_switch", "idle", "daily", "suspended", "resume_pending_expired", "new_session")
SESSION_COLS = ("id", "source", "model", "title", "parent_session_id", "started_at", "ended_at", "end_reason",
                "session_key", "model_config", "last_activity_at", "input_tokens", "cache_read_tokens",
                "cache_write_tokens", "output_tokens", "reasoning_tokens", "estimated_cost_usd", "actual_cost_usd")
TOKENS = ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens")


# ---------- 읽기 ----------

def default_db(profile=None):
    if profile:
        return Path.home() / ".hermes" / "profiles" / profile / "state.db"
    return HERMES_HOME / "state.db"


def connect(path):
    """읽기 전용(mode=ro)으로 연다. Hermes 가 쓰는 중이면 잠깐(5초)만 기다린다."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise SystemExit("세션 기록이 없다: %s (--db 나 --profile 로 지정한다)" % p)
    try:
        con = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("SELECT 1 FROM sessions LIMIT 1").fetchall()
    except sqlite3.Error as x:
        raise SystemExit("세션 기록을 읽기 전용으로 열지 못했다: %s (%s)" % (p, x))
    return con


def _columns(con, table):
    return {r[1] for r in con.execute("PRAGMA table_info(%s)" % table)}


def load_sessions(con):
    """{id: 세션}. Hermes 버전마다 칸이 달라 있는 칸만 읽는다."""
    have = _columns(con, "sessions")
    cols = [c for c in SESSION_COLS if c in have]
    out = {}
    for r in con.execute("SELECT %s FROM sessions" % ", ".join(cols)):
        s = {c: r[c] for c in cols}
        try:
            cfg = json.loads(s.get("model_config") or "{}")
        except (TypeError, ValueError):
            cfg = {}
        s["cfg"] = cfg if isinstance(cfg, dict) else {}
        out[s["id"]] = s
    return out


def separate(s, parent):
    """자식 세션이 따로 시작한 대화인가 (갈래·리셋). 압축으로 이어진 세션과 하위 에이전트 실행은 부모 대화에 합친다."""
    cfg = s["cfg"]
    if cfg.get("_delegate_from"):
        return False
    if cfg.get("_branched_from") or cfg.get("_reset_from"):
        return True
    why = parent.get("end_reason") or ""
    if why == "branched":
        return (s.get("started_at") or 0) >= (parent.get("ended_at") or 0)
    if why in RESET:
        return bool(s.get("session_key")) and s.get("session_key") == parent.get("session_key")
    return False


def conversations(sessions):
    """세션 → 대화 {처음 세션 id: {세션 id: 'main' | 'sub'}}.
    main = 처음 세션과 압축으로 이어진 세션 (사용자 질문이 여기 있다), sub = 하위 에이전트 실행 (시간·도구·토큰만 더한다)."""
    memo = {}

    def place(sid, depth=0):
        if sid in memo:
            return memo[sid]
        s = sessions[sid]
        pid = s.get("parent_session_id")
        parent = sessions.get(pid) if pid and pid != sid else None
        if parent is None or depth > 500 or separate(s, parent):
            memo[sid] = (sid, "main")
        else:
            root, role = place(pid, depth + 1)
            cont = parent.get("end_reason") == "compression" and not s["cfg"].get("_delegate_from")
            memo[sid] = (root, role if cont else "sub")
        return memo[sid]

    out = {}
    for sid in sessions:
        root, role = place(sid)
        out.setdefault(root, {})[sid] = role
    return out


def load_messages(con, ids):
    """세션들의 메시지 (시각 순). 내용은 질문(user)만 앞부분을 읽는다 — 도구 결과는 크다."""
    have = _columns(con, "messages")
    pick = ["id", "session_id", "role", "timestamp",
            "CASE WHEN role = 'user' THEN substr(content, 1, 400) END AS content",
            "tool_calls" if "tool_calls" in have else "NULL AS tool_calls",
            "_compressed_summary AS summary" if "_compressed_summary" in have else "0 AS summary"]
    ids, out = list(ids), []
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        q = "SELECT %s FROM messages WHERE session_id IN (%s)" % (", ".join(pick), ", ".join("?" * len(part)))
        out += [dict(r) for r in con.execute(q, part)]
    out.sort(key=lambda m: (m["timestamp"] or 0, m["id"]))
    return out


def tool_calls(m):
    """assistant 메시지의 도구 호출 [(이름, 인자 글)]. OpenAI 모양({function: {name, arguments}})과 납작한 모양 둘 다."""
    raw = m.get("tool_calls")
    if not raw:
        return []
    try:
        calls = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return [("?", str(raw))]
    out = []
    for c in calls if isinstance(calls, list) else [calls]:
        if not isinstance(c, dict):
            continue
        f = c.get("function") if isinstance(c.get("function"), dict) else c
        args = f.get("arguments", f.get("input", ""))
        out.append((str(f.get("name") or "?"), args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)))
    return out


# ---------- 재기 ----------

def segments(msgs, roles):
    """대화를 질문(사용자 메시지)마다 자른다. 하위 에이전트에게 준 지시와 압축 요약은 질문이 아니다.
    반환: [{start, end, question, responses, calls: [(이름, 인자, 하위?)], analysis}]"""
    segs, cur = [], None
    for m in msgs:
        main = roles.get(m["session_id"]) == "main"
        if m.get("summary"):
            continue
        ts = m["timestamp"] or 0
        if m["role"] == "user" and main:
            cur = {"start": ts, "end": ts, "question": one_line(m.get("content")), "responses": 0, "calls": []}
            segs.append(cur)
            continue
        if cur is None:                      # 질문 없이 시작한 기록 (예약 실행 등)
            cur = {"start": ts, "end": ts, "question": "", "responses": 0, "calls": []}
            segs.append(cur)
        cur["end"] = max(cur["end"], ts)
        if m["role"] == "assistant":
            cur["responses"] += 1
            cur["calls"] += [(n, a, not main) for n, a in tool_calls(m)]
    for s in segs:
        s["analysis"] = any(ANALYSIS.search(a) for _, a, _ in s["calls"])
        s["explore"] = sum(1 for n, a, _ in s["calls"] if EXPLORE.search(n + " " + a))
    return segs


def summarize(root, roles, sessions, msgs):
    """대화 하나의 한 줄 값."""
    segs = segments(msgs, roles)
    members = [sessions[i] for i in roles]
    tok = {k: sum(int(s.get(k) or 0) for s in members) for k in TOKENS}
    cost = sum(float(s.get("actual_cost_usd") or s.get("estimated_cost_usd") or 0) for s in members)
    calls = [c for s in segs for c in s["calls"]]
    first = sessions[root]
    times = [m["timestamp"] for m in msgs if m["timestamp"]]
    return {
        "id": root, "source": first.get("source") or "", "model": first.get("model") or "",
        "started": min(times + [first.get("started_at") or time.time()]),
        "last": max(times + [first.get("started_at") or 0]),
        "work_s": round(sum(s["end"] - s["start"] for s in segs)),
        "questions": sum(1 for s in segs if s["question"]), "analyses": sum(1 for s in segs if s["analysis"]),
        "responses": sum(s["responses"] for s in segs), "tools": len(calls), "explore": sum(s["explore"] for s in segs),
        "subagents": sum(1 for r in roles.values() if r == "sub"),
        "tokens": tok, "cost_usd": round(cost, 4),
        "top_tools": Counter(n for n, _, _ in calls).most_common(4),
        "question": next((s["question"] for s in segs if s["question"]), first.get("title") or ""),
        "segments": segs,
    }


def collect(con, since=None, until=None, include_all=False):
    """기간 안의 대화 요약 목록 (최근 것 먼저). include_all=False 면 knowledge.py context 를 부른 대화만."""
    sessions = load_sessions(con)
    convs = conversations(sessions)
    rows = []
    for root, roles in convs.items():
        stamps = [x for i in roles for x in (sessions[i].get("started_at"), sessions[i].get("ended_at"),
                                             sessions[i].get("last_activity_at")) if x]
        # 세션 시각으로 먼저 거른다 (하루 여유: 시각 칸이 늦게 찍히거나 비어 있을 수 있다). 정확한 판정은 메시지 시각으로
        if since and stamps and max(stamps) < since - 86400:
            continue
        if until and stamps and min(stamps) >= until + 86400:
            continue
        msgs = load_messages(con, roles)
        if not msgs:
            continue
        r = summarize(root, roles, sessions, msgs)
        if (since and r["last"] < since) or (until and r["started"] >= until):
            continue
        if include_all or r["analyses"]:
            rows.append(r)
    rows.sort(key=lambda r: -r["started"])
    return rows


def detail(con, sid):
    """대화 하나 (그 대화의 어느 세션 id 를 줘도 된다)."""
    sessions = load_sessions(con)
    hit = [i for i in sessions if i == sid] or [i for i in sessions if i.startswith(sid)]
    if len(hit) != 1:
        raise SystemExit("세션 %s 를 %s" % (sid, "못 찾았다" if not hit else "하나로 못 가렸다 (%d개)" % len(hit)))
    for root, roles in conversations(sessions).items():
        if hit[0] in roles:
            return summarize(root, roles, sessions, load_messages(con, roles))
    raise SystemExit("세션 %s 를 못 찾았다" % sid)


# ---------- 보여 주기 ----------

def one_line(text, limit=90):
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t if len(t) <= limit else t[:limit - 1] + "…"


def dur(sec):
    sec = int(sec or 0)
    if sec < 60:
        return "%ds" % sec
    if sec < 3600:
        return "%dm%02ds" % (sec // 60, sec % 60)
    return "%dh%02dm" % (sec // 3600, sec % 3600 // 60)


def num(n):
    n = int(n or 0)
    if n < 1000:
        return str(n)
    if n < 10000:
        return "%.1fk" % (n / 1000.0)
    if n < 1000000:
        return "%dk" % round(n / 1000.0)
    return "%.1fM" % (n / 1000000.0)


def when(ts, fmt="%m-%d %H:%M"):
    return dt.datetime.fromtimestamp(ts).strftime(fmt)


def call_text(name, args, limit=140):
    """도구 호출 한 줄. 터미널이면 명령만, 비밀처럼 보이는 값은 가린다."""
    try:
        a = json.loads(args) if args else {}
    except ValueError:
        a = args
    if isinstance(a, dict) and isinstance(a.get("command"), str):
        a = a["command"]
    elif not isinstance(a, str):
        a = json.dumps(a, ensure_ascii=False)
    return "%s  %s" % (name, one_line(SECRET.sub(r"\1***", a), limit))


WIDTHS = (11, 7, 4, 4, 4, 4, 6, 6, 6, 7)       # 시작 · 작업 · 질문 · 응답 · 도구 · 탐색 · 입력 · 캐시 · 출력 · 비용, 끝에 첫 질문


def _width(text):
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def row(*cells):
    """한글이 두 칸을 차지해도 줄이 맞게. 첫 칸은 왼쪽, 나머지 숫자 칸은 오른쪽 정렬, 마지막 칸(첫 질문)은 그대로."""
    out = []
    for i, (c, w) in enumerate(zip(cells, WIDTHS)):
        c = str(c)
        gap = " " * max(0, w - _width(c))
        out.append(c + gap if i == 0 else gap + c)
    return (" ".join(out) + "  " + " ".join(str(c) for c in cells[len(WIDTHS):])).rstrip()


def table(rows):
    L = [row("시작", "작업", "질문", "응답", "도구", "탐색", "입력", "캐시", "출력", "비용", "첫 질문")]
    for r in rows:
        t = r["tokens"]
        L.append(row(when(r["started"]), dur(r["work_s"]), r["questions"], r["responses"], r["tools"], r["explore"],
                     num(t["input_tokens"]), num(t["cache_read_tokens"]), num(t["output_tokens"]),
                     "$%.2f" % r["cost_usd"], one_line(r["question"], 60)))
    if rows:
        med = lambda k: statistics.median([r[k] for r in rows])  # noqa: E731
        mtok = lambda k: statistics.median([r["tokens"][k] for r in rows])  # noqa: E731
        L.append(row("중앙값", dur(med("work_s")), "%g" % med("questions"), "%g" % med("responses"),
                     "%g" % med("tools"), "%g" % med("explore"), num(mtok("input_tokens")),
                     num(mtok("cache_read_tokens")), num(mtok("output_tokens")), "$%.2f" % med("cost_usd")))
        tools = Counter()
        for r in rows:
            for n, c in r["top_tools"]:
                tools[n] += c
        L.append("비용 합계 $%.2f · 많이 쓴 도구: %s" % (sum(r["cost_usd"] for r in rows),
                                                ", ".join("%s %d" % kv for kv in tools.most_common(5)) or "-"))
    return "\n".join(L)


def detail_text(r):
    t = r["tokens"]
    L = ["대화 %s (%s, %s) %s 시작" % (r["id"], r["source"] or "?", r["model"] or "?", when(r["started"], "%Y-%m-%d %H:%M")),
         "작업 %s · 질문 %d · 응답 %d · 도구 %d (탐색 %d) · 하위 에이전트 %d · 입력 %s · 캐시 %s · 출력 %s · $%.2f" % (
             dur(r["work_s"]), r["questions"], r["responses"], r["tools"], r["explore"], r["subagents"],
             num(t["input_tokens"]), num(t["cache_read_tokens"]), num(t["output_tokens"]), r["cost_usd"])]
    for i, s in enumerate(r["segments"], 1):
        L.append("")
        L.append("[%d] %s  %s  응답 %d · 도구 %d · 탐색 %d%s" % (
            i, when(s["start"], "%m-%d %H:%M:%S"), dur(s["end"] - s["start"]), s["responses"], len(s["calls"]),
            s["explore"], " · 분석" if s["analysis"] else ""))
        L.append("    질문: %s" % (s["question"] or "(없음)"))
        for j, (n, a, sub) in enumerate(s["calls"], 1):
            L.append("    %3d %s%s" % (j, "↳ " if sub else "", call_text(n, a)))
    return "\n".join(L)


def _plain(r, segs=False):
    """--json 용: 시각은 읽을 수 있는 글로, 질문별 기록은 --session 일 때만."""
    out = {k: v for k, v in r.items() if k != "segments"}
    out["top_tools"] = dict(r["top_tools"])
    out["started"], out["last"] = when(r["started"], "%Y-%m-%dT%H:%M:%S"), when(r["last"], "%Y-%m-%dT%H:%M:%S")
    if segs:
        out["segments"] = [dict(s, start=when(s["start"], "%Y-%m-%dT%H:%M:%S"), seconds=round(s["end"] - s["start"]),
                                end=None, calls=[{"tool": n, "args": SECRET.sub(r"\1***", a), "subagent": b}
                                                 for n, a, b in s["calls"]]) for s in r["segments"]]
        for s in out["segments"]:
            s.pop("end")
    return out


def _day(s):
    try:
        return time.mktime(dt.datetime.strptime(s, "%Y-%m-%d").timetuple())
    except ValueError:
        raise SystemExit("날짜는 YYYY-MM-DD: %r" % s)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="state.db 경로")
    ap.add_argument("--profile", help="Hermes 프로필 이름 (~/.hermes/profiles/<이름>/state.db)")
    ap.add_argument("--days", type=int, default=14, help="최근 며칠 (기본 14)")
    ap.add_argument("--since", help="YYYY-MM-DD 부터 (--days 대신)")
    ap.add_argument("--until", help="YYYY-MM-DD 전까지")
    ap.add_argument("--all", action="store_true", help="분석이 아닌 대화도")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--session", help="대화 하나를 자세히 (세션 id 앞부분만 줘도 된다)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    db = Path(a.db).expanduser() if a.db else default_db(a.profile)
    con = connect(db)
    if a.session:
        r = detail(con, a.session)
        print(json.dumps(_plain(r, True), ensure_ascii=False, indent=1) if a.json else detail_text(r))
        return
    since = _day(a.since) if a.since else time.time() - a.days * 86400
    until = _day(a.until) if a.until else None
    rows = collect(con, since, until, a.all)
    shown = rows[:a.limit]
    if a.json:
        print(json.dumps([_plain(r) for r in shown], ensure_ascii=False, indent=1))
        return
    print("%s %d개 · %s ~ %s · %s%s" % ("대화" if a.all else "분석 대화", len(rows), when(since, "%Y-%m-%d"),
                                     when(until, "%Y-%m-%d") if until else "지금", db,
                                     " (최근 %d개만 보임)" % a.limit if len(rows) > a.limit else ""))
    if not rows:
        print("(없음) — 분석 대화는 knowledge.py context 를 부른 대화다. --all 이면 다 본다")
        return
    print(table(shown))


if __name__ == "__main__":
    main()
