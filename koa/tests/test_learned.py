import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import learned as L  # noqa: E402


class LearnedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = Path(self.tmp.name)
        L.home = lambda: self.h
        (self.h / "memories").mkdir()
        (self.h / "memories" / "MEMORY.md").write_text("기본 클러스터는 kind-lab\n§\n사용자는 KST 로 말한다")
        sk = self.h / "skills"
        for cat, name in (("devops", "koa-learned"), ("devops", "edited"), ("apple", "apple-notes"), ("devops", "agent-made")):
            (sk / cat / name).mkdir(parents=True)
            (sk / cat / name / "SKILL.md").write_text('---\nname: %s\ndescription: "d %s"\n---\n# body %s\n' % (name, name, name))
        (sk / ".curator_ledger.jsonl").write_text(
            json.dumps({"ts": "2026-10-11T01:00:00+00:00", "actor": "curator", "action": "patch", "skill": "edited"}) + "\nnot json\n")
        (sk / ".usage.json").write_text(json.dumps({"agent-made": {"created_by": "agent", "use_count": 3},
                                                    "apple-notes": {"created_by": None, "use_count": 9}}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_memory_read_and_delete(self):
        m = L.memory()
        self.assertEqual(m["memory"], ["기본 클러스터는 kind-lab", "사용자는 KST 로 말한다"])
        self.assertEqual(m["user"], [])
        left = L.delete_memory("memory", "기본 클러스터는 kind-lab")
        self.assertEqual(left, ["사용자는 KST 로 말한다"])
        self.assertEqual((self.h / "memories" / "MEMORY.md").read_text(), "사용자는 KST 로 말한다")

    def test_delete_refuses_changed_or_bad_target(self):
        with self.assertRaises(LookupError):
            L.delete_memory("memory", "없는 항목")
        with self.assertRaises(ValueError):
            L.delete_memory("../x", "a")

    def test_only_koa_skills(self):
        names = [s["name"] for s in L.skills()]
        self.assertEqual(names, ["koa-learned", "agent-made", "edited"])  # 배운 요령 먼저, 기본 스킬 제외
        e = [s for s in L.skills() if s["name"] == "edited"][0]
        self.assertEqual(e["change_count"], 1)
        self.assertEqual(e["body"].strip(), "# body edited")
        self.assertEqual([s for s in L.skills() if s["name"] == "agent-made"][0]["use_count"], 3)

    def test_ensure_learned_skill(self):
        self.assertIsNone(L.ensure_learned_skill())  # 이미 있음
        import shutil
        shutil.rmtree(self.h / "skills" / "devops" / "koa-learned")
        f = L.ensure_learned_skill()
        self.assertTrue(f.is_file())
        self.assertIn("name: koa-learned", f.read_text())

    def test_summarize_caches_and_goes_stale(self):
        import subprocess as sp
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return sp.CompletedProcess(cmd, 0, "분석 요령 모음.\n\n## 언제 쓰나\n- 분석할 때\nsession_id: x\n", "")
        L.paths.REPO = self.h  # 요약 저장 위치를 임시 폴더로
        old = L.subprocess.run
        L.subprocess.run = fake_run
        try:
            d = L.summarize("edited")
        finally:
            L.subprocess.run = old
        self.assertEqual(d["oneline"], "분석 요령 모음.")
        self.assertNotIn("session_id", d["text"])
        self.assertIn("todo", calls[0])  # 도구는 todo 하나만
        k = [s for s in L.skills() if s["name"] == "edited"][0]
        self.assertFalse(k["ko"]["stale"])
        f = self.h / "skills" / "devops" / "edited" / "SKILL.md"
        f.write_text(f.read_text() + "\nnew lesson\n")
        k = [s for s in L.skills() if s["name"] == "edited"][0]
        self.assertTrue(k["ko"]["stale"])
        with self.assertRaises(LookupError):
            L.summarize("apple-notes")  # KOA 가 만든·고친 스킬이 아니면 거부


if __name__ == "__main__":
    unittest.main()
