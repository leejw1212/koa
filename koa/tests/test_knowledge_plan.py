#!/usr/bin/env python3
"""knowledge.plan 의 확인 경로 검사 — 합성 지식만 쓴다 (클러스터·실제 local/knowledge 불필요).

  python3 koa/tests/test_knowledge_plan.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import knowledge as K  # noqa: E402

# 큐를 거치는 흐름: gw → shop-api → mq → shop-worker → 외부 결제사
# alias 'shop' 이 shop-worker 노드에 부분 문자열로 걸리는 함정도 같이 둔다
DATA = {
    "components": {
        "gw": {"workload": "edge/Deployment/gw", "kind": "ingress"},
        "shop-api": {"workload": "shop/Deployment/api", "kind": "app", "aliases": ["shop", "주문"]},
        "mq": {"workload": "shop/StatefulSet/mq", "kind": "message-queue"},
        "shop-worker": {"workload": "shop/Deployment/worker", "kind": "app", "aliases": ["워커"]},
        "pg": {"workload": "data/StatefulSet/pg", "kind": "database"},
        "pay": {"aliases": ["결제사"], "flow_nodes": ["결제 대행"], "external": True,
                "log_signatures": ["HTTP 502"]},
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
    "normal": [
        {"component": "shop-worker", "note": "결제사 502 는 재시도 후 dead-letter"},
        {"component": "pay", "note": "외부 결제사 점검 시간엔 502"},
    ],
}


def path_of(q):
    return [p["component"] for p in K.plan("c1", q, data=DATA, incidents=[])["path"]]


class PlanPath(unittest.TestCase):
    def test_request_symptom_reaches_flow_end(self):
        # "주문 생성이 안 돼" (pending): 대상 파드만이 아니라 큐 너머 소비자·외부 호출까지 본다
        p = path_of("주문 생성이 안 돼")
        self.assertEqual(p[0], "shop-api")
        for c in ("mq", "pg", "shop-worker", "pay"):
            self.assertIn(c, p)
        # 직접 하류(mq, pg)가 흐름 끝(worker, pay)보다 먼저
        self.assertLess(p.index("mq"), p.index("shop-worker"))
        self.assertLess(p.index("shop-worker"), p.index("pay"))

    def test_flow_node_name_resolves_to_component(self):
        # 흐름 노드 '결제 대행' 이 그대로 남지 않고 구성요소 pay 로 바뀐다
        p = path_of("주문이 계속 재시작돼")
        self.assertIn("pay", p)
        self.assertNotIn("결제 대행", p)

    def test_component_name_node_does_not_leak_via_alias(self):
        # 'shop-worker' 노드가 alias 'shop' 때문에 shop-api 로 오인되면 안 된다:
        # 워커가 대상일 때 상류 shop-api 의 하류(mq·pg)를 하류로 끌어오지 않는다
        p = path_of("워커가 죽어")
        self.assertEqual(p[0], "shop-worker")
        self.assertNotIn("mq", p)
        self.assertNotIn("gw", p)
        self.assertIn("pay", p)
        self.assertIn("pg", p)

    def test_downstream_stays_within_flow(self):
        # 정산 흐름(worker → pg)은 shop-api 를 지나지 않으므로 그 흐름으로 건너가지 않는다
        flows = K.plan("c1", "주문 생성이 안 돼", data=DATA, incidents=[])["flows"]
        self.assertNotIn("정산", flows)

    def test_normal_patterns_follow_path(self):
        p = K.plan("c1", "주문 생성이 안 돼", data=DATA, incidents=[])
        self.assertEqual({n["component"] for n in p["normal"]}, {"shop-worker", "pay"})

    def test_queue_backlog_unchanged_order(self):
        self.assertEqual(path_of("mq 소비가 안 돼")[:2], ["shop-worker", "mq"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
