#!/usr/bin/env python3
"""트리아지 테스트용 장애 장면을 API 서버에 직접 써 넣는다 (lab 전용, 관리자 컨텍스트).

컨트롤러·kubelet 이 없는 "API 서버만 있는" 환경(etcd + kube-apiserver) 전용이다. kind 처럼 컨트롤러가 돌면
진짜 파드가 생기고 가짜 노드가 정리돼 장면이 바뀐다. 그래서
노드·파드·RS·이벤트·EndpointSlice 의 status 까지 손으로 만든다. 실제 장애를 일으키지 않고
"장애가 났을 때 API 서버에 남는 모습"을 재현해 koa/triage.py 가 무엇을 잡는지 확인하는 용도다.

  python3 lab/triage-fixtures.py --context <lab-관리자-컨텍스트>          # 장면 생성
  python3 lab/triage-fixtures.py --context <lab-관리자-컨텍스트> --clean  # 지우기 (namespace 컨트롤러가 없으면 etcd 를 새로)

장면 (시각은 실행 시점 기준)
  (RS·ConfigMap·노드 생성 시각은 API 서버가 실제 시각으로 찍는다 → 장면 기준 SHIFT=20분 전 변경으로 보인다)
  A shop/order-api      20분 전 롤아웃(order:1.2→1.3) 뒤 3/3 CrashLoopBackOff, 가용 0, Service 엔드포인트 0
  B shop/payment-api    OOMKilled 재시작 2건 (12·13분 전), 20분 전 ConfigMap payment-config 수정
  C batch/report        Pending — 메모리 부족으로 스케줄 안 됨
  D lab-worker-2        10분 전 NotReady, 그 위 node-exporter 파드
  E 40분 전 lab-worker-1 의 워크로드 4개가 같은 분에 exit 255 로 재시작 (호스트 재시작 모양)
  F web/legacy          ImagePullBackOff
  G web/frontend 등     정상 (잡음)
"""
import argparse
import datetime as dt
import json
import subprocess
import sys

# API 서버가 creationTimestamp·managedFields 시각을 실제 생성 시각으로 찍으므로, 장면 기준 시각(NOW)을
# 실제보다 SHIFT 분 뒤로 잡는다. 그러면 지금 만든 RS·ConfigMap 이 장면에서는 "SHIFT 분 전 변경"이 된다.
# 트리아지는 출력되는 --at 으로 돌린다.
SHIFT = 20
NOW = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=SHIFT)).replace(microsecond=0)
NS = ["shop", "batch", "web", "monitoring", "infra"]
NODES = ["lab-control-plane", "lab-worker-1", "lab-worker-2"]


