#!/usr/bin/env python3
"""koa/flowmap.py 의 판정 규칙 검사 — 클러스터 없이 합성 데이터로만.

  python3 koa/tests/test_flowmap.py
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import flowmap as F  # noqa: E402


def svc(ns, name, ip="10.96.0.10", typ="ClusterIP", selector=None, ext=None):
    spec = {"clusterIP": ip, "type": typ, "selector": selector or {"app": name}}
    if ext:
        spec = {"type": "ExternalName", "externalName": ext}
    return {"metadata": {"namespace": ns, "name": name}, "spec": spec}


def pod(ns, name, owner_kind, owner, labels, ip, containers, init=None, phase="Running"):
    return {"metadata": {"namespace": ns, "name": name, "labels": labels,
                         "ownerReferences": [{"kind": owner_kind, "name": owner}]},
            "spec": {"containers": containers, "initContainers": init or []},
            "status": {"phase": phase, "podIP": ip, "podIPs": [{"ip": ip}], "startTime": "2026-01-01T00:00:00Z"}}


class FakeSource:
    """triage.KubectlSource 와 같은 모양 — 메모리 안 객체만 돌려준다."""
    label = "fake"
    registered = True

    def __init__(self, objs, cms, logs):
        self.objs, self.cms, self.logs = objs, cms, logs
        self.kube = self

    def list(self, res):
        if res not in self.objs:
            raise RuntimeError("the server doesn't have a resource type")
        return self.objs[res]

    def json(self, *args):  # get configmaps <name> -n <ns>
        name, ns = args[2], args[4]
        if (ns, name) not in self.cms:
            raise RuntimeError("NotFound")
        return {"data": self.cms[(ns, name)]}

    def run(self, *args, check=True):  # logs <pod> -n <ns> ...
        text = self.logs.get(args[1], "")
        return SimpleNamespace(returncode=0, stdout=text, stderr="")


class Addresses(unittest.TestCase):
    def test_urls_and_hostports(self):
        self.assertEqual(F.addresses("amqp://u:p@rabbit:5672/%2F"), [("amqp", "rabbit", 5672)])
        self.assertEqual(F.addresses("http://api.shop.svc.cluster.local/v1"), [("http", "api.shop.svc.cluster.local", None)])
        self.assertEqual(F.addresses("kafka-0.kafka:9092,kafka-1.kafka:9092"),
                         [("", "kafka-0.kafka", 9092), ("", "kafka-1.kafka", 9092)])
        self.assertEqual(F.addresses("mongodb://a:27017,b:27017/db?replicaSet=rs0"),
                         [("mongodb", "a", 27017), ("mongodb", "b", 27017)])
        self.assertEqual(F.addresses("--upstream=http://auth:8080"), [("http", "auth", 8080)])
        self.assertEqual(F.addresses("proxy_pass http://backend:8000;"), [("http", "backend", 8000)])

    def test_noise(self):
        self.assertEqual(F.addresses("12:30:45 started"), [])
        self.assertEqual(F.addresses("version 1.2.3"), [])
        self.assertEqual(F.addresses("http://${HOST}:8080"), [])
        self.assertEqual(F.addresses("redis"), [])  # 포트 없는 낱말은 주소 키의 값일 때만
        self.assertEqual(F.addresses("redis", bare=True), [("", "redis", None)])

    def test_hostish(self):
        for n in ("REDIS_URL", "DB_HOST", "spring.redis.host", "bootstrap.servers", "proxy_pass", "Endpoint",
                  "ConnectionString", "upstream"):
            self.assertTrue(F.hostish(n), n)
        for n in ("LOG_LEVEL", "replicas", "timeout", "MAX_ATTEMPTS", "runbook_url", "homepage", "web.external-url",
                  "root_url", "advertised.listeners", "redirect_uri"):
            self.assertFalse(F.hostish(n), n)

    def test_errtext(self):
        self.assertEqual(F.errtext('{\n "error": "Resource configmaps/x not found",\n "status": "not_found"\n}'),
                         "Resource configmaps/x not found")

    def test_redact(self):
        self.assertNotIn("s3cret", F.redact("amqp://user:s3cret@mq:5672"))
        self.assertNotIn("s3cret", F.redact("password=s3cret host=db"))
        self.assertNotIn("abcdefghij", F.redact("Authorization: Bearer abcdefghij"))


class Resolve(unittest.TestCase):
    def setUp(self):
        self.R = F.Resolver([svc("shop", "api", "10.96.1.1"), svc("data", "pg", "10.96.2.2"),
                             svc("shop", "mq", "None"), svc("shop", "pay", ext="pay.example.com")], ["shop", "data"])
        self.R.pod_ip["10.244.0.5"] = "shop/Deployment/api"

    def test_forms(self):
        r = self.R.resolve
        self.assertEqual(r("api", "shop"), ("svc", "shop", "api"))
        self.assertIsNone(r("api", "data"))  # 다른 네임스페이스의 짧은 이름은 안 맞춘다
        self.assertEqual(r("pg.data", "shop"), ("svc", "data", "pg"))
        self.assertEqual(r("pg.data.svc.cluster.local", "shop"), ("svc", "data", "pg"))
        self.assertEqual(r("mq-0.mq", "shop"), ("svc", "shop", "mq"))  # StatefulSet 파드.헤드리스
        self.assertEqual(r("mq-0.mq.shop", "data"), ("svc", "shop", "mq"))
        self.assertEqual(r("ghost.data.svc.cluster.local", "shop"), ("unknown", "ghost.data.svc.cluster.local"))
        self.assertEqual(r("10.96.2.2", "shop"), ("svc", "data", "pg"))
        self.assertEqual(r("10.244.0.5", "data"), ("pod", "shop/Deployment/api"))
        self.assertEqual(r("api.stripe.com", "shop"), ("ext", "api.stripe.com"))
        self.assertIsNone(r("localhost", "shop"))
        self.assertIsNone(r("192.168.1.10", "shop"))  # 모르는 사설 IP
        self.assertIsNone(r("app.log", "shop"))  # 파일 이름처럼 생긴 것


class EndToEnd(unittest.TestCase):
    """입구 → web → (큐) → worker → 외부, web → DB(ConfigMap), 비밀 env, 로그 확인."""

    def build(self):
        web = pod("shop", "web-1", "ReplicaSet", "web-7d9", {"app": "web"}, "10.244.0.10", [{
            "name": "web", "image": "example/web:1",
            "env": [{"name": "QUEUE_URL", "value": "amqp://u:pw@mq:5672/"},
                    {"name": "DB_URL", "valueFrom": {"configMapKeyRef": {"name": "web-cfg", "key": "db.url"}}},
                    {"name": "CACHE_HOST", "valueFrom": {"secretKeyRef": {"name": "cache", "key": "host"}}}]}])
        worker = pod("shop", "worker-1", "ReplicaSet", "worker-5f6", {"app": "worker"}, "10.244.0.11", [{
            "name": "worker", "image": "example/worker:1",
            "env": [{"name": "BROKER", "value": "mq.shop.svc.cluster.local:5672"},
                    {"name": "CALLBACK_URL", "value": "http://web:8000/internal/done"}]}])
        mq = pod("shop", "mq-0", "StatefulSet", "mq", {"app": "mq"}, "10.244.0.12",
                 [{"name": "mq", "image": "library/rabbitmq:3"}])
        pg = pod("data", "pg-0", "StatefulSet", "pg", {"app": "pg"}, "10.244.0.13",
                 [{"name": "pg", "image": "library/postgres:16"}])
        ing = pod("edge", "ctrl-1", "ReplicaSet", "ingress-nginx-controller-6c8", {"app": "ctrl"}, "10.244.0.2",
                  [{"name": "controller", "image": "ingress-nginx/controller:v1"}])
        objs = {
            "pods": [web, worker, mq, pg, ing],
            "services": [svc("shop", "web", "10.96.0.20"), svc("shop", "mq", "10.96.0.21"),
                         svc("data", "pg", "10.96.0.22"), svc("edge", "ctrl", "10.96.0.2", typ="LoadBalancer")],
            "ingresses.networking.k8s.io": [{"metadata": {"namespace": "shop", "name": "web"},
                                             "spec": {"ingressClassName": "nginx", "rules": [{"host": "shop.example.com", "http": {
                                                 "paths": [{"path": "/", "backend": {"service": {"name": "web", "port": {"number": 8000}}}}]}}]}}],
            "ingressclasses.networking.k8s.io": [{"metadata": {"name": "nginx"}, "spec": {"controller": "k8s.io/ingress-nginx"}}],
            "namespaces": [{"metadata": {"name": n}} for n in ("shop", "data", "edge")],
        }
        cms = {("shop", "web-cfg"): {"db.url": "postgresql://pg.data:5432/app"}}
        logs = {  # 요청 ID 근거는 서로 다른 ID 2개 이상이 두 로그에 같이 나올 때만 (하나는 우연일 수 있다)
            "web-1": ("2026-01-01T00:00:01Z POST /orders request_id=ord-12345678 queued\n"
                      "2026-01-01T00:00:02Z POST /orders request_id=ord-87654321 queued\n"),
            "worker-1": ("2026-01-01T00:00:03Z start request_id=ord-12345678 url=https://partner.example.org/x\n"
                         "2026-01-01T00:00:04Z done request_id=ord-12345678\n"
                         "2026-01-01T00:00:05Z start request_id=ord-87654321\n"),
            "mq-0": "2026-01-01T00:00:00Z accepting AMQP connection 10.244.0.11:41000 -> 10.244.0.12:5672\n",
        }
        return FakeSource(objs, cms, logs)

    def test_flows(self):
        F.CALLS.update(list=0, get=0, logs=0)
        src = self.build()
        a = SimpleNamespace(ns=None, skip_ns="kube-system", no_logs=False, tail=100, pods=1, max_logs=50, cluster="t")
        limits = []
        res = F.run(src, a, limits)
        asm = F.assemble(res)
        E = res["E"].e
        web, worker = "shop/Deployment/web", "shop/Deployment/worker"
        mq, pg, ctrl = "shop/StatefulSet/mq", "data/StatefulSet/pg", "edge/Deployment/ingress-nginx-controller"
        self.assertIn((ctrl, web), E)  # 입구: IngressClass 컨트롤러 이름으로 컨트롤러 워크로드를 찾는다
        self.assertEqual(F.verdict(E[(web, mq)]), "설정")
        self.assertIn("amqp", E[(web, mq)]["protos"])
        self.assertEqual(F.verdict(E[(worker, mq)]), "설정+로그")  # mq 로그의 worker 파드 IP
        self.assertEqual(asm["role"][(web, mq)], "produce")
        self.assertEqual(asm["role"][(worker, mq)], "consume")  # 호출을 받지 않는 쪽 = 꺼내는 쪽
        self.assertIn((web, pg), E)  # ConfigMap 값
        self.assertIn(("shop/Deployment/worker", "shop/Deployment/web"), E)  # CALLBACK_URL
        self.assertTrue(any(x["env"] == "CACHE_HOST" for x in res["notes"]["secret_env"]))
        self.assertTrue(any(h == "partner.example.org" for (_, h) in res["ext_logs"]))  # 흐름이 아니라 표로
        self.assertNotIn((worker, "ext:partner.example.org"), E)
        self.assertTrue(any(ev["type"] == "log-id" for ev in E[(web, worker)]["evidence"]))  # 같은 요청 ID, web 이 먼저
        self.assertEqual(F.verdict(E[(web, worker)]), "요청ID")  # 큐를 거친 간접 순서 — 흐름의 직접 연결로 쓰지 않는다
        f = next(f for f in asm["flows"] if f["path"][:4] == [ctrl, web, mq, worker])
        self.assertIn((web, worker), f["ids_confirm"])
        chains = [" → ".join(f["nodes"]) for f in asm["flows"]]
        self.assertTrue(any(c.startswith("ingress-nginx-controller → web → mq → worker") for c in chains), chains)
        self.assertTrue(any(c.startswith("ingress-nginx-controller → web → pg") for c in chains), chains)
        self.assertEqual(F.CALLS["logs"], 5)
        self.assertEqual(F.CALLS["get"], 1)
        for e in E.values():  # 비밀번호가 근거에 남지 않는다
            for ev in e["evidence"]:
                self.assertNotIn("pw@", str(ev.get("value", "")))


if __name__ == "__main__":
    unittest.main(verbosity=1)
