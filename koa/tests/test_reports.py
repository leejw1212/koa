#!/usr/bin/env python3
"""보고서(마크다운) 화면 검사 — 클러스터 없이 임시 폴더와 합성 데이터로만.

  python3 koa/tests/test_reports.py

- triage.render_result: 저장된 JSON → 화면에 찍던 것과 같은 마크다운
- web.list_reports / get_report: 탐색·트리아지·조사 결과 md 를 모아 읽기 (예전 JSON 만 있는 트리아지 포함)
- koa/web/md.js: node 가 있으면 표·이스케이프·링크 규칙 (없으면 건너뜀)
"""
import datetime as dt
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import triage as T  # noqa: E402
import web as W  # noqa: E402

UTC = dt.timezone.utc
AT = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def sample():
    pods = [{"pod": "api-1", "ns": "shop", "container": "app", "multi": False, "restarted": True, "waiting": "CrashLoopBackOff"}]
    f = NS(key="shop/Deployment/api", kind="workload", score=90, status=["CrashLoopBackOff"],
           evidence=["재시작 5회", "exit 1"], onset=AT - dt.timedelta(minutes=20), pods=pods,
           change={"at": AT - dt.timedelta(minutes=25), "target": "Deployment/api", "what": "이미지 a→b"})
    changes = [{"at": AT - dt.timedelta(minutes=25), "ns": "shop", "target": "Deployment/api", "what": "이미지 a→b"}]
    limits = ["이벤트는 1h 만"]
    since, lookback = dt.timedelta(hours=1), dt.timedelta(hours=6)
    md = T.render("c1", AT, since, lookback, "kubernetes MCP", 1.5, [f], changes, limits, 12)
    result = {"schema": "koa.triage/v1", "cluster": "c1", "at": AT.isoformat(), "since_seconds": 3600.0,
              "lookback_seconds": 21600.0, "source": "kubernetes MCP", "seconds": 1.5,
              "findings": [{"key": f.key, "kind": f.kind, "score": 90, "status": f.status, "evidence": f.evidence,
                            "onset": f.onset.isoformat(), "pods": pods,
                            "change": dict(f.change, at=f.change["at"].isoformat())}],
              "changes": [dict(changes[0], at=changes[0]["at"].isoformat())], "limits": limits}
    return md, result


class TriageMarkdown(unittest.TestCase):
    def test_json_renders_same_as_live(self):
        md, result = sample()
        self.assertEqual(T.render_result(result), md)
        self.assertIn("이전 컨테이너 로그", md)  # 다음 확인(파드 정보)까지 같은지

    def test_old_json_without_lookback(self):
        md, result = sample()
        del result["lookback_seconds"]  # 예전 결과
        self.assertEqual(T.render_result(result), md)

    def test_no_findings(self):
        out = T.render_result({"cluster": "c1", "at": AT.isoformat(), "since_seconds": 3600, "findings": [], "changes": [], "limits": []})
        self.assertIn("이상 징후 없음", out)
        self.assertTrue(out.startswith("# 트리아지 c1"))


