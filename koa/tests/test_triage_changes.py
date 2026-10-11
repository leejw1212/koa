#!/usr/bin/env python3
"""트리아지 변경 타임라인 검사 — 클러스터 없이 합성 객체만.

  python3 koa/tests/test_triage_changes.py

- NetworkPolicy 생성·수정이 변경 타임라인에 들어가고, 허용 목록이 요약된다
  (파드가 멀쩡한데 통신만 끊기는 장애에서 triage 가 '변경 0건' 으로 말하던 것)
- 기간 밖의 정책은 넣지 않는다
"""
import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import triage as T  # noqa: E402

UTC = dt.timezone.utc
AT = dt.datetime(2026, 1, 2, 3, 0, 0, tzinfo=UTC)


def iso(minutes_ago):
    return (AT - dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def netpol(name, created, edited=None, spec=None):
    m = {"name": name, "namespace": "shop", "creationTimestamp": iso(created)}
    if edited is not None:
        m["managedFields"] = [{"manager": "kubectl", "time": iso(created)}, {"manager": "kubectl", "time": iso(edited)}]
    return {"metadata": m, "spec": spec or {
        "podSelector": {"matchLabels": {"app": "api"}}, "policyTypes": ["Ingress"],
        "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "ingress"}}}],
                     "ports": [{"protocol": "TCP", "port": 8000}]}]}}


def empty(**extra):
    d = {k: [] for k in ("nodes", "pods", "events", "deployments.apps", "replicasets.apps", "statefulsets.apps",
                         "daemonsets.apps", "jobs.batch", "services", "controllerrevisions.apps", "endpointslices")}
    d.update(extra)
    return d


class NetworkPolicyChanges(unittest.TestCase):
    def changes(self, *nps):
        _, changes, _ = T.analyze(empty(networkpolicies=list(nps)), AT, dt.timedelta(hours=1), dt.timedelta(hours=6))
        return changes

    def test_created_policy_is_a_change_with_allow_list(self):
        ch = self.changes(netpol("api-allow", created=10))
        self.assertEqual(len(ch), 1)
        c = ch[0]
        self.assertEqual(c["target"], "NetworkPolicy/api-allow")
        self.assertEqual(c["ns"], "shop")
        self.assertTrue(c["what"].startswith("생성"))
        self.assertIn("app=api", c["what"])
        self.assertIn("kubernetes.io/metadata.name=ingress", c["what"])
        self.assertIn("8000", c["what"])

    def test_edit_time_from_managed_fields(self):
        ch = self.changes(netpol("old", created=60 * 24, edited=5))
        self.assertEqual(len(ch), 1)
        self.assertTrue(ch[0]["what"].startswith("수정"))
        self.assertEqual(ch[0]["at"], AT - dt.timedelta(minutes=5))

    def test_outside_lookback_ignored(self):
        self.assertEqual(self.changes(netpol("old", created=60 * 24)), [])

    def test_deny_all_and_egress(self):
        s = T.netpol_summary({"spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"],
                                       "egress": [{"to": [{"ipBlock": {"cidr": "10.0.0.0/8"}}]}]}})
        self.assertIn("대상 파드 모든", s)
        self.assertIn("Ingress 전부 차단", s)
        self.assertIn("Egress 허용 ip 10.0.0.0/8:*", s)


if __name__ == "__main__":
    unittest.main()
