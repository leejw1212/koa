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
