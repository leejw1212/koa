#!/usr/bin/env python3
"""kind-lab 장애 주입기 — KOA 와 일반 프로필의 분석을 비교하기 위한 '문제 출제기' (lab 전용).

  python3 lab/chaos/chaos.py inject [--scenario <이름>] [--delay-max 분]   # 무작위(또는 지정) 장애 주입
  python3 lab/chaos/chaos.py status                                        # 지금 걸린 장애가 있는지만 (답은 안 보여 줌)
  python3 lab/chaos/chaos.py reveal                                        # 정답 보기 (분석이 끝난 뒤)
  python3 lab/chaos/chaos.py recover                                       # 원래대로

규칙
- 관리자 컨텍스트(--kubeconfig ~/.kube/config --context kind-lab)로만 쓴다. 분석 쪽(에이전트)은 읽기 전용 SA.
- 주입할 때 어떤 장애인지 출력하지 않는다. 정답은 ~/.koa-chaos/answer.json 에 base64 로 둔다
  (에이전트가 grep 으로 우연히 보지 않게. 보안 장치는 아니다).
- 증상(사용자가 말하는 말)만 출력한다 — 그걸 그대로 두 에이전트에 질문으로 준다.
- 장애는 사용자 트래픽이 있어야 증거가 쌓인다: lab/chaos/loadgen.yaml 이 떠 있는지 먼저 확인한다.
- 시나리오 객체 이름에 chaos/test 같은 단어를 넣지 않는다 — 이름만 보고 답을 맞히지 않게.
"""
import argparse
import base64
import datetime as dt
import json
import os
import random
import subprocess
import sys
import time

KUBE = ["kubectl", "--kubeconfig", os.path.expanduser("~/.kube/config"), "--context", "kind-lab"]
STATE = os.path.expanduser("~/.koa-chaos")
ANSWER = os.path.join(STATE, "answer.json")


def kubectl(*args, data=None, check=True):
    p = subprocess.run(KUBE + list(args), input=data, capture_output=True, text=True)
    if check and p.returncode:
        sys.exit("kubectl %s 실패: %s" % (" ".join(args[:3]), p.stderr.strip()[:300]))
    return p.stdout


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ─────────────────────────────────────────────────────────────
# 시나리오. 하나씩 늘려 간다. 각 시나리오는
#   symptom   : 사용자가 하는 말 (에이전트에게 주는 질문)
#   answer    : 정답 (원인, 어디서 보이는지)
#   keywords  : 채점용 — 보고서에 이 말 중 하나라도 있으면 '원인 지목'으로 본다 (그룹마다 하나 이상)
#   inject / recover
# ─────────────────────────────────────────────────────────────

def _np_callback_blocked():
    """누군가 'web 은 인그레스로만 들어오게' 막는 NetworkPolicy 를 넣었다.
    워커 → web 의 /internal/result 콜백까지 막혀 작업은 끝나는데 결과가 저장되지 않는다.
    파드는 전부 Running/Ready — 파드 상태만 봐서는 안 보인다."""
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "linkcard-web-allow-ingress", "namespace": "linkcard",
                     "labels": {"app": "linkcard-web"}},
        "spec": {
            "podSelector": {"matchLabels": {"app": "linkcard-web"}},
            "policyTypes": ["Ingress"],
            "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {
                "kubernetes.io/metadata.name": "ingress-nginx"}}}],
                "ports": [{"protocol": "TCP", "port": 8000}]}],
        },
    }


def _np_site_blocked():
    """누군가 slowsite 를 '인그레스로만 들어오게' 막았다. 워커가 대상 페이지를 가져오다 타임아웃 →
    5초·15초 뒤 재시도 → dead-letter. 지식의 정상 패턴('대상 사이트 탓의 실패는 앱 장애가 아니다')이
    함정이다 — 503/404 가 아니라 클러스터 안 연결 타임아웃이다."""
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "slowsite-allow-ingress", "namespace": "linkcard", "labels": {"app": "slowsite"}},
        "spec": {
            "podSelector": {"matchLabels": {"app": "slowsite"}},
            "policyTypes": ["Ingress"],
            "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {
                "kubernetes.io/metadata.name": "ingress-nginx"}}}]}],
        },
    }


