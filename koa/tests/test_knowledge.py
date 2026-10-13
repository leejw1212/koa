#!/usr/bin/env python3
"""knowledge.py 검사 — 꺼내기(context) · 더하기(add) · 제안(suggest). 임시 폴더에서만 (실제 local/knowledge 는 안 건드림).

  python3 koa/tests/test_knowledge.py
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import knowledge as K  # noqa: E402

# gw → shop-api → mq → shop-worker → 결제 대행(외부),  gw → shop-api → pg,  shop-worker → pg (정산)
# alias 'shop' 이 shop-worker 에 부분 문자열로 걸리는 함정도 같이 둔다. 흐름 칸은 구성요소 이름 그대로.
DATA = {
    "components": {
        "gw": {"workload": "edge/Deployment/gw", "kind": "ingress"},
        "shop-api": {"workload": "shop/Deployment/api", "kind": "app", "aliases": ["shop", "주문 api"]},
        "mq": {"workload": "shop/StatefulSet/mq", "kind": "message-queue", "aliases": ["래빗"]},
        "shop-worker": {"workload": "shop/Deployment/worker", "kind": "app", "aliases": ["워커"]},
        "pg": {"workload": "data/StatefulSet/pg", "kind": "database"},
        "pay": {"aliases": ["결제사", "결제 대행"], "external": True},
        "grafana": {"workload": "monitoring/Deployment/grafana"},
    },
    "flows": {
        "주문-만들기": {"kind": "queue", "hops": [
            {"from": "gw", "to": "shop-api", "role": "call"},
            {"from": "shop-api", "to": "mq", "role": "produce"},
            {"from": "mq", "to": "shop-worker", "role": "consume"},
            {"from": "shop-worker", "to": "pay", "role": "call"}]},
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

    def test_flow_node_is_component_name(self):
        p = ctx("워커가 죽어")
        self.assertIn("pay", p["components"])
        self.assertEqual(K.node_to_comps("pay", DATA["components"]), ["pay"])
        self.assertEqual(K.node_to_comps("결제 대행", DATA["components"]), [])   # 별명은 흐름 칸이 아니다
        self.assertEqual(K.node_to_comps("shop", DATA["components"]), [])       # 부분 문자열로 붙지 않는다

    def test_group_picks_its_components(self):
        d = {"components": {"a": {"group": "결제"}, "b": {"group": "결제"}, "c": {"group": "인증"}},
             "flows": {}, "normal": []}
        p = K.context("c1", "결제 쪽이 이상해", data=d, incidents=[])
        self.assertEqual(p["asked"], ["a", "b"])
        self.assertEqual(K.context("c1", "", names=["인증"], data=d, incidents=[])["asked"], ["c"])
        d["components"]["결제"] = {}
        self.assertTrue(any("그룹 '결제'" in m for _, m in K.check("c1", d)))

    def test_group_as_flow_node(self):
        # 클라이언트 → edge → wrapper(admin/manager/user) → nginx → api
        d = {"components": {"client": {"external": True}, "edge": {"workload": "e/Deployment/edge"},
                            "admin-wrapper": {"workload": "c/Deployment/admin", "group": "wrapper"},
                            "manager-wrapper": {"workload": "c/Deployment/manager", "group": "wrapper"},
                            "user-wrapper": {"workload": "c/Deployment/user", "group": "wrapper"},
                            "nginx": {"workload": "i/Deployment/nginx"}, "api": {"workload": "b/Deployment/api"},
                            "other": {"workload": "x/Deployment/other"}},
             "flows": {"콘솔": {"hops": ["client", "edge", "wrapper", "nginx", "api"]}}, "normal": []}
        self.assertEqual(K.node_to_comps("wrapper", d["components"]), ["admin-wrapper", "manager-wrapper", "user-wrapper"])
        self.assertEqual(K.check("c1", d), [])
        p = K.context("c1", "manager-wrapper 가 500 을 낸다", data=d, incidents=[])
        self.assertIn("콘솔", p["flows"])                                   # 멤버를 물어도 그룹 칸의 흐름
        self.assertEqual(p["groups"], {"wrapper": ["admin-wrapper", "manager-wrapper", "user-wrapper"]})
        self.assertEqual(p["group_scope"], {"wrapper": ["manager-wrapper"]})   # 말한 멤버만 범위에 (나머지는 다른 갈래)
        self.assertNotIn("user-wrapper", p["components"])
        self.assertNotIn("other", p["components"])
        self.assertIn("그룹 wrapper (3개", K.context_text(p))
        self.assertIn("user-wrapper", K.context_text(p))                     # 이름은 남긴다 (넓힐 때 쓰게)
        # 그룹 이름을 말하면 전부
        self.assertIn("user-wrapper", K.context("c1", "wrapper 가 이상해", data=d, incidents=[])["components"])
        # 멤버를 안 말한 작은 그룹 칸(GROUP_EXPAND 이하)은 전부 범위에
        self.assertIn("user-wrapper", K.context("c1", "nginx 가 느려", data=d, incidents=[])["components"])
        # 띄어 쓴 이름은 그 구성요소 하나 (그룹 이름 'wrapper' 로 셋 다 잡히지 않게)
        d["components"]["manager-console-wrapper"] = d["components"].pop("manager-wrapper")
        self.assertEqual(K.context("c1", "manager console wrapper 에서 502", data=d, incidents=[])["asked"],
                         ["manager-console-wrapper"])

    def test_normal_and_incident_on_group(self):
        # 정상 패턴·과거 이슈의 구성요소 칸에 그룹 이름 → 그 그룹의 구성요소 중 하나라도 범위에 있으면 꺼낸다
        d = {"components": {"edge": {"workload": "e/Deployment/edge"},
                            "admin-wrapper": {"workload": "c/Deployment/admin", "group": "wrapper"},
                            "user-wrapper": {"workload": "c/Deployment/user", "group": "wrapper"},
                            "api": {"workload": "a/Deployment/api"}, "batch": {"workload": "b/CronJob/batch"}},
             "flows": {"f": {"hops": ["edge", "wrapper", "api"]}},
             "normal": [{"component": "wrapper", "note": "배포 때 30초 502"}, {"component": "batch", "note": "새벽에만 돈다"}]}
        incs = [{"file": "w.md", "meta": {"components": ["wrapper"], "cause": "wrapper 전체 OOM"}, "body": ""}]
        p = K.context("c1", "admin-wrapper 가 502 를 낸다", data=d, incidents=incs)
        self.assertEqual(p["normal"], [{"component": "wrapper", "note": "배포 때 30초 502",
                                        "members": ["admin-wrapper", "user-wrapper"]}])
        self.assertEqual([k["file"] for k in p["incidents"]], ["w.md"])
        self.assertIn("노트 wrapper (그룹: admin-wrapper, user-wrapper) [노트]: 배포 때 30초 502", K.context_text(p))
        # 흐름 위의 다른 구성요소(api)를 물으면: 그룹은 한 곳이라 '범위 안 두 곳' 에 걸리지 않는다
        self.assertEqual(K.context("c1", "api 가 느려", data=d, incidents=incs)["incidents"], [])
        self.assertEqual(K.context("c1", "batch 가 안 돈다", data=d, incidents=incs)["normal"],
                         [{"component": "batch", "note": "새벽에만 돈다"}])
        self.assertEqual(K.check("c1", d), [])                              # 그룹 이름은 경고하지 않는다
        d["normal"].append({"component": "Nginx 로그", "note": "x"})        # 주제 노트도 경고하지 않는다
        self.assertEqual(K.check("c1", d), [])
        d["normal"].append({"component": "api-server", "note": "x"})        # 구성요소 이름을 잘못 쓴 것 같은 대상은 알린다
        self.assertTrue(any("'api-server'" in m and "→ api" in m for _, m in K.check("c1", d)))

    def test_incident_text_finds_group(self):
        comps = {"admin-wrapper": {"group": "wrapper"}, "user-wrapper": {"group": "wrapper"}, "api": {}}
        self.assertEqual(K.incident_meta("2026-10-01 wrapper 전체가 502. 원인: 노드 장애", comps)["components"], ["wrapper"])
        self.assertEqual(K.incident_meta("admin-wrapper 만 502", comps)["components"], ["admin-wrapper"])

    def test_explicit_name_by_alias(self):
        self.assertEqual(ctx("", names=["결제사"])["asked"], ["pay"])

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


class Notes(unittest.TestCase):
    """노트 종류 · 주제 노트 · 흐름 별명 · 이름이 안 걸린 질문 (0.2.19)."""

    def data(self):
        d = json.loads(json.dumps(DATA))
        d["flows"]["주문-만들기"]["aliases"] = ["카드 만들기"]
        d["normal"] += [
            {"component": "Nginx 로그", "note": "upstream_status 가 비고 504 면 백엔드 미도달 → 앱 로그 볼 필요 없음", "kind": "rule"},
            {"component": "Nginx 로그", "note": "upstream_status 500 이면 백엔드가 500 을 낸 것 → 컨테이너 로그로"},
            {"component": "콘솔 nginx", "note": "로그 시각이 실제보다 +9h 오프셋 — 시각 맞출 때 보정", "kind": "pitfall"},
            {"component": "container 로그", "note": "logs-* 인덱스를 app 라벨로 좁혀 조회", "kind": "보는 법"}]
        return d

    def test_topic_notes_reach_any_question(self):
        p = K.context("c1", "워커가 계속 죽어", data=self.data(), incidents=[])
        self.assertEqual([n["component"] for n in p["normal"]], ["shop-worker"])      # 구성요소 노트는 범위 안 것만
        self.assertEqual([n["component"] for n in p["topics"]],
                         ["Nginx 로그", "Nginx 로그", "콘솔 nginx", "container 로그"])     # 주제 노트는 늘, 적힌 순서로
        self.assertEqual(p["topics"][3]["kind"], "howto")                             # 한글 종류 이름도 받는다
        t = K.context_text(p)
        self.assertIn("주제 노트 (클러스터 전체):", t)
        self.assertIn("  Nginx 로그 [판정 규칙]: upstream_status 가 비고 504", t)
        self.assertIn("  Nginx 로그 [노트]: upstream_status 500", t)                     # 종류 없음은 '노트'
        self.assertIn("판정 규칙=어느 hop 탓인지 가르고", t)                             # 나온 종류만 읽는 법을 붙인다
        self.assertNotIn("평소 수치=", t)

    def test_note_on_alias_is_the_component_note(self):
        # 별명('래빗' = mq)에 붙인 노트는 주제 노트가 아니라 그 구성요소의 노트. load_flows 는 보통 구성요소 이름으로 바꿔 두지만
        # (migrate), 그 전의 데이터나 두 구성요소에 걸린 별명은 그대로 온다 → 대상은 적힌 그대로 두고 누구인지 덧붙인다
        d = self.data()
        d["normal"].append({"component": "래빗", "note": "큐 길이 1000 까지는 평소", "kind": "baseline"})
        p = K.context("c1", "래빗 큐가 쌓여", data=d, incidents=[])
        self.assertIn({"component": "래빗", "note": "큐 길이 1000 까지는 평소", "kind": "baseline", "alias_of": ["mq"]},
                      p["normal"])
        self.assertNotIn("래빗", [n["component"] for n in p["topics"]])
        self.assertIn("노트 래빗 (= mq) [평소 수치]: 큐 길이 1000 까지는 평소", K.context_text(p))
        self.assertNotIn("래빗", [n["component"] for n in K.context("c1", "pg 가 느려", data=d, incidents=[])["normal"]])
        self.assertEqual([m for _, m in K.check("c1", d) if "래빗" in m and "날짜" not in m], [])   # 별명 대상은 경고 안 함 (평소 수치 날짜 경고는 별개)

    def test_topic_budget_keeps_relevant_first(self):
        d = self.data()
        d["normal"] += [{"component": "잡담 %d" % i, "note": "가" * 300} for i in range(20)]
        p = K.context("c1", "nginx 504 가 많아", data=d, incidents=[])
        got = [n["component"] for n in p["topics"]]
        self.assertIn("Nginx 로그", got)                                            # 질문과 겹치는 주제가 먼저 들어간다
        self.assertLessEqual(sum(len(n["component"]) + len(n["note"]) + 12 for n in p["topics"]), K.TOPIC_BUDGET)
        self.assertTrue(any("길어서 뺐다" in n and "notes c1" in n for n in p["notes"]))

    def test_flow_alias_is_a_name(self):
        p = K.context("c1", "카드 만들기가 안 끝나요", data=self.data(), incidents=[])
        self.assertEqual(list(p["flows"]), ["주문-만들기"])
        self.assertEqual(p["guessed"], [])

    def test_closest_flow_when_no_name_matched(self):
        d = self.data()
        p = K.context("c1", "결제가 자꾸 실패해요", data=d, incidents=[])        # '결제' 는 pay 의 별명 '결제사' 와 겹친다
        self.assertEqual(p["guessed"], ["주문-만들기"])
        self.assertIn("pay", p["components"])
        self.assertTrue(any("주문-만들기 흐름으로 읽었어요" in n for n in p["notes"]))
        self.assertIn("가까운 흐름으로 시작: 주문-만들기", K.context_text(p))
        q = K.context("c1", "뭔가 느려", data=d, incidents=[])                    # 겹치는 말이 없으면 묻는다
        self.assertEqual(q["guessed"], [])
        self.assertTrue(any("사용자에게 묻는다" in n for n in q["notes"]))

    def test_suggest_kind(self):
        self.assertEqual(K.suggest_kind("upstream_status 가 비어 있고 status=504 이면 백엔드 미도달"), "rule")
        self.assertEqual(K.suggest_kind("logs-* 인덱스; app 라벨로 좁혀서 조회"), "howto")
        self.assertEqual(K.suggest_kind("region 필드에 두 리전 이벤트가 공존 — 조회 시 region 으로 좁혀야 한다"), "pitfall")
        self.assertEqual(K.suggest_kind("워커가 rabbitmq 에 AMQP 연결 — notification 발행"), "structure")
        self.assertEqual(K.suggest_kind("매일 02:00 배치 때 큐가 쌓였다 빠진다"), "normal")
        self.assertIsNone(K.suggest_kind("결제사 502 는 재시도 후 dead-letter"))

    def test_kinds_are_checked(self):
        d = self.data()
        self.assertEqual(K.check("c1", d), [])
        d["normal"].append({"component": "grafana", "note": "y", "kind": "아무거나"})
        self.assertTrue(any("종류 '아무거나'" in m for _, m in K.check("c1", d)))
        d["flows"]["정산"]["aliases"] = ["워커"]                                  # 구성요소 별명과 같은 흐름 별명
        self.assertTrue(any("별명 '워커'" in m and "shop-worker" in m for _, m in K.check("c1", d)))

    def test_list_notes_hints_untyped(self):
        items = K.list_notes("c1", "nginx", untyped=True, data=self.data())
        self.assertEqual([(n["component"], n.get("hint_kind"), n["topic"]) for n in items], [("Nginx 로그", "rule", True)])
        self.assertIn("[종류 없음 → 제안: 판정 규칙] Nginx 로그 (주제): upstream_status 500", K.notes_text(items))


class NoteStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig = K.KNOWLEDGE
        K.KNOWLEDGE = Path(self.tmp.name)
        K.save_shards("c1", {
            "components": {"api": {"workload": "shop/Deployment/api"}, "db": {"workload": "data/StatefulSet/pg"}},
            "flows": [{"name": "주문", "nodes": ["api", "db"], "aliases": ["주문 넣기"]}],
            "normal": [{"component": "Nginx 로그", "note": "504 면 백엔드 미도달"},
                       {"component": "api", "note": "배포 때 30초 502", "kind": "normal"},
                       {"component": "api", "note": "같은 메모 두 번", "kind": "모르는 종류"},
                       {"component": "api", "note": "같은 메모 두 번", "kind": "rule"}]})

    def tearDown(self):
        K.KNOWLEDGE = self.orig
        self.tmp.cleanup()

    def test_save_keeps_kinds_and_flow_aliases(self):
        d = K.load_flows("c1")
        self.assertEqual(d["flows"]["주문"]["aliases"], ["주문 넣기"])
        self.assertEqual(d["normal"], [{"component": "Nginx 로그", "note": "504 면 백엔드 미도달"},
                                       {"component": "api", "note": "배포 때 30초 502", "kind": "normal"},
                                       {"component": "api", "note": "같은 메모 두 번"}])   # 모르는 종류는 빼고, 같은 글은 처음 것만

    def test_add_fills_kind_but_never_overwrites(self):
        r = K.apply_additions("c1", {"normal": [{"component": "Nginx 로그", "note": "504 면 백엔드 미도달", "kind": "판정 규칙"},
                                                {"component": "api", "note": "배포 때 30초 502", "kind": "rule"},
                                                {"component": "db", "note": "백업 때 느림", "kind": "baseline"},
                                                {"component": "db", "note": "z", "kind": "엉뚱"}]})
        self.assertEqual([x["component"] for x in r["kinded"]], ["Nginx 로그"])
        self.assertEqual(r["normal"], [{"component": "db", "note": "백업 때 느림", "kind": "baseline"}])
        self.assertEqual([n for n, _ in r["skipped"]], ["노트 db"])
        text = K.additions_text(r)
        self.assertIn("~ 노트 Nginx 로그: 종류 → 판정 규칙", text)
        self.assertIn("+ 노트 db [평소 수치]: 백업 때 느림", text)
        kinds = {x["note"]: x.get("kind") for x in K.load_flows("c1")["normal"]}
        self.assertEqual(kinds["504 면 백엔드 미도달"], "rule")
        self.assertEqual(kinds["배포 때 30초 502"], "normal")                     # 정해 둔 종류는 안 바꾼다
        self.assertFalse(K.apply_additions("c1", {"normal": [{"component": "Nginx 로그", "note": "504 면 백엔드 미도달",
                                                              "kind": "rule"}]})["changed"])

    def test_add_alias_to_existing_flow(self):
        r = K.apply_additions("c1", {"flows": [{"name": "주문", "aliases": ["주문 넣기", "결제 흐름"]}]})
        self.assertEqual(r["flow_aliases"], {"주문": ["결제 흐름"]})
        self.assertIn("~ 흐름 주문 (별명: 결제 흐름)", K.additions_text(r))
        self.assertEqual(K.load_flows("c1")["flows"]["주문"]["aliases"], ["주문 넣기", "결제 흐름"])
        self.assertFalse(K.apply_additions("c1", {"flows": [{"name": "주문", "aliases": ["결제 흐름"]}]})["changed"])
        sid = K.add_suggestion("c1", "별명", {"flows": [{"name": "주문", "aliases": ["오더"]}]}, "분석")
        sid2 = K.add_suggestion("c1", "별명", {"flows": [{"name": "주문", "aliases": ["오더2"]}]}, "분석")
        self.assertTrue(sid and sid2 and sid != sid2)                            # 별명만 다른 제안도 따로 남는다

    def test_web_save_merges_kind_set_in_chat(self):
        meta = {}
        d = K.load_flows("c1", meta=meta)
        page = {"components": d["components"], "normal": [dict(x) for x in d["normal"]],
                "flows": [{"name": n, "kind": f.get("kind"), "nodes": K.flow_nodes(f), "aliases": f.get("aliases")}
                          for n, f in d["flows"].items()]}
        base = {"version": meta["version"], "data": json.loads(json.dumps(page))}
        K.apply_additions("c1", {"normal": [{"component": "Nginx 로그", "note": "504 면 백엔드 미도달", "kind": "rule"}],
                                 "flows": [{"name": "주문", "aliases": ["오더"]}]})          # 채팅이 종류·별명을 더함
        page["flows"][0]["kind"] = "queue"                                                # 웹에서는 흐름 종류를 고침
        K.save_shards("c1", page, base=base)
        d = K.load_flows("c1")
        self.assertEqual(d["normal"][0].get("kind"), "rule")
        self.assertEqual(d["flows"]["주문"]["aliases"], ["주문 넣기", "오더"])               # 흐름도 칸별로 합친다
        self.assertEqual(d["flows"]["주문"]["kind"], "queue")
        page2 = {"components": d["components"], "normal": [dict(x) for x in d["normal"]],
                 "flows": [{"name": n, "kind": f.get("kind"), "nodes": K.flow_nodes(f)} for n, f in d["flows"].items()]}
        meta = {}
        K.load_flows("c1", meta=meta)
        base = {"version": meta["version"], "data": json.loads(json.dumps(page2))}
        K.apply_additions("c1", {"normal": [{"component": "api", "note": "같은 메모 두 번", "kind": "howto"}]})
        page2["normal"][2]["kind"] = "pitfall"                                            # 같은 노트에 다른 종류
        with self.assertRaises(K.Conflict) as cm:
            K.save_shards("c1", page2, base=base)
        self.assertEqual(cm.exception.items, ["노트 api (종류)"])


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


class WebSave(unittest.TestCase):
    """웹 화면은 연 때의 상태 전체를 저장한다. 그사이 KOA 채팅이 더한 것을 지우면 안 된다 (base 로 합친다)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig = K.KNOWLEDGE
        K.KNOWLEDGE = Path(self.tmp.name)
        K.save_shards("c1", {
            "components": {"api": {"workload": "shop/Deployment/api", "aliases": ["에이피아이"]},
                           "db": {"workload": "data/StatefulSet/pg", "kind": "database"}},
            "flows": [{"name": "주문", "nodes": ["api", "db"]}],
            "normal": [{"component": "api", "note": "사람이 쓴 메모"}]})

    def tearDown(self):
        K.KNOWLEDGE = self.orig
        self.tmp.cleanup()

    def open_page(self):
        """웹이 화면을 열 때처럼: 읽은 버전 + 화면 상태 (흐름은 목록)."""
        meta = {}
        d = K.load_flows("c1", meta=meta)
        state = {"components": json.loads(json.dumps(d["components"])),
                 "flows": [{"name": n, "kind": f.get("kind"), "nodes": K.flow_nodes(f)} for n, f in d["flows"].items()],
                 "normal": [dict(x) for x in d["normal"]]}
        return state, {"version": meta["version"], "data": json.loads(json.dumps(state))}

    def test_save_keeps_what_chat_added_meanwhile(self):
        page, base = self.open_page()
        K.apply_additions("c1", {"components": {"api": {"aliases": ["주문 API"]}, "cache": {"workload": "shop/StatefulSet/cache"}},
                                 "normal": [{"component": "db", "note": "백업 때 느림"}]})       # 채팅이 더함
        page["components"]["api"]["aliases"].append("API")                                    # 웹에서 고친 것
        page["components"]["web"] = {"workload": "shop/Deployment/web"}
        page["normal"][0]["note"] = "고친 메모"
        r = K.save_shards("c1", page, base=base)
        d = K.load_flows("c1")
        self.assertEqual(d["components"]["api"]["aliases"], ["에이피아이", "주문 API", "API"])
        self.assertEqual(set(d["components"]), {"api", "db", "web", "cache"})
        self.assertEqual(d["normal"], [{"component": "db", "note": "백업 때 느림"}, {"component": "api", "note": "고친 메모"}])
        self.assertEqual(sorted(r["merged"]), ["구성요소 api", "구성요소 cache", "노트 db"])

    def test_deletes_on_both_sides_survive(self):
        page, base = self.open_page()
        K.save_shards("c1", dict(page, flows=[]))                     # 다른 창에서 흐름을 지웠다
        del page["components"]["db"]                                  # 이 창에서는 db 를 지웠다
        K.save_shards("c1", page, base=base)
        d = K.load_flows("c1")
        self.assertEqual((set(d["components"]), d["flows"]), ({"api"}, {}))

    def test_same_field_both_sides_is_a_conflict(self):
        page, base = self.open_page()
        other = json.loads(json.dumps(page))
        other["components"]["db"]["workload"] = "data/StatefulSet/pg-new"
        K.save_shards("c1", other)                                    # 다른 창에서 저장
        before = (K.KNOWLEDGE / "c1" / "flows.yaml").read_text()
        page["components"]["db"]["workload"] = "data/StatefulSet/pg-old"
        with self.assertRaises(K.Conflict) as cm:
            K.save_shards("c1", page, base=base)
        self.assertEqual(cm.exception.items, ["구성요소 db(workload)"])
        self.assertEqual((K.KNOWLEDGE / "c1" / "flows.yaml").read_text(), before)   # 아무것도 쓰지 않는다

    def test_delete_vs_change_is_a_conflict(self):
        page, base = self.open_page()
        K.apply_additions("c1", {"components": {"db": {"aliases": ["피지"]}}})
        del page["components"]["db"]
        with self.assertRaises(K.Conflict) as cm:
            K.save_shards("c1", page, base=base)
        self.assertEqual(cm.exception.items, ["구성요소 db(지움)"])

    def test_nothing_changed_meanwhile_saves_as_is(self):
        page, base = self.open_page()
        page["components"]["api"]["aliases"] = []
        r = K.save_shards("c1", page, base=base)
        self.assertEqual(r["merged"], [])
        self.assertNotIn("aliases", K.load_flows("c1")["components"]["api"])


