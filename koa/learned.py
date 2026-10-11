#!/usr/bin/env python3
"""KOA 가 쓰면서 배운 것: 이 프로필(HERMES_HOME)의 Hermes 메모리와, KOA 가 만들거나 고친 스킬.

- 메모리: memories/MEMORY.md (KOA 메모), memories/USER.md (사용자). 항목 구분은 Hermes 와 같은 "\\n§\\n".
  지우기는 Hermes 와 같은 잠금 파일(<파일>.lock, flock)을 잡고 임시 파일 → rename 으로 쓴다.
- 스킬: koa-learned (KOA 가 분석하며 배운 요령을 쌓는 스킬, 저장소에 없어서 프로필 업데이트가 덮지 않는다)
  + skills/.curator_ledger.jsonl 에 기록이 있는 스킬 + .usage.json 에 created_by: agent 인 스킬.
  보기만 한다.

  python3 koa/learned.py            # 요약
  python3 koa/learned.py init       # koa-learned 스킬이 없으면 만든다
"""
import datetime as dt
import json
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

DELIM = "\n§\n"
LEARNED_SKILL = "koa-learned"
MEMORY_FILES = {"memory": "MEMORY.md", "user": "USER.md"}


def home():
    return Path(paths.HERMES_HOME)


# ---------- 메모리 ----------

def _mem_path(target):
    if target not in MEMORY_FILES:
        raise ValueError("target 은 memory 또는 user")
    return home() / "memories" / MEMORY_FILES[target]


def _parse(raw):
    return [e for e in (x.strip() for x in raw.split(DELIM)) if e]


def memory():
    out = {}
    for t in MEMORY_FILES:
        f = _mem_path(t)
        out[t] = _parse(f.read_text(encoding="utf-8")) if f.is_file() else []
    return out


@contextmanager
def _lock(path):
    lock = path.with_suffix(path.suffix + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def delete_memory(target, text):
    """항목 하나를 지운다. text 는 화면에 보인 항목 전체 (같은 글자일 때만 지운다 — 그사이 바뀌었으면 거부)."""
    f = _mem_path(target)
    with _lock(f):
        entries = _parse(f.read_text(encoding="utf-8")) if f.is_file() else []
        if text not in entries:
            raise LookupError("그 항목이 없다 (그사이 바뀌었을 수 있다. 새로고침)")
        entries.remove(text)
        fd, tmp = tempfile.mkstemp(dir=str(f.parent), prefix=".mem_")
        with os.fdopen(fd, "w", encoding="utf-8") as w:
            w.write(DELIM.join(entries))
        os.replace(tmp, str(f))
    return entries


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
        meta, body = _front(p.read_text(encoding="utf-8"))
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


def summary():
    m = memory()
    return {"home": str(home()), "memory": m, "skills": skills()}


if __name__ == "__main__":
    if sys.argv[1:] == ["init"]:
        f = ensure_learned_skill()
        print("만들었다: %s" % f if f else "이미 있다")
        sys.exit(0)
    s = summary()
    print("HERMES_HOME:", s["home"])
    for t, es in s["memory"].items():
        print("메모리 %s: %d 항목" % (t, len(es)))
        for e in es:
            print("  - " + e.replace("\n", " ")[:120])
    print("스킬: %d" % len(s["skills"]))
    for k in s["skills"]:
        print("  %s%s  변경 %d · 사용 %d · %s" % (k["name"], " (배운 요령)" if k["learned"] else "",
                                             k["change_count"], k["use_count"], k["path"]))
