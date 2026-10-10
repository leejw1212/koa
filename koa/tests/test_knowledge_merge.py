#!/usr/bin/env python3
"""knowledge.merge_candidates 검사 — 임시 폴더에서만 (실제 local/knowledge 는 건드리지 않는다).

  python3 koa/tests/test_knowledge_merge.py
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import knowledge as K  # noqa: E402


class Merge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()  # TMPDIR 아래
        self.orig = K.KNOWLEDGE
        K.KNOWLEDGE = Path(self.tmp.name)
        K.save_shards("c1", {
            "components": {"api": {"workload": "shop/Deployment/api-server", "kind": "app", "aliases": ["에이피아이"]},
                           "db": {"workload": "data/StatefulSet/pg", "kind": "database"}},
            "flows": [{"name": "주문", "nodes": ["api", "db"]}],
            "normal": [{"component": "api", "note": "사람이 쓴 메모"}]})

    def tearDown(self):
        K.KNOWLEDGE = self.orig
        self.tmp.cleanup()

    def cand(self):
        return {"components": {"ingress-nginx-controller": {"workload": "edge/Deployment/ingress-nginx-controller", "kind": "ingress"},
                               "api-server": {"workload": "shop/Deployment/api-server", "kind": "app"},
                               "pg": {"workload": "data/StatefulSet/pg", "kind": "database"},
                               "cache": {"workload": "shop/StatefulSet/cache", "kind": "cache"}},
                "flows": [{"name": "a", "kind": "http", "nodes": ["api-server", "pg"]},
                          {"name": "b", "kind": "http", "nodes": ["ingress-nginx-controller", "api-server", "cache"]}]}

    def test_preview_does_not_write(self):
        before = (K.KNOWLEDGE / "c1" / "flows.yaml").read_text()
        r = K.merge_candidates("c1", self.cand())
        self.assertFalse(r["saved"])
        self.assertEqual((K.KNOWLEDGE / "c1" / "flows.yaml").read_text(), before)

    def test_apply_keeps_existing_and_renames(self):
        r = K.merge_candidates("c1", self.cand(), apply=True)
        self.assertTrue(r["saved"])
        self.assertEqual([n for n, _ in r["skipped"]], ["a"])  # api→db 와 같다 (같은 workload = 같은 구성요소)
        self.assertEqual(r["flows"], [("b", ["ingress-nginx-controller", "api", "cache"])])
        d = K.load_flows("c1")
        self.assertEqual(d["components"]["api"]["aliases"], ["에이피아이"])  # 사람이 쓴 것은 그대로
        self.assertEqual(d["normal"], [{"component": "api", "note": "사람이 쓴 메모"}])
        self.assertIn("주문", d["flows"])
        self.assertIn("cache", d["components"])
        self.assertNotIn("api-server", d["components"])  # 기존 이름으로 바꿔 썼다

    def test_pick(self):
        r = K.merge_candidates("c1", self.cand(), pick=[1])
        self.assertEqual(r["flows"], [])
        self.assertEqual(r["components"], [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
