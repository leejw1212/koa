"""자유 형식 글 → 클러스터 지식 구조. 웹 화면(koa/web.py)과 knowledge.py 가 쓴다.

사람은 architecture.md 에 말하듯 쓴다. 여기서 분석에 필요한 것만 뽑아 flows.yaml 을 만든다.

  화살표 줄       사용자 → ingress-nginx → gateway → order-api → orders-db      흐름 (→ -> => ⇒)
                  order-api → rabbitmq → order-worker                            큐를 지나면 produce/consume
  이름(별명)      rabbitmq(rmq, 래빗)                                            괄호 안은 별명
  이름: …         orders-db: 주문 DB, mysql, 클러스터 밖                          콜론 뒤 쉼표 목록은 별명, '밖/외부' 는 external
  워크로드        order-worker 는 shop/Deployment/order-worker                    ns/Kind/name 이 같은 줄에 있으면 그 워크로드
  로그 문구       orders-db 가 죽으면 "Too many connections" 가 찍힌다           따옴표 안 문구 → 그 구성요소의 log_signatures
  정상 패턴       매일 02:00 정산 배치 때 order-worker 큐가 쌓였다 빠진다         정상·평소·매일·배치 + 구성요소 이름 → normal

규칙에 안 맞는 글은 그대로 두고(분석 때 참고로 읽는다) 뽑지 않는다. 클러스터는 건드리지 않는다.
"""
import datetime as dt
import re

ARROW = re.compile(r"\s*(?:→|->|=>|⇒|──>|-->)\s*")
WORKLOAD = re.compile(r"\b([a-z0-9][a-z0-9-]*)/(Deployment|StatefulSet|DaemonSet|deployment|statefulset|daemonset)/([a-z0-9][a-z0-9.-]*)")
QUOTED = re.compile(r"[\"“”'‘’`]([^\"“”'‘’`]{4,120})[\"“”'‘’`]")
SKIP_NODES = {"사용자", "유저", "user", "users", "client", "클라이언트", "브라우저", "browser", "외부", "인터넷", "internet", "앱", "모바일"}
QUEUE_WORDS = re.compile(r"rabbit|rmq|kafka|sqs|nats|pulsar|queue|큐|^mq$|-mq$|activemq|redis-stream", re.I)
EXTERNAL_WORDS = re.compile(r"클러스터\s*밖|외부|external|rds|aurora|saas|cloud ?sql", re.I)
DB_WORDS = re.compile(r"(^|-)db$|rds|mysql|postgres|aurora|oracle|mongo|dynamo", re.I)
NORMAL_WORDS = re.compile(r"정상|평소|매일|매주|배치|원래|늘 ")
KIND_FIX = {"deployment": "Deployment", "statefulset": "StatefulSet", "daemonset": "DaemonSet"}


def _clean(line):
    line = re.sub(r"^\s*(?:[-*+>]|\d+[.)])\s+", "", line)   # 목록 기호
    return line.replace("**", "").replace("`", "").strip()


def _node(tok):
    """'rabbitmq(rmq, 래빗)' → ('rabbitmq', ['rmq', '래빗']). 노드가 아니면 (None, [])."""
    tok = tok.strip().strip(".,;:")
    aliases = []
    m = re.match(r"^(.*?)\s*[(（\[]([^)）\]]*)[)）\]]\s*$", tok)
    if m:
        tok, inner = m.group(1).strip(), m.group(2)
        aliases = [a.strip() for a in re.split(r"[,/·]", inner) if a.strip() and len(a.strip()) <= 30]
    tok = re.sub(r"\s+", " ", tok)
    if not tok or len(tok) > 40 or tok.lower() in SKIP_NODES:
        return None, []
    # 문장 조각(공백 3개 이상)은 노드로 보지 않는다
    if tok.count(" ") >= 3:
        return None, []
    return tok, aliases


def _norm_workload(m):
    return "%s/%s/%s" % (m.group(1), KIND_FIX.get(m.group(2).lower(), m.group(2)), m.group(3))


