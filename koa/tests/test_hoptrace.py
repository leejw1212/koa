#!/usr/bin/env python3
"""hoptrace.py 검사 — 가짜 로그 줄로 줄 해석 · 요청 ID 잇기 · nginx upstream → 구성요소 · 세 홉 시간 대조를 본다.

  python3 koa/tests/test_hoptrace.py
"""
import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hoptrace as T  # noqa: E402

G_UUID = "req-6be2d61d-1111-2222-3333-444455556666"
G_HEX = "req-f0ba6929819f4fadbd282fe65894238a"
LOCAL = "req-7cac5640-c812-4ba3-be99-769f8ee84e24"


def k(ts, rest):
    return "2026-01-02T10:00:%sZ %s" % (ts, rest)


def nginx_line(path, st, ua, rt, upstream, addr):
    return ('10.0.0.9 - - [02/Jan/2026:10:00:05 +0000] "GET %s HTTP/1.1" %s 12 "-" "%s" 300 %s [%s] [-] %s 12 %s %s '
            'abcdefabcdefabcdefabcdefabcdefab' % (path, st, ua, rt, upstream, addr, rt, st))


class LineRules(unittest.TestCase):
    def test_ids_hyphen_and_hex_are_one(self):
        self.assertEqual(T.ids_in("x %s y" % G_HEX), ["f0ba6929819f4fadbd282fe65894238a"])
        self.assertEqual(T.ids_in("[%s]" % "req-f0ba6929-819f-4fad-bd28-2fe65894238a"),
                         ["f0ba6929819f4fadbd282fe65894238a"])

    def test_nginx_32hex_tail_is_not_a_request_id(self):
        self.assertEqual(T.ids_in(nginx_line("/v1/x", "200", "ua", "0.1", "ns_svc_8080", "1.2.3.4:8080")), [])

    def test_nginx_retry_lists(self):
        line = ('10.0.0.9 - - [02/Jan/2026:10:00:05 +0000] "POST /v1/users/bulk HTTP/2.0" 200 9 "-" "python-requests/2.32.5" '
                '300 0.374 [ns_svc_8080] [] 10.1.1.1:8080, 10.1.1.2:8080 0, 1047 0.001, 0.373 502, 200 abcdefabcdefabcdefabcdefabcdefab')
        n = T.parse_nginx(line)
        self.assertEqual((n["upaddr"], n["ustatus"], n["tries"], n["rt"]), ("10.1.1.2:8080", "200", 2, 0.374))

    def test_kubelet_time_and_rest(self):
        t, rest = T.kubelet_time("2026-01-02T10:00:05.123456789Z hello")
        self.assertEqual((t.second, t.microsecond, rest), (5, 123456, "hello"))
        self.assertEqual(T.kubelet_time("no prefix"), (None, "no prefix"))
        t, rest = T.kubelet_time("2026-01-02T19:00:05.5+09:00 x")      # 노드 시간대 → UTC
        self.assertEqual((t.hour, t.second, t.microsecond, rest), (10, 5, 500000, "x"))

    def test_served_oslo_and_access(self):
        s = T.parse_served("INFO x [req-a] Processed request: 404 NOT FOUND - GET http://console.example/svc/v1/a?b=1 x.py:30")
        self.assertEqual((s["status"], s["method"], s["host"], s["path"]), ("404", "GET", "console.example", "/svc/v1/a?b=1"))
        s = T.parse_served('1.1.1.1 "POST /v1/items HTTP/1.1" 201 5')
        self.assertEqual((s["status"], s["method"], s["path"]), ("201", "POST", "/v1/items"))

    def test_call_with_duration(self):
        c = T.parse_call("Openstack call https://svc-a.example/v1/items time 0.4614 client.py:158")
        self.assertEqual((c["host"], c["path"], c["sec"]), ("svc-a.example", "/v1/items", 0.4614))
        self.assertIsNone(T.parse_call("Openstack call https://svc-a.example/v1/items client.py:118"))

    def test_upstream_label_both_styles(self):
        svcs = {("shop", "order-api"): "order-api", ("shop", "order"): "x"}
        self.assertEqual(T.upstream_service("shop_order-api_8080", svcs), ("shop", "order-api"))
        self.assertEqual(T.upstream_service("shop-order-api-8080", svcs), ("shop", "order-api"))
        self.assertIsNone(T.upstream_service("other_order-api_8080", svcs))