class Migrate(unittest.TestCase):
    """예전 지식: 흐름 칸이 flow_nodes·별명·대소문자 다른 이름으로 구성요소를 가리키던 것 → 구성요소 이름으로."""

    def old(self):
        return {"components": {
            "public-gw": {"workload": "gw/Deployment/public", "kind": "gateway", "flow_nodes": ["Gateway"]},
            "admin-gw": {"workload": "gw/Deployment/admin", "kind": "gateway", "flow_nodes": ["Gateway"]},
            "console": {"workload": "ui/Deployment/console", "flow_nodes": ["Console Wrapper", "콘솔"]},
            "api": {"workload": "app/Deployment/api", "aliases": ["에이피아이"]},
            "db": {"workload": "data/StatefulSet/db"}},
            "flows": {"f": {"hops": ["Console Wrapper", "Gateway", "에이피아이", "DB", "K8s Service"]}},
            "normal": [{"component": "Console Wrapper", "note": "x"}]}

    def test_rewrites_nodes_and_groups_shared(self):
        d = self.old()
        moved = K.migrate(d)
        self.assertTrue(moved)
        self.assertEqual(d["flows"]["f"]["hops"], ["console", "Gateway", "api", "db", "K8s Service"])
        self.assertNotIn("Gateway", d["components"])                         # 공유 이름은 그룹으로
        self.assertEqual(K.node_to_comps("Gateway", d["components"]), ["public-gw", "admin-gw"])
        self.assertEqual(d["components"]["console"]["aliases"], ["Console Wrapper", "콘솔"])  # 질문에서 부르는 말로 남는다
        self.assertFalse(any("flow_nodes" in c for c in d["components"].values()))
        self.assertEqual(d["normal"][0]["component"], "console")
        self.assertEqual(K.migrate(d), [])                                    # 두 번 해도 같다
        chk = [m for _, m in K.check("c", d)]
        self.assertTrue(any("K8s Service" in m for m in chk))                 # 모르는 칸은 남기고 알린다
        self.assertFalse(any("'Gateway'" in m for m in chk))

    def test_member_question_reaches_group_flow(self):
        d = self.old()
        K.migrate(d)
        p = K.context("c", "public-gw 가 느려", data=d, incidents=[])
        self.assertIn("f", p["flows"])

    def test_theme_becomes_group(self):
        d = {"components": {"a": {"theme": "결제"}}, "flows": {}, "normal": []}
        self.assertTrue(K.migrate(d))
        self.assertEqual(d["components"]["a"], {"group": "결제"})

    def test_load_writes_once_with_backup(self):
        with tempfile.TemporaryDirectory() as t:
            orig, K.KNOWLEDGE = K.KNOWLEDGE, Path(t)
            try:
                (Path(t) / "c").mkdir()
                K._write_flows("c", self.old())
                before = (Path(t) / "c" / "flows.yaml").read_text()
                notes = []
                K.load_flows("c", notes=notes)
                self.assertTrue(notes)
                self.assertEqual((Path(t) / "c" / "flows.yaml.pre-names.bak").read_text(), before)
                self.assertNotIn("flow_nodes", (Path(t) / "c" / "flows.yaml").read_text())
                again = []
                K.load_flows("c", notes=again)
                self.assertEqual(again, [])
            finally:
                K.KNOWLEDGE = orig