def _np_worker_egress():
    """워커의 나가는 트래픽을 web·slowsite 로만 허용 → DNS(kube-dns)가 막혀 'Temporary failure in name resolution'.
    지식의 정상 패턴('노드 재시작 직후 이름을 못 찾아 재연결하다 정상')과 같은 문구라 정상으로 넘기기 쉽다.
    재시작이 없고, 계속되며, 워커만 그렇다(web 은 이름을 잘 찾는다)."""
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "linkcard-worker-egress", "namespace": "linkcard", "labels": {"app": "linkcard-worker"}},
        "spec": {
            "podSelector": {"matchLabels": {"app": "linkcard-worker"}},
            "policyTypes": ["Egress"],
            "egress": [{"to": [{"podSelector": {"matchLabels": {"app": "linkcard-web"}}},
                               {"podSelector": {"matchLabels": {"app": "slowsite"}}}]}],
        },
    }


def _redis(*cmd):
    return kubectl("-n", "linkcard", "exec", "redis-0", "--", "redis-cli", *cmd)


def _rmq(*cmd):
    return kubectl("-n", "linkcard", "exec", "rabbitmq-0", "--", "rabbitmqctl", *cmd)


SCENARIOS = {
    "callback-blocked": {
        "symptom": "링크카드에서 '만들기'를 누르면 접수는 되는데 '결과를 기다리는 중…'에서 끝나지 않아요. 원인 찾아줘.",
        "answer": {
            "cause": "linkcard 네임스페이스에 NetworkPolicy linkcard-web-allow-ingress 가 생겨 linkcard-web 으로 들어오는 "
                     "트래픽을 ingress-nginx 네임스페이스에서만 허용 → 워커가 결과를 돌려주는 "
                     "POST http://linkcard-web:8000/internal/result 가 막힘 (타임아웃).",
            "where": "worker 로그 job.report_failed (timed out) 직후 job.done; web 로그에 결과 수신 없음; "
                     "kubectl get networkpolicy -n linkcard; 파드·이벤트는 정상",
            "fix": "NetworkPolicy 에 app=linkcard-worker 파드에서 오는 8000 허용 추가 (또는 정책 제거)",
        },
        "keywords": [["networkpolicy", "network policy", "네트워크 정책", "네트워크폴리시", "linkcard-web-allow-ingress"],
                     ["internal/result", "콜백", "callback", "report_failed", "결과를 돌려", "결과 전달"]],
        "inject": lambda: kubectl("apply", "-f", "-", data=json.dumps(_np_callback_blocked())),
        "recover": lambda: kubectl("-n", "linkcard", "delete", "networkpolicy", "linkcard-web-allow-ingress",
                                   "--ignore-not-found"),
        "check": lambda: "linkcard-web-allow-ingress" in kubectl("-n", "linkcard", "get", "networkpolicy", "-o", "name"),
    },
    "site-blocked": {
        "symptom": "링크카드에서 카드를 만들면 한참 기다리다가 전부 '실패'로 끝나요. 원인 찾아줘.",
        "answer": {
            "cause": "linkcard 네임스페이스에 NetworkPolicy slowsite-allow-ingress 가 생겨 slowsite 로 들어오는 트래픽을 "
                     "ingress-nginx 네임스페이스에서만 허용 → 워커가 대상 페이지(http://slowsite:8080)를 가져오다 타임아웃, "
                     "재시도 후 dead-letter.",
            "where": "worker 로그 job.retry / job.dead_lettered (timed out); slowsite 로그에 요청이 안 옴; "
                     "kubectl get networkpolicy -n linkcard; 파드·이벤트는 정상",
            "fix": "NetworkPolicy 에 app=linkcard-worker 에서 오는 8080 허용 추가 (또는 정책 제거)",
        },
        "keywords": [["networkpolicy", "network policy", "네트워크 정책", "네트워크폴리시", "slowsite-allow-ingress"],
                     ["slowsite", "대상 사이트", "대상 페이지"]],
        "inject": lambda: kubectl("apply", "-f", "-", data=json.dumps(_np_site_blocked())),
        "recover": lambda: kubectl("-n", "linkcard", "delete", "networkpolicy", "slowsite-allow-ingress",
                                   "--ignore-not-found"),
        "check": lambda: "slowsite-allow-ingress" in kubectl("-n", "linkcard", "get", "networkpolicy", "-o", "name"),
    },
    # 어려운 것: k8s 객체 변경 없음(실행 중 설정만), 파드·이벤트 정상, 증상 로그(job.report_failed)가
    # 콜백 차단 과거 이슈와 같다 → 지식이 오답(NetworkPolicy)으로 끌 수 있다.
    "redis-oom": {
        "symptom": "링크카드에서 카드 만들기를 누르면 바로 오류가 나고, 가끔 접수돼도 결과가 안 나와요. 원인 찾아줘.",
        "answer": {
            "cause": "redis 의 maxmemory 가 실행 중 2mb 로 낮아졌고(정책 noeviction) 이미 그 이상을 쓰고 있어 쓰기가 전부 거부됨 "
                     "(OOM command not allowed when used memory > 'maxmemory'). web 의 POST /api/cards 와 "
                     "/internal/result 가 redis 에 쓰다 500.",
            "where": "web 로그 redis.exceptions.OutOfMemoryError, status 500; worker 로그 job.report_failed HTTP Error 500 "
                     "(timeout 아님); NetworkPolicy 없음; redis 파드 정상·재시작 없음. 설정 변경은 k8s 에 흔적이 없다",
            "fix": "redis maxmemory 원복 (또는 재시작) — 운영자 조치",
        },
        "keywords": [["redis", "레디스"], ["maxmemory", "outofmemory", "oom", "메모리"]],
        "inject": lambda: _redis("CONFIG", "SET", "maxmemory", "2mb"),
        "recover": lambda: _redis("CONFIG", "SET", "maxmemory", "0"),
        "check": lambda: _redis("CONFIG", "GET", "maxmemory").split()[-1] != "0",
    },
    # 정상 패턴과 같은 겉모습: 'Temporary failure in name resolution' 은 지식에 '재시작 직후면 정상' 으로 적혀 있다.
    "worker-dns-blocked": {
        "symptom": "링크카드 카드가 한참 걸리다가 실패하거나 결과가 안 나와요. 아까부터 계속 그래요. 원인 찾아줘.",
        "answer": {
            "cause": "linkcard 네임스페이스에 NetworkPolicy linkcard-worker-egress 가 생겨 linkcard-worker 의 나가는 트래픽을 "
                     "linkcard-web·slowsite 파드로만 허용 → DNS(kube-dns 53) 가 막혀 워커가 slowsite·linkcard-web 이름을 못 찾음 "
                     "(Temporary failure in name resolution) → 대상 페이지 재시도 후 실패, 결과 콜백 실패.",
            "where": "worker 로그 job.retry_scheduled / job.report_failed 'Temporary failure in name resolution' 이 재시작 없이 계속; "
                     "web 은 정상; kubectl get networkpolicy -n linkcard (Egress). 정상 패턴(재시작 직후 DNS)과 구분: 재시작 없음·지속",
            "fix": "NetworkPolicy 에 kube-system/kube-dns 53 UDP/TCP 와 rabbitmq 허용 추가 (또는 정책 제거)",
        },
        "keywords": [["networkpolicy", "network policy", "네트워크 정책", "linkcard-worker-egress"],
                     ["dns", "name resolution", "이름 해석", "53", "egress"]],
        "inject": lambda: kubectl("apply", "-f", "-", data=json.dumps(_np_worker_egress())),
        "recover": lambda: kubectl("-n", "linkcard", "delete", "networkpolicy", "linkcard-worker-egress", "--ignore-not-found"),
        "check": lambda: "linkcard-worker-egress" in kubectl("-n", "linkcard", "get", "networkpolicy", "-o", "name"),
    },
    # 지식에 없는 종류: k8s 객체 변경 없음(실행 중 브로커 설정만), 파드·이벤트·재시작 정상, NetworkPolicy 없음.
    # 과거 이슈(네트워크 정책 3건, redis OOM)와 겉모습이 겹친다(오류·타임아웃·결과 안 나옴).
    "rmq-memory-alarm": {
        "symptom": "링크카드에서 만들기를 누르면 한참 돌다가 오류가 나요. 아까부터 계속 그래요. 원인 찾아줘.",
        "answer": {
            "cause": "rabbitmq 의 메모리 경보 기준(vm_memory_high_watermark)이 실행 중 50MB 로 낮아져 이미 쓰는 메모리보다 작다 → "
                     "memory alarm 이 켜져 브로커가 발행(publish)하는 연결을 전부 막음(connection.blocked). "
                     "web 의 POST /api/cards 가 큐에 넣다 멈춤 → gunicorn 60초 WORKER TIMEOUT → 인그레스 502/504. "
                     "워커의 재시도 발행도 같은 연결에서 막힌다.",
            "where": "rabbitmq 로그 'memory resource limit alarm set' / 'Publishers will be blocked'; "
                     "rabbitmqctl status 의 Alarms; web 로그에 job.queued 가 끊기고 gunicorn WORKER TIMEOUT; "
                     "ingress 로그 502/504 ~60s. NetworkPolicy 없음, redis 정상, 파드 재시작 없음. k8s 에 흔적 없음",
            "fix": "watermark 원복(rabbitmqctl set_vm_memory_high_watermark 0.6) 또는 브로커 재시작 — 운영자 조치",
        },
        "keywords": [["rabbitmq", "래빗", "rmq", "브로커"],
                     ["memory alarm", "메모리 경보", "memory resource limit", "watermark", "blocked", "막", "alarm"]],
        "inject": lambda: _rmq("set_vm_memory_high_watermark", "absolute", "50MB"),
        "recover": lambda: _rmq("set_vm_memory_high_watermark", "0.6"),
        "check": lambda: "memory" in _rmq("status").split("Alarms", 1)[-1].split("\n\n", 2)[1].lower(),
    },
}


