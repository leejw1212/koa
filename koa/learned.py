#!/usr/bin/env python3
"""KOA 가 쓰면서 배운 것: KOA 가 만들거나 고친 스킬.

- 스킬: koa-learned (KOA 가 분석하며 배운 요령을 쌓는 스킬, 저장소에 없어서 프로필 업데이트가 덮지 않는다)
  + skills/.curator_ledger.jsonl 에 기록이 있는 스킬 + .usage.json 에 created_by: agent 인 스킬.
  보기만 한다.

  python3 koa/learned.py            # 요약
  python3 koa/learned.py init       # koa-learned 스킬이 없으면 만든다
  python3 koa/learned.py summarize <스킬>   # 한국어 요약을 만든다 (KOA 프로필의 모델로, 도구 없이)

한국어 요약은 local/learned/summaries/<스킬>.json 에 SKILL.md 의 sha256 과 함께 저장한다.
스킬이 바뀌면 hash 가 달라져 "요약이 오래됨" 으로 보이고, 다시 만들면 된다.
"""
import datetime as dt
import hashlib
import json
import subprocess
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths  # noqa: E402

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

LEARNED_SKILL = "koa-learned"


def home():
    return Path(paths.HERMES_HOME)


# ---------- 스킬 ----------

def _skills_root():
    return home() / "skills"


def _find_skill(name):
    root = _skills_root()
    if not root.is_dir():
        return None
    for p in root.glob("*/%s/SKILL.md" % name):
        return p
    p = root / name / "SKILL.md"
    return p if p.is_file() else None


def _ledger():
    f = _skills_root() / ".curator_ledger.jsonl"
    out = []
    if f.is_file():
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("skill"):
                out.append({"ts": d.get("ts") or "", "actor": d.get("actor") or "", "action": d.get("action") or "",
                            "skill": d["skill"]})
    return out


def _usage():
    f = _skills_root() / ".usage.json"
    try:
        d = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
    except ValueError:
        d = {}
    return d if isinstance(d, dict) else {}


def _front(text):
    meta = {}
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            for line in text[3:end].splitlines():
                if ":" in line and not line.startswith(" "):
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip().strip('"')
            text = text[end + 4:].lstrip("\n")
    return meta, text


def skills():
    led = _ledger()
    use = _usage()
    names = {LEARNED_SKILL} | {e["skill"] for e in led} | {n for n, r in use.items()
                                                           if isinstance(r, dict) and r.get("created_by") == "agent"}
    out = []
    for n in sorted(names):
        p = _find_skill(n)
        if not p:
            continue
        raw = p.read_text(encoding="utf-8")
        meta, body = _front(raw)
        r = use.get(n)
        r = r if isinstance(r, dict) else {}
        hist = [e for e in led if e["skill"] == n]
        out.append({
            "name": n,
            "description": meta.get("description", ""),
            "path": str(p.relative_to(home())),
            "learned": n == LEARNED_SKILL,
            "changes": hist[-30:],
            "change_count": len(hist),
            "use_count": r.get("use_count", 0),
            "last_used": r.get("last_used_at") or "",
            "modified": dt.datetime.fromtimestamp(p.stat().st_mtime, dt.timezone.utc).isoformat(timespec="seconds"),
            "body": body,
            "ko": cached_summary(n, raw),
        })
    out.sort(key=lambda s: (not s["learned"], s["name"]))
    return out


SEED = """---
name: koa-learned
description: "Use when analyzing K8s in KOA: lessons KOA learned so far."
version: 0.1.0
author: KOA
metadata:
  hermes:
    tags: [koa, kubernetes, lessons]
    related_skills: [cluster-incident-analysis]
---

# What KOA learned while analyzing

Cluster-independent lessons from past analyses: tool quirks, better queries, pitfalls.
No cluster names, workloads or flows here (those go to `local/knowledge/<cluster>/`).
Each lesson: one imperative line + why. Newest last.

## Lessons
"""