class BigGroupScope(unittest.TestCase):
    """큰 그룹 칸(백엔드 여럿)이 흐름에 있을 때 context 가 형제 멤버의 노트·구성요소를 끌고 오지 않는다."""

    def data(self):
        comps = {"nginx": {"workload": "i/DaemonSet/nginx"}, "mq": {"workload": "m/StatefulSet/mq", "kind": "message-queue"}}
        for i in range(8):
            comps["api-%d" % i] = {"workload": "b/Deployment/api-%d" % i, "group": "백엔드"}
        return {"components": comps,
                "flows": {"http": {"hops": ["nginx", "백엔드"]}, "알림": {"hops": [
                    {"from": "백엔드", "to": "mq", "role": "produce"}]}},
                "normal": [{"component": "api-1", "note": "api-1 노트", "kind": "structure"},
                           {"component": "api-2", "note": "api-2 노트", "kind": "structure"},
                           {"component": "백엔드", "note": "그룹 노트", "kind": "structure"}]}

    def test_member_question_keeps_only_member(self):
        p = K.context("c1", "api-1 세션 추적", data=self.data(), incidents=[])
        self.assertEqual(sorted(p["components"]), ["api-1", "mq", "nginx"])
        self.assertEqual([n["note"] for n in p["normal"]], ["api-1 노트", "그룹 노트"])
        t = K.context_text(p)
        self.assertIn("그룹 백엔드 (8개", t)
        self.assertIn("노트 백엔드 (그룹 8개) [구조]: 그룹 노트", t)

    def test_unnamed_big_group_is_names_only(self):
        p = K.context("c1", "mq 가 쌓여", data=self.data(), incidents=[])
        self.assertEqual(sorted(p["components"]), ["mq"])
        self.assertEqual(p["group_scope"], {"백엔드": []})
        self.assertEqual([n["note"] for n in p["normal"]], ["그룹 노트"])      # 그룹 노트는 남는다
        self.assertIn("멤버가 많아 이름만 둔다", K.context_text(p))

    def test_group_named_takes_all(self):
        p = K.context("c1", "", names=["백엔드"], data=self.data(), incidents=[])
        self.assertEqual(len([c for c in p["components"] if c.startswith("api-")]), 8)