class Chain(unittest.TestCase):
    def setUp(self):
        self.pod_comp = {"wrap-1": "wrapper", "ngx-1": "nginx", "api-1": "api", "api-2": "api"}
        self.svc = {("shop", "api"): "api"}

    def logs(self, wrapper_id, wrapper_sec=0.46, rt="0.418"):
        return {
            # 앱: Begin 줄이 global(wrapper 가 준 것) + local 을 함께 찍고, 처리 줄은 local 만
            "api-1": [k("04.000", "INFO ctx [%s] Begin processing global request %s, request %s" % (LOCAL, wrapper_id, LOCAL)),
                      k("04.408", "INFO base [%s - -] Processed request: 200 OK - GET http://console.example/svc/v1/items/42 b.py:30" % LOCAL)],
            "api-2": [k("01.000", "INFO base [req-99999999-8888-7777-6666-555544443333] Processed request: 404 NOT FOUND - GET http://svc-a.example/v1/internal b.py:30")],
            "ngx-1": [k("04.410", nginx_line("/v1/items/42", "200", "Mozilla/5.0 Chrome", rt, "shop_api_8080", "10.1.1.1:8080"))],
            "wrap-1": [k("04.412", "INFO client [%s -] Openstack call https://svc-a.example/v1/items/42 time %s c.py:158" % (wrapper_id, wrapper_sec)),
                       k("04.420", "INFO ctx [%s -] Processed request: 200 OK - GET http://console.example/console/api/svc/v1/items/42 c.py:40" % wrapper_id)],
        }

    def run_it(self, logs):
        now = dt.datetime(2026, 1, 2, 10, 30)
        return T.analyze(logs, self.pod_comp, self.svc, now=now)

    def test_hex_wrapper_id_joins_through_begin_line(self):
        res, depth = self.run_it(self.logs(G_HEX))
        api = res["api"]
        self.assertEqual(api["requests"], 2)
        self.assertEqual(api["sessions"], 1)
        self.assertIn(("wrapper", 1), api["callers"])
        s = api["sample"]
        self.assertEqual(s["caller_call"]["comp"], "wrapper")
        self.assertEqual(s["nginx"]["pod"], "ngx-1")
        self.assertEqual(s["nginx"]["ua"], "Browser")
        self.assertAlmostEqual(s["app_sec"], 0.408, places=3)
        self.assertTrue(s["order_ok"])
        self.assertEqual(api["nginx"]["lines"], 1)
        self.assertEqual(depth["nginx"], 29 * 60 + 55)        # 10:00:04.410 → 10:30

    def test_uuid_wrapper_id_too(self):
        res, _ = self.run_it(self.logs(G_UUID))
        self.assertEqual(res["api"]["sample"]["caller_call"]["comp"], "wrapper")

    def test_order_violation_is_reported(self):
        res, _ = self.run_it(self.logs(G_HEX, wrapper_sec=0.1))
        self.assertFalse(res["api"]["sample"]["order_ok"])

    def test_unrelated_request_has_no_caller(self):
        res, _ = self.run_it(self.logs(G_HEX))
        self.assertIn(("(이어진 곳 없음)", 1), res["api"]["callers"])

    def test_line_with_many_ids_does_not_merge_chains(self):
        logs = self.logs(G_HEX)
        other = "req-99999999-8888-7777-6666-555544443333"          # api-2 의 404 요청
        third = "req-abcdefab-1234-1234-1234-123456789012"
        logs["api-2"].append(k("02.000", "batch %s %s %s" % (other, third, LOCAL)))   # 3개 → 묶지 않는다
        res, _ = self.run_it(logs)
        self.assertIn(("(이어진 곳 없음)", 1), res["api"]["callers"])

    def test_app_time_uses_own_ids_not_whole_chain(self):
        logs = self.logs(G_HEX)
        # 같은 세션(global)이 앞서 이 앱을 한 번 더 불렀다: global 은 일찍 찍혀 있어도 이번 요청은 local 시작부터
        logs["api-1"].insert(0, k("00.000", "INFO ctx [req-00000000-1111-2222-3333-444444444444] Begin processing global request %s, request req-00000000-1111-2222-3333-444444444444" % G_HEX))
        # 처리 줄이 global·local 을 함께 찍는 앱 (대괄호 [global local])
        logs["api-1"][-1] = logs["api-1"][-1].replace("[%s - -]" % LOCAL, "[%s %s -]" % (G_HEX, LOCAL))
        res, _ = self.run_it(logs)
        self.assertAlmostEqual(res["api"]["sample"]["app_sec"], 0.408, places=3)

    def test_prefers_full_three_hop_session_over_newer_partial(self):
        logs = self.logs(G_HEX)
        newer = "req-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        logs["api-1"].append(k("09.000", "INFO base [%s] Processed request: 200 OK - GET http://console.example/svc/v1/other b.py:30" % newer))
        logs["wrap-1"].append(k("09.100", "INFO ctx [%s] Processed request: 200 OK - GET http://console.example/console/api/svc/v1/other c.py:40" % newer))
        res, _ = self.run_it(logs)
        self.assertTrue(res["api"]["sample"]["request"].endswith("/items/42"))

    def test_nginx_is_not_a_served_component(self):
        res, _ = self.run_it(self.logs(G_HEX))
        self.assertNotIn("nginx", res)


if __name__ == "__main__":
    unittest.main(verbosity=1)