def ensure_learned_skill():
    """koa-learned 가 없으면 만든다 (저장소에 싣지 않는다 — 실으면 프로필 업데이트가 배운 것을 덮는다)."""
    if _find_skill(LEARNED_SKILL):
        return None
    f = _skills_root() / "devops" / LEARNED_SKILL / "SKILL.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(SEED, encoding="utf-8")
    return f


# ---------- 한국어 요약 ----------

SUMMARY_PROMPT = """아래는 KOA(쿠버네티스 장애 분석 에이전트)의 스킬 문서다. 사용자가 웹에서 읽을 한국어 요약을 써라.
형식 (이것만 출력, 다른 말 없이):
첫 줄: 이 스킬이 무엇을 위한 것인지 한 문장
빈 줄
## 언제 쓰나
- 1~3줄
## 핵심 규칙·절차
- 중요한 것부터 3~8줄. 명령·파일 이름은 그대로 둔다
## 주의할 점
- 함정·배운 것 0~6줄 (없으면 이 절을 뺀다)
문장은 짧고 평이하게. 원문에 없는 내용은 쓰지 않는다.

---
"""


def _sum_file(name):
    return paths.REPO / "local" / "learned" / "summaries" / ("%s.json" % name)


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cached_summary(name, text):
    f = _sum_file(name)
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    d["stale"] = d.get("sha256") != _sha(text)
    return d


def _profile_args():
    """hermes CLI 를 이 프로필로. ~/.hermes/profiles/<이름> 이면 -p <이름>."""
    h = Path(paths.HERMES_HOME).resolve()
    prof = (Path.home() / ".hermes" / "profiles").resolve()
    if h.parent == prof:
        return ["-p", h.name], {k: v for k, v in os.environ.items() if k != "HERMES_HOME"}
    return [], paths.hermes_env()


def summarize(name, timeout=240):
    """한국어 요약을 만들어 저장한다. 모델 호출 1번, 도구는 todo 하나만 (클러스터·파일에 손대지 않는다)."""
    p = _find_skill(name)
    if not p or name not in {s["name"] for s in skills()}:
        raise LookupError("KOA 가 만들거나 고친 스킬이 아니다: %s" % name)
    text = p.read_text(encoding="utf-8")
    pre, env = _profile_args()
    fd, q = tempfile.mkstemp(prefix="koa-sum-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as w:
            w.write(SUMMARY_PROMPT + text)
        r = subprocess.run([str(paths.HERMES_BIN)] + pre + ["chat", "-Q", "--query-file", q, "-t", "todo",
                                                           "--reasoning", "low", "--ignore-rules", "--source", "koa-web"],
                           capture_output=True, text=True, timeout=timeout, env=env, cwd=str(Path.home()))
    finally:
        os.unlink(q)
    out = [l for l in (r.stdout or "").splitlines() if not l.startswith("session_id:")]
    body = "\n".join(out).strip()
    if r.returncode != 0 or not body:
        raise RuntimeError("요약 실패: %s" % ((r.stderr or r.stdout or "").strip()[-300:] or r.returncode))
    first, _, rest = body.partition("\n")
    d = {"name": name, "sha256": _sha(text), "created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
         "oneline": first.strip(), "text": rest.strip()}
    f = _sum_file(name)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    d["stale"] = False
    return d


def summary():
    return {"home": str(home()), "skills": skills()}


if __name__ == "__main__":
    if sys.argv[1:2] == ["summarize"] and len(sys.argv) == 3:
        d = summarize(sys.argv[2])
        print(d["oneline"] + "\n\n" + d["text"])
        sys.exit(0)
    if sys.argv[1:] == ["init"]:
        f = ensure_learned_skill()
        print("만들었다: %s" % f if f else "이미 있다")
        sys.exit(0)
    s = summary()
    print("HERMES_HOME:", s["home"])
    print("스킬: %d" % len(s["skills"]))
    for k in s["skills"]:
        print("  %s%s  변경 %d · 사용 %d · %s" % (k["name"], " (배운 요령)" if k["learned"] else "",
                                             k["change_count"], k["use_count"], k["path"]))