class CheckIncidentsAndBaseline(unittest.TestCase):
    def test_incident_component_unknown_and_weak_signature(self):
        d = {"components": {"api": {"workload": "a/Deployment/api"}}, "flows": {}, "normal": []}
        incs = [{"file": "x.md", "meta": {"components": ["api-outbox"], "signature": {"logs": [
            "10.1.2.3", "aiohttp", "Authorization failed for token"]}}}]
        msgs = [m for _, m in K.check("c1", d, incidents=incs)]
        self.assertTrue(any("x.md" in m and "'api-outbox'" in m and "안 나온다" in m for m in msgs))
        self.assertTrue(any("'10.1.2.3'" in m for m in msgs))
        self.assertTrue(any("'aiohttp'" in m for m in msgs))
        self.assertFalse(any("Authorization failed" in m for m in msgs))
        self.assertEqual(K.check("c1", d, incidents=[{"file": "y.md", "meta": {"components": ["api"]}}]), [])
        bad = K.parse_front("---\nlogs: [a, /x?y=]\n---\n본문")[0]           # 따옴표 없는 ?…] 는 YAML 오류 → 머리말이 빈다
        self.assertTrue(any("z.md" in m and "읽지 못했다" in m
                            for _, m in K.check("c1", d, incidents=[{"file": "z.md", "meta": bad}])))

    def test_baseline_needs_date_and_ages(self):
        d = {"components": {"api": {"workload": "a/Deployment/api"}}, "flows": {}, "normal": [
            {"component": "api", "note": "분당 300건", "kind": "baseline"},
            {"component": "api", "note": "분당 200건 (2020-01-01 잼)", "kind": "baseline"},
            {"component": "api", "note": "분당 250건 (%s 잼)" % K.dt.date.today().isoformat(), "kind": "baseline"}]}
        msgs = [m for _, m in K.check("c1", d, incidents=[])]
        self.assertEqual(len(msgs), 2)
        self.assertTrue(any("날짜가 없다" in m for m in msgs))
        self.assertTrue(any("2020-01-01" in m and "다시 재서" in m for m in msgs))