def save_answer(obj):
    os.makedirs(STATE, exist_ok=True)
    with open(ANSWER, "w") as f:
        f.write(base64.b64encode(json.dumps(obj, ensure_ascii=False).encode()).decode())
    os.chmod(ANSWER, 0o600)


def load_answer():
    if not os.path.exists(ANSWER):
        return None
    return json.loads(base64.b64decode(open(ANSWER).read()).decode())


def loadgen_running():
    out = kubectl("-n", "traffic", "get", "deploy", "loadgen", "-o", "jsonpath={.status.readyReplicas}", check=False)
    return out.strip() == "1"


def cmd_inject(a):
    if load_answer():
        sys.exit("이미 장애가 걸려 있다. 먼저 recover 하거나 reveal 로 확인.")
    if not loadgen_running():
        sys.exit("traffic/loadgen 이 없다. 먼저: kubectl --kubeconfig ~/.kube/config --context kind-lab apply -f lab/chaos/loadgen.yaml")
    name = a.scenario or random.choice(sorted(SCENARIOS))
    if name not in SCENARIOS:
        sys.exit("모르는 시나리오: %s (있는 것: %s)" % (name, ", ".join(SCENARIOS)))
    sc = SCENARIOS[name]
    delay = random.randint(0, a.delay_max * 60) if a.delay_max else 0
    if delay:
        print("%d초 안의 무작위 시점에 주입한다 (기다리는 중)..." % (a.delay_max * 60))
        time.sleep(delay)
    sc["inject"]()
    t = now()
    save_answer({"scenario": name, "injected_at": t, "symptom": sc["symptom"], **sc["answer"],
                 "keywords": sc["keywords"]})
    print("주입 완료 %s (UTC). 증거가 쌓이게 %d분쯤 기다린 뒤 아래 질문을 두 에이전트에 그대로 준다." % (t, a.settle))
    print()
    print("질문: " + sc["symptom"])