def _match_workload(name, workloads):
    """구성요소 이름 → 클러스터 워크로드 (이름이 같거나, 하나뿐인 포함 관계)."""
    if not workloads:
        return None
    key = name.lower().replace(" ", "-")
    exact = [w for w in workloads if w.rsplit("/", 1)[-1].lower() == key]
    if len(exact) == 1:
        return exact[0]
    part = [w for w in workloads if key in w.rsplit("/", 1)[-1].lower()]
    return part[0] if len(part) == 1 else None


def extract(text, existing=None, workloads=None):
    """자유 글 → (flows.yaml dict 의 components·flows·normal, 메모 목록).
    existing: 지금 flows.yaml 의 components (워크로드·별명 등 이미 정한 값은 유지)."""
    comps = {k: dict(v or {}) for k, v in (existing or {}).items()}
    flows, normal, notes = {}, [], []
    text = re.sub(r"<!--.*?-->", "", text or "", flags=re.S)   # 주석(예시·안내)은 읽지 않는다
    lines = [_clean(l) for l in text.splitlines()]
    lines = [l for l in lines if l and not l.startswith("<!--") and not l.startswith("#")]

    def comp(name):
        return comps.setdefault(name, {})

    def add_alias(name, al):
        c = comp(name)
        cur = list(c.get("aliases") or [])
        for a in al:
            if a and a != name and a not in cur and not WORKLOAD.search(a) and not EXTERNAL_WORDS.fullmatch(a.strip()):
                cur.append(a)
        if cur:
            c["aliases"] = cur

    # 1) 화살표 줄 → 흐름
    for line in lines:
        if not ARROW.search(line):
            continue
        body = re.sub(r"^[^:：]{1,20}[:：]\s*(?=\S+\s*(→|->|=>|⇒))", "", line)  # "HTTP 흐름: a → b" 의 머리말
        parts = ARROW.split(body)
        nodes = []
        for p in parts:
            n, al = _node(p)
            if n:
                nodes.append(n)
                comp(n)
                add_alias(n, al)
        if len(nodes) < 2:
            continue
        queue = [n for n in nodes if QUEUE_WORDS.search(n) or comps[n].get("kind") == "message-queue"]
        if queue:
            hops = []
            for a, b in zip(nodes, nodes[1:]):
                role = "produce" if b in queue else "consume" if a in queue else "call"
                hops.append({"from": a, "to": b, "role": role})
            kind = "queue"
            for q in queue:
                comps[q].setdefault("kind", "message-queue")
        else:
            hops, kind = nodes, "http"
        name = "%s-%s" % (nodes[0], nodes[-1])
        i = 2
        while name in flows and flows[name]["hops"] != hops:
            name = "%s-%s-%d" % (nodes[0], nodes[-1], i)
            i += 1
        flows[name] = {"kind": kind, "hops": hops}

    names = sorted(comps, key=len, reverse=True)

    def mentioned(line):
        low = line.lower()
        out = []
        for n in names:
            for a in [n] + list(comps[n].get("aliases") or []):
                if a and re.search(r"(^|[^0-9a-z가-힣_-])%s($|[^0-9a-z_-])" % re.escape(a.lower()), low):
                    out.append(n)
                    break
        return out

    # 2) 줄마다: 별명 정의, 워크로드, 클러스터 밖, 로그 문구, 정상 패턴
    for line in lines:
        if ARROW.search(line):
            continue
        m = re.match(r"^([^:：]{1,40})[:：=]\s*(.+)$", line)
        if m:
            n, al = _node(m.group(1))
            if n and (n in comps or WORKLOAD.search(m.group(2))):
                comp(n)
                add_alias(n, al)
                rest = WORKLOAD.sub("", QUOTED.sub("", m.group(2)))
                if len(rest) <= 80:   # 짧은 쉼표 목록만 별명으로 (문장은 아님)
                    add_alias(n, [a.strip() for a in re.split(r"[,，]|\.\s", rest) if 0 < len(a.strip()) <= 20 and not EXTERNAL_WORDS.search(a)])
        hits = mentioned(line)
        if not hits:
            continue
        # "… 주문 워커, 컨슈머라고도 불러" → 별명
        m = re.search(r"([^.]*?)\s*(?:이)?라고도?\s*(?:불|부르)", line)
        if m and len(hits) == 1:
            seg = re.split(r"(?:이고|이며|고|는|은|를|을)\s+", m.group(1))[-1]
            add_alias(hits[0], [a.strip() for a in re.split(r"[,，]", seg) if 0 < len(a.strip()) <= 20])
        wl = WORKLOAD.search(line)
        if wl and len(hits) >= 1:
            comps[hits[0]]["workload"] = _norm_workload(wl)
            comps[hits[0]].pop("external", None)
        if EXTERNAL_WORDS.search(line) and len(hits) == 1 and not comps[hits[0]].get("workload"):
            comps[hits[0]]["external"] = True
        quotes = [q.strip() for q in QUOTED.findall(line)]
        if quotes:
            tgt = next((h for h in hits if comps[h].get("external")), hits[0])
            sig = list(comps[tgt].get("log_signatures") or [])
            for q in quotes:
                if q not in sig and q.lower() not in [h.lower() for h in hits]:
                    sig.append(q)
            if sig:
                comps[tgt]["log_signatures"] = sig
        if NORMAL_WORDS.search(line):
            note = {"component": hits[0], "note": line[:160]}
            if note not in normal:
                normal.append(note)

    # 3) 워크로드 채우기: 클러스터 목록에서 이름으로
    for n, c in comps.items():
        if c.get("workload") or c.get("external"):
            continue
        w = _match_workload(n, workloads)
        if w:
            c["workload"] = w
        elif DB_WORDS.search(n):
            c["external"] = True
        else:
            notes.append("%s: 클러스터에서 같은 이름의 워크로드를 못 찾았다. 같은 줄에 ns/Kind/이름 을 적어 주세요 (예: %s 는 shop/Deployment/%s)" % (n, n, n.replace(" ", "-")))
    if not flows:
        notes.append("흐름을 못 찾았다. 'a → b → c' 처럼 화살표로 한 줄 적으면 분석 경로로 쓴다")
    # 키 순서 정리
    order = ("workload", "kind", "tier", "aliases", "external", "log_signatures")
    comps = {n: {k: c[k] for k in sorted(c, key=lambda k: order.index(k) if k in order else 99)} for n, c in comps.items()}
    return {"components": comps, "flows": flows, "normal": normal}, notes