class EditNoteAndRecur(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.orig, K.KNOWLEDGE = K.KNOWLEDGE, Path(self.t.name)
        (Path(self.t.name) / "c").mkdir()
        K._write_flows("c", {"schema": "koa.knowledge/v1", "cluster": "c",
                             "components": {"api": {"workload": "a/Deployment/api"}, "job": {"workload": "a/Deployment/job"}},
                             "flows": {"f": {"kind": "http", "hops": ["api", "job"]}},
                             "normal": [{"component": "api", "note": "request-id 는 안 남는다", "kind": "howto"},
                                        {"component": "api", "note": "6h 1,000건 (2026-10-12)", "kind": "structure"}]})

    def tearDown(self):
        K.KNOWLEDGE = self.orig
        self.t.cleanup()

    def test_edit_kind_text_and_remove(self):
        r = K.edit_note("c", {"component": "api", "match": "6h 1,000", "kind": "baseline"})
        self.assertTrue(r["saved"])
        self.assertEqual(r["after"]["kind"], "baseline")
        K.edit_note("c", {"component": "api", "match": "request-id", "note": "nginx 자체 id 만 남는다"})
        K.edit_note("c", {"component": "api", "match": "6h", "remove": True})
        notes = K.load_flows("c")["normal"]
        self.assertEqual(notes, [{"component": "api", "note": "nginx 자체 id 만 남는다", "kind": "howto"}])
        self.assertEqual(list(K.load_flows("c")["flows"]), ["f"])                      # 흐름은 그대로
        with self.assertRaises(LookupError):
            K.edit_note("c", {"component": "api", "match": "없는 글", "remove": True})
        with self.assertRaises(ValueError):
            K.edit_note("c", {"component": "api", "match": "nginx", "kind": "모름"})

    def test_dry_run_does_not_write(self):
        before = (Path(self.t.name) / "c" / "flows.yaml").read_text()
        r = K.edit_note("c", {"component": "api", "match": "request-id", "remove": True}, apply=False)
        self.assertFalse(r["saved"])
        self.assertEqual((Path(self.t.name) / "c" / "flows.yaml").read_text(), before)

    def test_recur_adds_component(self):
        f = K.write_incident("c", {"date": "2026-10-13", "components": ["api"], "logs": ["boom happened"], "cause": "x"})
        K.recur_incident("c", f.name, {"date": "2026-10-14", "note": "job 에서도", "components": ["job"]})
        meta, _ = K.parse_front(f.read_text())
        self.assertEqual(meta["components"], ["api", "job"])
        p = K.context("c", "job 이 이상해", incidents=K.load_incidents("c"))
        self.assertEqual([k["file"] for k in p["incidents"]], [f.name])


if __name__ == "__main__":
    unittest.main(verbosity=1)
