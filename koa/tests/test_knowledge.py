#!/usr/bin/env python3
"""knowledge.py 검사 — 꺼내기(context) · 더하기(add) · 제안(suggest). 임시 폴더에서만 (실제 local/knowledge 는 안 건드림).

  python3 koa/tests/test_knowledge.py
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import knowledge as K  # noqa: E402

# gw → shop-api → mq → shop-worker → 결제 대행(외부),  gw → shop-api → pg,  shop-worker → pg (정산)
# alias 'shop' 이 shop-worker 에 부분 문자열로 걸리는 함정도 같이 둔다
DATA = {
    "components": {
        "gw": {"workload": "edge/Deployment/gw", "kind": "ingress"},
        "shop-api": {"workload": "shop/Deployment/api", "kind": "app", "aliases": ["shop", "주문 api"]},
        "mq": {"workload": "shop/StatefulSet/mq", "kind": "message-queue", "aliases": ["래빗"]},
        "shop-worker": {"workload": "shop/Deployment/worker", "kind": "app", "aliases": ["워커"]},
        "pg": {"workload": "data/StatefulSet/pg", "kind": "database"},
        "pay": {"aliases": ["결제사"], "flow_nodes": ["결제 대행"], "external": True},
        "grafana": {"workload": "monitoring/Deployment/grafana"},
    },
    "flows": {
        "주문-만들기": {"kind": "queue", "hops": [
            {"from": "gw", "to": "shop-api", "role": "call"},
            {"from": "shop-api", "to": "mq", "role": "produce"},
            {"from": "mq", "to": "shop-worker", "role": "consume"},
            {"from": "shop-worker", "to": "결제 대행", "role": "call"}]},
        "주문-조회": {"kind": "http", "hops": ["gw", "shop-api", "pg"]},
        "정산": {"kind": "http", "hops": ["shop-worker", "pg"]},
    },
    "normal": [{"component": "shop-worker", "note": "결제사 502 는 재시도 후 dead-letter"},
               {"component": "grafana", "note": "새벽 재시작"}],
}
INCS = [
    {"file": "a.md", "meta": {"components": ["mq"], "signature": {"logs": ["disk alarm"]}, "cause": "디스크"}, "body": ""},
    {"file": "b.md", "meta": {"components": ["grafana"], "cause": "무관"}, "body": ""},
]


def ctx(q, names=None):
    return K.context("c1", q, names=names, data=DATA, incidents=INCS)


class Context(unittest.TestCase):
    def test_only_what_was_asked(self):
        p = ctx("워커가 계속 죽어")
        self.assertEqual(p["asked"], ["shop-worker"])
        self.assertEqual(set(p["flows"]), {"주문-만들기", "정산"})
        self.assertNotIn("grafana", p["components"])        # 상관없는 구성요소는 안 꺼낸다
        self.assertNotIn("b.md", [k["file"] for k in p["incidents"]])
        self.assertEqual([n["component"] for n in p["normal"]], ["shop-worker"])

    def test_longest_name_wins(self):
        # 'shop-worker' 를 말했는데 alias 'shop' 때문에 shop-api 까지 잡히면 안 된다
        self.assertEqual(ctx("shop-worker 로그 봐줘")["asked"], ["shop-worker"])

    def test_flow_node_resolves_to_component(self):
        p = ctx("워커가 죽어")
        self.assertIn("pay", p["components"])
        self.assertNotIn("결제 대행", p["components"])

    def test_explicit_names(self):
        p = ctx("이거 봐줘", names=["주문-조회"])
        self.assertEqual(p["flows"], {"주문-조회": "gw → shop-api → pg"})
        self.assertNotIn("mq", p["components"])

    def test_incident_by_component_or_log(self):
        self.assertIn("a.md", [k["file"] for k in ctx("래빗 이상해")["incidents"]])
        self.assertIn("a.md", [k["file"] for k in ctx("로그에 disk alarm 이 보여")["incidents"]])

    def test_nothing_matched_asks_user(self):
        p = ctx("뭔가 느려")
        self.assertEqual(p["components"], {})
        self.assertTrue(any("사용자에게 묻는다" in n for n in p["notes"]))

    def test_unknown_name_is_noted(self):
        p = ctx("", names=["billing"])
        self.assertTrue(any("billing" in n for n in p["notes"]))


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
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
        return {"components": {"ingress": {"workload": "edge/Deployment/ingress", "kind": "ingress"},
                               "api-server": {"workload": "shop/Deployment/api-server", "kind": "app"},
                               "pg": {"workload": "data/StatefulSet/pg"},
                               "cache": {"workload": "shop/StatefulSet/cache", "kind": "cache"}},
                "flows": [{"name": "a", "nodes": ["api-server", "pg"]},
                          {"name": "b", "nodes": ["ingress", "api-server", "cache"]}]}

    def test_add_keeps_existing(self):
        r = K.apply_additions("c1", self.cand())
        self.assertTrue(r["saved"])
        self.assertEqual([n for n, _ in r["skipped"]], ["a"])            # 같은 workload = 같은 구성요소 → 기존 흐름
        self.assertEqual(r["flows"], [("b", ["ingress", "api", "cache"])])
        d = K.load_flows("c1")
        self.assertEqual(d["components"]["api"]["aliases"], ["에이피아이"])  # 사람이 쓴 것은 그대로
        self.assertEqual(d["normal"], [{"component": "api", "note": "사람이 쓴 메모"}])
        self.assertNotIn("api-server", d["components"])

    def test_add_merges_lists(self):
        r = K.apply_additions("c1", {"components": {"api": {"aliases": ["주문 API"], "log_signatures": ["Too many"],
                                                            "workload": "x/Deployment/other"}}})
        self.assertEqual(r["updated"], {"api": ["aliases", "log_signatures"]})   # workload 는 덮지 않는다
        c = K.load_flows("c1")["components"]["api"]
        self.assertEqual(c["aliases"], ["에이피아이", "주문 API"])
        self.assertEqual(c["workload"], "shop/Deployment/api-server")

    def test_add_is_idempotent(self):
        K.apply_additions("c1", self.cand())
        self.assertFalse(K.apply_additions("c1", self.cand())["changed"])

    def test_dry_run_does_not_write(self):
        before = (K.KNOWLEDGE / "c1" / "flows.yaml").read_text()
        self.assertFalse(K.apply_additions("c1", self.cand(), apply=False)["saved"])
        self.assertEqual((K.KNOWLEDGE / "c1" / "flows.yaml").read_text(), before)

    def test_flowmap_suggestions_wait_for_user(self):
        before = (K.KNOWLEDGE / "c1" / "flows.yaml").read_text()
        ids = K.suggest_from_flowmap("c1", self.cand(), "flowmap t1")
        self.assertEqual(len(ids), 1)                                    # 'a' 는 이미 지식에 있어 제안하지 않는다
        self.assertEqual((K.KNOWLEDGE / "c1" / "flows.yaml").read_text(), before)  # 제안만, 지식은 그대로
        self.assertEqual(len(K.list_suggestions("c1")), 1)
        K.resolve_suggestion("c1", ids[0], accept=True)
        self.assertIn("cache", K.load_flows("c1")["components"])
        self.assertEqual(K.list_suggestions("c1"), [])

    def test_dismissed_is_not_suggested_again(self):
        ids = K.suggest_from_flowmap("c1", self.cand(), "flowmap t1")
        K.resolve_suggestion("c1", ids[0], accept=False)
        self.assertEqual(K.suggest_from_flowmap("c1", self.cand(), "flowmap t2"), [])
        self.assertNotIn("cache", K.load_flows("c1")["components"])

    def test_rerun_replaces_old_flowmap_suggestions(self):
        K.suggest_from_flowmap("c1", self.cand(), "flowmap t1")
        K.suggest_from_flowmap("c1", self.cand(), "flowmap t2")
        self.assertEqual([p["source"] for p in K.list_suggestions("c1")], ["flowmap t2"])

    def test_suggestion_already_in_knowledge_disappears(self):
        K.suggest_from_flowmap("c1", self.cand(), "flowmap t1")
        K.apply_additions("c1", self.cand())                            # 사용자가 대화로 이미 더함
        self.assertEqual(K.list_suggestions("c1"), [])

    def test_incident(self):
        f = K.write_incident("c1", {"date": "2026-10-11", "components": ["db"], "logs": ["too many"], "cause": "커넥션 고갈"})
        m = K.load_incidents("c1")[0]["meta"]
        self.assertEqual(f.name, "2026-10-11-커넥션-고갈.md")
        self.assertEqual(K.incident_logs(m), ["too many"])

    def test_slug_cuts_on_word_boundary(self):
        n = K.slug("NetworkPolicy slowsite-allow-ingress (ingress-nginx 네임스페이스만 허용) 가 막음")
        self.assertEqual(n, "networkpolicy-slowsite-allow-ingress")
        self.assertTrue(len(K.slug("a" * 80)) <= 40)
        self.assertEqual(K.slug(""), "incident")

    def test_recur_appends_once(self):
        f = K.write_incident("c1", {"date": "2026-10-01", "components": ["db"], "logs": ["too many"], "cause": "커넥션 고갈"})
        _, added = K.recur_incident("c1", f.name, {"date": "2026-10-11", "note": "배치 겹침", "logs": ["too many", "pool exhausted"]})
        self.assertTrue(added)
        _, again = K.recur_incident("c1", f.name, {"date": "2026-10-11", "note": "배치 겹침"})
        self.assertFalse(again)
        m = K.load_incidents("c1")[0]["meta"]
        self.assertEqual(m["recurrences"], [{"date": "2026-10-11", "note": "배치 겹침"}])
        self.assertEqual(K.incident_logs(m), ["too many", "pool exhausted"])
        self.assertEqual(len(K.load_incidents("c1")), 1)  # 새 파일을 만들지 않는다
        with self.assertRaises(ValueError):
            K.recur_incident("c1", "../x.md", {})
        with self.assertRaises(LookupError):
            K.recur_incident("c1", "none.md", {})

    def test_branching_flow_is_flagged_and_shown_truthfully(self):
        f = {"hops": [{"from": "a", "to": "b", "role": "call"}, {"from": "b", "to": "q", "role": "produce"},
                      {"from": "b", "to": "cache", "role": "call"}, {"from": "q", "to": "w", "role": "consume"}]}
        self.assertEqual(K.flow_text(f), "a → b → q → w; b → cache")
        self.assertEqual(K.flow_text({"hops": ["a", "b", "c"]}), "a → b → c")
        data = {"components": {n: {"workload": "ns/Deployment/%s" % n} for n in ("a", "b", "q", "w", "cache")},
                "flows": {"f": f}, "normal": []}
        self.assertTrue(any("갈래" in m for _, m in K.check("c1", data)))


if __name__ == "__main__":
    unittest.main(verbosity=1)