def t(minutes_ago):
    return (NOW - dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class K:
    def __init__(self, ctx):
        self.base = ["kubectl", "--context", ctx]

    def run(self, *a, data=None, check=True):
        p = subprocess.run(self.base + list(a), input=json.dumps(data) if data else None, capture_output=True, text=True)
        if check and p.returncode:
            raise SystemExit("kubectl %s: %s" % (" ".join(a[:4]), p.stderr.strip()[:400]))
        return p.stdout

    def create(self, obj, status=None):
        out = json.loads(self.run("create", "-o", "json", "-f", "-", data=obj))
        if status is not None:
            ns = ["-n", obj["metadata"]["namespace"]] if obj["metadata"].get("namespace") else []
            self.run("patch", obj["kind"].lower() + ("." + obj["apiVersion"].split("/")[0] if "/" in obj["apiVersion"] else ""),
                     obj["metadata"]["name"], *ns, "--subresource=status", "--type=merge", "-p", json.dumps({"status": status}))
        return out


def owner(o):
    return [{"apiVersion": o["apiVersion"], "kind": o["kind"], "name": o["metadata"]["name"],
             "uid": o["metadata"]["uid"], "controller": True}]


def node(k, name, ready=True, since=600):
    conds = [{"type": "Ready", "status": "True" if ready else "Unknown",
              "reason": "KubeletReady" if ready else "NodeStatusUnknown",
              "message": "kubelet is posting ready status" if ready else "Kubelet stopped posting node status.",
              "lastHeartbeatTime": t(0 if ready else 11), "lastTransitionTime": t(since)}]
    for c in ("MemoryPressure", "DiskPressure", "PIDPressure"):
        conds.append({"type": c, "status": "False", "reason": "KubeletHas", "message": "ok",
                      "lastHeartbeatTime": t(0), "lastTransitionTime": t(3000)})
    k.create({"apiVersion": "v1", "kind": "Node", "metadata": {"name": name, "labels": {"kubernetes.io/hostname": name}}},
             {"conditions": conds, "capacity": {"cpu": "4", "memory": "8Gi", "pods": "110"},
              "allocatable": {"cpu": "4", "memory": "8Gi", "pods": "110"}})


def deployment(k, ns, name, image, replicas, available, rev=1, rev_minutes=3000, prev_image=None, labels=None):
    labels = labels or {"app": name}
    d = k.create({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": ns},
                  "spec": {"replicas": replicas, "selector": {"matchLabels": labels},
                           "template": {"metadata": {"labels": labels},
                                        "spec": {"containers": [{"name": name, "image": image}]}}}},
                 {"replicas": replicas, "availableReplicas": available, "readyReplicas": available,
                  "updatedReplicas": replicas, "observedGeneration": 1})
    rss = []
    revs = [(1, prev_image or image, 3000)] + ([(rev, image, rev_minutes)] if rev > 1 else [])
    for r, img, _ in revs:
        h = "h%d%s" % (r, name[:3])
        rs = k.create({"apiVersion": "apps/v1", "kind": "ReplicaSet",
                       "metadata": {"name": "%s-%s" % (name, h), "namespace": ns, "ownerReferences": owner(d),
                                    "annotations": {"deployment.kubernetes.io/revision": str(r)}},
                       "spec": {"replicas": replicas if r == rev else 0,
                                "selector": {"matchLabels": dict(labels, hash=h)},
                                "template": {"metadata": {"labels": dict(labels, hash=h)},
                                             "spec": {"containers": [{"name": name, "image": img}]}}}})
        rss.append(rs)
    return d, rss[-1]


def pod(k, ns, name, rs, image, node_name, ready=True, waiting=None, last=None, phase="Running", restarts=0,
        conditions=None, labels=None, kind_owner=True):
    cname = rs["metadata"]["name"].rsplit("-", 1)[0] if rs else name.rsplit("-", 1)[0]
    meta = {"name": name, "namespace": ns, "labels": labels or {"app": cname}}
    if rs and kind_owner:
        meta["ownerReferences"] = owner(rs)
    spec = {"containers": [{"name": cname, "image": image}]}
    if node_name:
        spec["nodeName"] = node_name
    k.create({"apiVersion": "v1", "kind": "Pod", "metadata": meta, "spec": spec})
    cs = {"name": cname, "image": image, "imageID": "", "ready": ready, "restartCount": restarts,
          "started": ready, "state": {"waiting": waiting} if waiting else {"running": {"startedAt": t(3)}},
          "lastState": {"terminated": last} if last else {}}
    st = {"phase": phase, "startTime": t(3000)}
    if conditions is not None:
        st["conditions"] = conditions
    else:
        st["conditions"] = [{"type": "PodScheduled", "status": "True", "lastTransitionTime": t(3000)},
                            {"type": "Ready", "status": "True" if ready else "False",
                             "reason": None if ready else "ContainersNotReady",
                             "lastTransitionTime": t(3000 if ready else 18)}]
    if phase != "Pending":
        st["containerStatuses"] = [cs]
    k.run("patch", "pod", name, "-n", ns, "--subresource=status", "--type=merge", "-p", json.dumps({"status": st}))


def event(k, ns, kind, name, reason, msg, first, last, count=1, typ="Warning"):
    k.create({"apiVersion": "v1", "kind": "Event",
              "metadata": {"name": "%s.%s.%d" % (name, reason.lower(), first), "namespace": ns or "default"},
              "involvedObject": {"kind": kind, "name": name, "namespace": ns or None},
              "reason": reason, "message": msg, "type": typ, "count": count,
              "firstTimestamp": t(first), "lastTimestamp": t(last), "source": {"component": "kubelet"}})


def svc(k, ns, name, sel, ready_ips):
    k.create({"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": ns},
              "spec": {"selector": sel, "ports": [{"port": 80, "targetPort": 8080}]}})
    k.create({"apiVersion": "discovery.k8s.io/v1", "kind": "EndpointSlice",
              "metadata": {"name": name + "-abc", "namespace": ns, "labels": {"kubernetes.io/service-name": name}},
              "addressType": "IPv4", "ports": [{"port": 8080}],
              "endpoints": [{"addresses": [ip], "conditions": {"ready": r}} for ip, r in ready_ips]})


