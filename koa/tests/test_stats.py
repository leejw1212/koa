#!/usr/bin/env python3
"""stats.py 검사 — 임시 state.db 를 만들어 대화 묶기 · 질문별 시간 · 도구/탐색 세기 · 읽기 전용을 본다.

  python3 koa/tests/test_stats.py
"""
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import stats as S  # noqa: E402

# Hermes state.db 의 일부 칸만 (버전마다 칸이 달라도 읽혀야 한다: api_call_count·reasoning_tokens 등은 일부러 뺐다)
SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, model TEXT, title TEXT, parent_session_id TEXT,
  started_at REAL, ended_at REAL, end_reason TEXT, session_key TEXT, model_config TEXT,
  input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, estimated_cost_usd REAL);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,
  tool_calls TEXT, tool_name TEXT, timestamp REAL, _compressed_summary INTEGER DEFAULT 0);
"""
NOW = time.time()
T0 = NOW - 2 * 86400


def call(name, **args):
    return {"id": "c", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def build(path):
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)

    def sess(sid, parent=None, start=T0, end=None, why=None, key=None, cfg=None, tok=(0, 0, 0), cost=0.0):
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, "cli", "m", None, parent, start, end, why, key, json.dumps(cfg or {}), tok[0], tok[1], tok[2], cost))

    def msg(sid, role, ts, content=None, calls=None, summary=0):
        con.execute("INSERT INTO messages (session_id, role, content, tool_calls, timestamp, _compressed_summary) "
                    "VALUES (?,?,?,?,?,?)", (sid, role, content, json.dumps(calls) if calls else None, ts, summary))

    # 분석 대화 a: a1 → (압축) a2, a2 에서 하위 에이전트 a3
    sess("a1", end=T0 + 60, why="compression", tok=(100, 10, 1000), cost=0.5)
    msg("a1", "user", T0, "주문 api 가 500 나요")
    msg("a1", "assistant", T0 + 5, calls=[call("terminal", command='python3 koa/knowledge.py context lab "주문 500"')])
    msg("a1", "tool", T0 + 6, "질문: ...")
    msg("a1", "assistant", T0 + 20, calls=[call("mcp_opensearch_ListIndexTool"),
                                           call("mcp_opensearch_IndexMappingTool", index="logs-*")])
    msg("a1", "tool", T0 + 21, "...")
    msg("a1", "tool", T0 + 22, "...")
    msg("a1", "assistant", T0 + 60, "결론: ...")
    sess("a2", parent="a1", start=T0 + 61, tok=(50, 5, 500), cost=0.25)
    msg("a2", "user", T0 + 61, "[이전 대화 요약] ...", summary=1)
    msg("a2", "user", T0 + 3600, "그럼 product 는요?")                       # 사용자가 한 시간 뒤에 다시 물었다
    msg("a2", "assistant", T0 + 3610, calls=[call("mcp_opensearch_SearchIndexTool", index="logs-*",
                                                  query_dsl={"size": 0}, password="hunter2")])
    msg("a2", "tool", T0 + 3611, "...")
    msg("a2", "assistant", T0 + 3650, "product 는 정상")
    sess("a3", parent="a2", start=T0 + 3620, cfg={"_delegate_from": "a2"}, tok=(10, 1, 100), cost=0.05)
    msg("a3", "user", T0 + 3620, "하위 작업: 파드 상태")
    msg("a3", "assistant", T0 + 3625, calls=[call("terminal", command="kubectl get pods -n shop")])
    msg("a3", "tool", T0 + 3626, "...")
    msg("a3", "assistant", T0 + 3630, "다 Running")
    # 분석 아닌 대화 b
    sess("b1", start=T0 + 100)
    msg("b1", "user", T0 + 100, "안녕")
    msg("b1", "assistant", T0 + 101, "안녕하세요")
    # 리셋: c1 이 끝나고 같은 열쇠로 c2 가 새로 시작 → 따로 센다. 분석은 c2 에만
    sess("c1", start=T0 + 200, end=T0 + 210, why="session_reset", key="k")
    msg("c1", "user", T0 + 200, "아까 것")
    msg("c1", "assistant", T0 + 201, "네")
    sess("c2", parent="c1", start=T0 + 300, key="k")
    msg("c2", "user", T0 + 300, "rmq 소비가 안 돼")
    msg("c2", "assistant", T0 + 310, calls=[call("terminal", command="python3 koa/knowledge.py context lab rmq")])
    msg("c2", "assistant", T0 + 330, "원인: ...")
    # 오래된 분석 d (30일 전)
    sess("d1", start=NOW - 30 * 86400)
    msg("d1", "user", NOW - 30 * 86400, "옛날 질문")
    msg("d1", "assistant", NOW - 30 * 86400 + 9, calls=[call("terminal", command="python3 koa/knowledge.py context lab x")])
    con.commit()
    con.close()


class Stats(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state.db"
        build(self.db)
        self.raw = self.db.read_bytes()
        self.con = S.connect(self.db)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def rows(self, **kw):
        return {r["id"]: r for r in S.collect(self.con, since=kw.get("since", NOW - 14 * 86400),
                                              until=kw.get("until"), include_all=kw.get("all", False))}

    def test_only_analysis_conversations_in_window(self):
        self.assertEqual(set(self.rows()), {"a1", "c2"})                 # b1 은 분석 아님, d1 은 기간 밖
        self.assertEqual(set(self.rows(all=True)), {"a1", "b1", "c1", "c2"})
        self.assertEqual(set(self.rows(since=NOW - 40 * 86400)), {"a1", "c2", "d1"})
        self.assertEqual(set(self.rows(since=NOW - 40 * 86400, until=NOW - 7 * 86400)), {"d1"})

    def test_compression_and_subagent_fold_into_one_conversation(self):
        r = self.rows()["a1"]
        self.assertEqual(r["questions"], 2)                              # 압축 요약·하위 에이전트 지시는 질문이 아니다
        self.assertEqual(r["work_s"], 60 + 50)                           # 질문 사이 기다린 한 시간은 빼고
        self.assertEqual(r["tools"], 5)
        self.assertEqual(r["explore"], 2)
        self.assertEqual(r["responses"], 7)
        self.assertEqual(r["subagents"], 1)
        self.assertEqual(r["tokens"]["input_tokens"], 160)
        self.assertEqual(r["tokens"]["reasoning_tokens"], 0)             # 없는 칸은 0
        self.assertAlmostEqual(r["cost_usd"], 0.8)
        self.assertEqual(r["question"], "주문 api 가 500 나요")

    def test_detail_from_any_session_id(self):
        r = S.detail(self.con, "a3")
        self.assertEqual(r["id"], "a1")
        segs = r["segments"]
        self.assertEqual([s["analysis"] for s in segs], [True, False])
        self.assertEqual([sub for _, _, sub in segs[1]["calls"]], [False, True])
        text = S.detail_text(r)
        self.assertIn("python3 koa/knowledge.py context lab", text)
        self.assertIn("↳ terminal  kubectl get pods", text)
        self.assertNotIn("hunter2", text)                                # 비밀처럼 보이는 값은 가린다

    def test_table_has_median_row(self):
        text = S.table(S.collect(self.con, since=NOW - 14 * 86400))
        self.assertIn("중앙값", text)
        self.assertIn("mcp_opensearch_ListIndexTool", text)

    def test_reads_only(self):
        S.collect(self.con, since=0, include_all=True)
        with self.assertRaises(sqlite3.OperationalError):
            self.con.execute("DELETE FROM sessions")
        self.assertEqual(self.db.read_bytes(), self.raw)

    def test_missing_db_is_a_clear_error(self):
        with self.assertRaises(SystemExit) as e:
            S.connect(Path(self.tmp.name) / "nope.db")
        self.assertIn("세션 기록이 없다", str(e.exception))


if __name__ == "__main__":
    unittest.main()