# ---------- 과거 이슈 ----------

def _date(text):
    m = re.search(r"(20\d\d)[-./년\s]+(\d{1,2})[-./월\s]+(\d{1,2})", text)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", text)
    if m:
        today = dt.date.today()
        try:
            d = dt.date(today.year, int(m.group(1)), int(m.group(2)))
            return d if d <= today else d.replace(year=today.year - 1)
        except ValueError:
            return None
    return None


def incident_meta(text, comps, find_symptom):
    """이슈 자유 글 → 머리말 (date, symptom, components, signature.logs, cause)."""
    meta = {}
    d = _date(text)
    if d:
        meta["date"] = d
    s = find_symptom(text)
    if s:
        meta["symptom"] = s
    low = text.lower()
    found = []
    for n, c in sorted((comps or {}).items(), key=lambda kv: -len(kv[0])):
        for a in [n] + list((c or {}).get("aliases") or []):
            if a and a.lower() in low:
                found.append(n)
                break
    if found:
        meta["components"] = found
    quotes = []
    for q in QUOTED.findall(text):
        q = q.strip()
        if q not in quotes:
            quotes.append(q)
    if quotes:
        meta["signature"] = {"logs": quotes}
    first = next((l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")), "")
    m = re.search(r"원인\s*[:：은는]?\s*(.+)", text)
    cause = (m.group(1) if m else first).strip()
    meta["cause"] = re.split(r"(?<=[^0-9])\.\s|\n", cause)[0].strip()[:120]
    return meta