def build(k):
    for ns in NS:
        k.run("create", "namespace", ns)
    for ns in NS + ["default"]:
        k.run("create", "serviceaccount", "default", "-n", ns, check=False)
    node(k, "lab-control-plane")
    node(k, "lab-worker-1")
    node(k, "lab-worker-2", ready=False, since=10)

    # A: 롤아웃 후 CrashLoop
    d, rs = deployment(k, "shop", "order-api", "registry.local/shop/order:1.3", 3, 0, rev=2, rev_minutes=20,
                       prev_image="registry.local/shop/order:1.2")
    for i in range(3):
        pod(k, "shop", "order-api-h2ord-%d" % i, rs, "registry.local/shop/order:1.3", "lab-worker-1", ready=False,
            waiting={"reason": "CrashLoopBackOff", "message": "back-off 5m0s restarting failed container"},
            last={"exitCode": 1, "reason": "Error", "startedAt": t(3), "finishedAt": t(2)}, restarts=8)
    svc(k, "shop", "order-api", {"app": "order-api"}, [("10.0.1.%d" % i, False) for i in range(3)])
    event(k, "shop", "Pod", "order-api-h2ord-0", "BackOff", "Back-off restarting failed container order-api", 19, 1, 40)

    # B: OOMKilled + ConfigMap 수정
    d, rs = deployment(k, "shop", "payment-api", "registry.local/shop/payment:2.0", 2, 2)
    for i in range(2):
        pod(k, "shop", "payment-api-h1pay-%d" % i, rs, "registry.local/shop/payment:2.0", "lab-worker-1",
            last={"exitCode": 137, "reason": "OOMKilled", "startedAt": t(28), "finishedAt": t(12 + i)}, restarts=1)
    k.create({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "payment-config", "namespace": "shop"},
              "data": {"POOL_SIZE": "64"}})
    svc(k, "shop", "payment-api", {"app": "payment-api"}, [("10.0.1.10", True), ("10.0.1.11", True)])

    # C: Pending
    d, rs = deployment(k, "batch", "report", "registry.local/batch/report:5", 1, 0)
    pod(k, "batch", "report-h1rep-0", rs, "registry.local/batch/report:5", None, ready=False, phase="Pending",
        conditions=[{"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "lastTransitionTime": t(25),
                     "message": "0/3 nodes are available: 1 node(s) had untolerated taint, 2 Insufficient memory."}])
    event(k, "batch", "Pod", "report-h1rep-0", "FailedScheduling",
          "0/3 nodes are available: 1 node(s) had untolerated taint, 2 Insufficient memory.", 25, 2, 12)

    # D: 노드 NotReady + 그 위 DaemonSet
    ds = k.create({"apiVersion": "apps/v1", "kind": "DaemonSet", "metadata": {"name": "node-exporter", "namespace": "monitoring"},
                   "spec": {"selector": {"matchLabels": {"app": "node-exporter"}},
                            "template": {"metadata": {"labels": {"app": "node-exporter"}},
                                         "spec": {"containers": [{"name": "node-exporter", "image": "prom/node-exporter:v1.9.0"}]}}}},
                  {"desiredNumberScheduled": 3, "numberUnavailable": 1, "numberReady": 2, "currentNumberScheduled": 3,
                   "numberMisscheduled": 0})
    for i, n in enumerate(NODES):
        pod(k, "monitoring", "node-exporter-%d" % i, ds, "prom/node-exporter:v1.9.0", n, ready=(n != "lab-worker-2"))
    event(k, "", "Node", "lab-worker-2", "NodeNotReady", "Node lab-worker-2 status is now: NodeNotReady", 10, 10)

    # E: 40분 전 같은 분에 lab-worker-1 워크로드 4개 exit 255 재시작
    for name, ns in [("cache", "infra"), ("queue", "infra"), ("gateway", "infra"), ("search", "web")]:
        d, rs = deployment(k, ns, name, "registry.local/%s:1" % name, 1, 1)
        pod(k, ns, "%s-h1%s-0" % (name, name[:3]), rs, "registry.local/%s:1" % name, "lab-worker-1",
            last={"exitCode": 255, "reason": "Unknown", "startedAt": t(2000), "finishedAt": t(40)}, restarts=1)

    # F: ImagePullBackOff
    d, rs = deployment(k, "web", "legacy", "registry.local/web/legacy:9.9", 1, 0)
    pod(k, "web", "legacy-h1leg-0", rs, "registry.local/web/legacy:9.9", "lab-worker-1", ready=False,
        waiting={"reason": "ImagePullBackOff", "message": "Back-off pulling image \"registry.local/web/legacy:9.9\""})

    # G: 정상 잡음
    d, rs = deployment(k, "web", "frontend", "registry.local/web/frontend:3", 3, 3)
    for i in range(3):
        pod(k, "web", "frontend-h1fro-%d" % i, rs, "registry.local/web/frontend:3", "lab-worker-1")
    svc(k, "web", "frontend", {"app": "frontend"}, [("10.0.2.%d" % i, True) for i in range(3)])
    event(k, "web", "Pod", "frontend-h1fro-0", "Pulled", "Successfully pulled image", 3000, 3000, typ="Normal")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--context", required=True, help="관리자 컨텍스트 (lab 전용)")
    ap.add_argument("--clean", action="store_true")
    a = ap.parse_args()
    if "readonly" in a.context:
        sys.exit("읽기 전용 컨텍스트로는 만들 수 없다. lab 관리자 컨텍스트를 준다.")
    k = K(a.context)
    # 컨트롤러가 도는 클러스터(kube-system 에 파드가 있음)에는 넣지 않는다
    if k.run("get", "pods", "-n", "kube-system", "-o", "name", check=False).strip():
        sys.exit("kube-system 에 파드가 있다 = 컨트롤러가 도는 클러스터. 이 fixture 는 API 서버 전용 lab 에만 쓴다.")
    for ns in NS:
        k.run("delete", "namespace", ns, "--wait=false", check=False)
    for n in NODES:
        k.run("delete", "node", n, check=False)
    k.run("delete", "events", "-n", "default", "--all", check=False)
    if a.clean:
        return
    build(k)
    print("장면 생성 완료. 장면 기준 시각 %s → python3 koa/triage.py --at %s"
          % (NOW.strftime("%H:%M:%SZ"), NOW.strftime("%Y-%m-%dT%H:%M:%SZ")))


if __name__ == "__main__":
    main()