def cmd_status(a):
    ans = load_answer()
    if not ans:
        print("걸린 장애 없음")
        return
    live = SCENARIOS[ans["scenario"]]["check"]()
    print("장애 걸림 (주입 %s UTC, 클러스터에 %s)" % (ans["injected_at"], "남아 있음" if live else "없음 — 누가 고쳤다"))
    print("질문: " + ans["symptom"])


def cmd_reveal(a):
    ans = load_answer()
    print(json.dumps(ans, ensure_ascii=False, indent=2) if ans else "걸린 장애 없음")


def cmd_recover(a):
    ans = load_answer()
    names = [ans["scenario"]] if ans else list(SCENARIOS)
    for n in names:
        SCENARIOS[n]["recover"]()
    if ans:
        os.rename(ANSWER, os.path.join(STATE, "answer-%s.json" % ans["injected_at"].replace(":", "")))
    print("복구: %s" % ", ".join(names))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = p.add_subparsers(dest="cmd", required=True)
    i = s.add_parser("inject")
    i.add_argument("--scenario")
    i.add_argument("--delay-max", type=int, default=0, help="0~N분 사이 무작위 시점에 주입")
    i.add_argument("--settle", type=int, default=3)
    s.add_parser("status")
    s.add_parser("reveal")
    s.add_parser("recover")
    a = p.parse_args()
    {"inject": cmd_inject, "status": cmd_status, "reveal": cmd_reveal, "recover": cmd_recover}[a.cmd](a)


if __name__ == "__main__":
    main()