class Reports(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.old = (W.CLUSTERS, W.REQUESTS)
        W.CLUSTERS, W.REQUESTS = self.tmp / "clusters", self.tmp / "requests"
        W.CLUSTERS.mkdir()
        (W.CLUSTERS / "c1.report.md").write_text("# KOA 탐색 결과 — c1\n\n> 읽기 전용 ✅\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
        (W.CLUSTERS / "c2.report.md").write_text("# 다른 클러스터\n")
        md, result = sample()
        t = W.CLUSTERS / "c1.triage"
        t.mkdir()
        (t / "20260102T030405Z.json").write_text(json.dumps(result))
        (t / "20260102T030405Z.md").write_text(md + "\n")
        (t / "20260101T000000Z.json").write_text(json.dumps(dict(result, at="2026-01-01T00:00:00+00:00")))  # md 없는 예전 결과
        (t / "notes.md").write_text("# 무시")  # 시각 이름이 아닌 파일
        r = W.REQUESTS / "c1" / "20260103T000000Z"
        r.mkdir(parents=True)
        (r / "request.yaml").write_text("request: api 가 죽어\ncluster: c1\n")
        (r / "report.md").write_text("# 조사 결과\n\n원인: 이미지\n")
        (W.REQUESTS / "c1" / "20260104T000000Z").mkdir()  # report.md 없는 요청 → 목록에 없음

    def tearDown(self):
        W.CLUSTERS, W.REQUESTS = self.old
        shutil.rmtree(self.tmp)

    def test_list(self):
        rs = W.list_reports("c1")
        kinds = [(r["kind"], r["id"]) for r in rs]
        self.assertIn(("discover", "c1"), kinds)
        self.assertIn(("triage", "20260102T030405Z"), kinds)
        self.assertIn(("triage", "20260101T000000Z"), kinds)
        self.assertIn(("analysis", "20260103T000000Z"), kinds)
        self.assertEqual(len(rs), 4)  # c2 보고서·notes.md·report 없는 요청은 빠진다
        tri = next(r for r in rs if r["id"] == "20260102T030405Z")
        self.assertEqual(tri["findings"], 1)
        self.assertEqual(tri["title"], "트리아지 · 최근 1.0h")
        ana = next(r for r in rs if r["kind"] == "analysis")
        self.assertEqual(ana["summary"], "api 가 죽어")
        dates = [r["at"] for r in rs if r["kind"] != "discover"]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_get(self):
        self.assertIn("| 1 | 2 |", W.get_report("c1", "discover", "c1")["md"])
        old = W.get_report("c1", "triage", "20260101T000000Z")
        self.assertTrue(old["md"].startswith("# 트리아지 c1 · 2026-01-01 00:00Z"))
        self.assertFalse((W.CLUSTERS / "c1.triage" / "20260101T000000Z.md").exists(), "보기만 하고 파일은 쓰지 않는다")
        self.assertIn("원인: 이미지", W.get_report("c1", "analysis", "20260103T000000Z")["md"])

    def test_bad_input(self):
        with self.assertRaises(ValueError):
            W.get_report("c1", "triage", "../../etc/passwd")
        with self.assertRaises(ValueError):
            W.get_report("../x", "discover", "c1")
        with self.assertRaises(ValueError):
            W.get_report("c1", "other", "20260101T000000Z")
        with self.assertRaises(LookupError):
            W.get_report("c1", "discover", "c2")  # 다른 클러스터 보고서는 이 이름으로 안 열린다
        with self.assertRaises(LookupError):
            W.get_report("c1", "analysis", "20260104T000000Z")


@unittest.skipUnless(shutil.which("node"), "node 없음")
class MarkdownJs(unittest.TestCase):
    def js(self, md):
        code = "const M=require(%s);process.stdout.write(M.render(%s))" % (json.dumps(str(HERE.parent / "web" / "md.js")), json.dumps(md))
        return subprocess.run(["node", "-e", code], stdout=subprocess.PIPE, check=True).stdout.decode()

    def test_table_and_pipe_escape(self):
        h = self.js("| 대상 | 근거 |\n|---|---|\n| a | x \\| y `p|q` |\n")
        self.assertIn("<table>", h)
        self.assertIn("<td>x | y <code>p|q</code></td>", h)

    def test_escape_and_links(self):
        h = self.js("<img src=x onerror=alert(1)> [a](javascript:alert(1)) [b](https://ok.example)")
        self.assertNotIn("<img", h)
        self.assertNotIn('href="javascript', h)
        self.assertIn('href="https://ok.example"', h)

    def test_blocks(self):
        h = self.js("# 제목\n> 인용\n\n1. 하나\n2. 둘\n\n- a\n  - b\n\n```\n<x>\n```\n")
        for tag in ("<h1", "<blockquote>", "<ol>", "<ul>", "<pre><code>&lt;x&gt;"):
            self.assertIn(tag, h)

    def test_real_triage_markdown(self):
        md, _ = sample()
        h = self.js(md)
        self.assertEqual(h.count("<table>"), 1)
        self.assertIn("<h2", h)


if __name__ == "__main__":
    unittest.main()
