---
name: cluster-incident-analysis
description: "Use when analyzing a K8s problem the user names in KOA."
version: 2.0.0
author: KOA
license: MIT
metadata:
  hermes:
    tags: [kubernetes, incident, root-cause, koa, read-only, knowledge]
    related_skills: [koa-cluster-discovery, kubernetes-agent-access, hermes-mcp-servers]
---

# Cluster incident analysis (KOA)

KOA analyzes only what the user names, using the cluster knowledge in `local/knowledge/<cluster>/`, and
grows that knowledge with what each analysis teaches — only after the user's OK. Onboarding a new
cluster (discover, MCP setup) is `koa-cluster-discovery`. Lab setup notes: `references/kind-lab-fixtures.md`.

## When to Use

- The user names a component or flow and a symptom ("rmq 소비가 안 돼", "카드 만들기 흐름이 느려") and wants the cause.
- The user asks to record/correct what KOA knows about a cluster (components, flows, normal patterns, incidents).

## Always-on rules

- **Scope = what the user named.** No cluster-wide sweeps (all pods, all events, all namespaces). Touch
  only the components in the context slice. If the evidence points outside it, say so and ask before widening.
- **Read-only path only**: the kubernetes/opensearch/... MCP tools, or terminal `kubectl` with
  `~/.kube/hermes-readonly.yaml`. Never `~/.kube/config`. Reachability GETs are fine; requests that make
  an app do work need the user's OK.
- **Knowledge changes need the user's OK.** Never edit `local/knowledge/` without it; unconfirmed findings
  go to suggestions.
- **Filter before it enters context.** `kubectl_logs` returns raw lines; always pass `since`/`tail`; prefer
  terminal kubectl with `grep -v`, `wc -l`, `jsonpath` when you need counts or exclusions.
- **Say what you could not verify.** Missing signal (no metrics, short event TTL, no heartbeat) → state the
  conclusion as an inference and name the missing signal.
- Reply in Korean, plain claims, tables for evidence; end with the knowledge proposal / next decision.

## Procedure

1. **Which cluster**: `python3 koa/knowledge.py list`. One → use it. Several → the read-only kubeconfig's
   current context minus `-readonly`, or ask.
2. **Pull the slice**: `python3 koa/knowledge.py context <cluster> "<사용자 질문 그대로>"`
   (`--with a,b` when the user named things explicitly or corrected you). Show its first lines (asked
   components, flows) at the top of your first reply so the user can correct the reading.
   - Nothing matched → ask which component/flow they mean (offer the knowledge's component names). Their
     answer is a new alias: include it in the proposal at the end.
   - A name the knowledge doesn't have → find just that workload read-only (`kubectl get deploy,sts,ds -A
     | grep <name>`), analyze it, and propose adding it.
3. **Past incidents first**: for each `과거 이슈` in the slice, grep its log lines in the named components
   (`kubectl logs --since=1h ... | grep -c "<line>"`). A hit is the strongest lead.
4. **Walk the flow hop by hop** over the components in the slice, in flow order (for a queue backlog start
   at the consumer, then broker, then the consumer's downstream, then the producer). Per hop: pods
   (ready/restarts/lastState), recent events for those pods, logs (`since`, `tail`, the component's
   `로그` lines), and the call to the next hop. Judge each against its `정상 패턴` before calling it
   abnormal — a matching normal pattern means "not the cause", say so.
   The cause point is where the flow turns from normal to abnormal.
5. **Hypotheses**: 2–3 with evidence for/against; share the early read, then confirm.
6. **Report**: conclusion + confidence, evidence table (hop · what was checked · tool · result), what could
   not be verified, where the knowledge didn't match the cluster (missing workload, wrong hop).
7. **Knowledge proposal** — always end with it, even if empty ("새로 알게 된 것 없음"):

   | 종류 | 내용 | 근거 |
   |---|---|---|
   | 구성요소 / 별명 / 로그 문구 / 흐름 / 정상 패턴 / 과거 이슈 | … | 어디서 봤는지 |

   Ask "이대로 지식에 더할까요?" After OK, write exactly the approved rows:
   ```bash
   python3 koa/knowledge.py add <c> '{"components": {"rabbitmq": {"aliases": ["래빗"], "log_signatures": ["disk resource limit alarm set"]}},
                                     "flows": [{"name": "카드-만들기", "nodes": ["linkcard-web", "rabbitmq", "linkcard-worker"]}],
                                     "normal": [{"component": "linkcard-worker", "note": "…"}]}'
   python3 koa/knowledge.py incident <c> '{"date": "2026-10-11", "components": ["rabbitmq"], "logs": ["…"], "cause": "…", "found_by": "…", "body": "한 문단"}'
   ```
   `add` only adds (merges list fields, fills empty fields, skips flows already known); show its output.
   Rows the user declines but that may matter later → `knowledge.py suggest <c> '<json>' --title "…" --why "…"`
   only if they say "나중에". Use `--dry-run` first when unsure what `add` will change.
8. **Self-improvement** (no OK needed, tell the user in one line): a cluster-independent analysis lesson →
   patch the Pitfalls of this skill (in the dev checkout `~/hermes-config/skills/...` if it exists, so it
   survives profile updates); a stable user/environment fact → memory. Never put cluster names/flows there.

## Flow discovery on request

"흐름 찾아줘" → `python3 koa/flowmap.py [--ns a,b]` (config + logs only). It saves flows not yet in the
knowledge as suggestions; show its table and tell the user to pick them in the web UI (클러스터 지식 → 제안)
or say which to add (`knowledge.py suggestions <c>`, then `accept <c> <id>` per the user's choice).

## Pitfalls

- A cluster-wide event looks like many faults: pod `RESTARTS (Xh ago)` all the same age, `lastState.terminated`
  exit 255/`Unknown` at one timestamp, DNS errors right after → host/cluster restart. On kind, a Docker/Mac
  restart looks exactly like this. Check the restart times of the components in scope before chasing each.
- "No documents since T" in a log index is ambiguous: no traffic and a stalled shipper look the same without a
  heartbeat. Compare source lines (`kubectl logs --since-time=… | wc -l`, minus what the forwarder filters on
  purpose) with what reached the index.
- `fieldSelector status.phase!=Running` misses CrashLoopBackOff; read RESTARTS and container state.
- mcp-server-kubernetes 4.1.9 `kubectl_get` with `output: json` on a list returns only name/namespace/status;
  use `output: yaml` for full objects. Its default output cap is 1 MB (`SPAWN_MAX_BUFFER`) — scope by namespace.
- Pods healthy but calls time out (callback, DB) → check NetworkPolicies in the namespaces in scope
  (`kubectl get netpol -n <ns> -o yaml`, look at creation/edit time).
- With the MCP, ConfigMap edits show only as creation (no managedFields); terminal kubectl
  `--show-managed-fields` shows edit times.
- Remaining k8s events are not a retention measure — read kube-apiserver `--event-ttl` (default 1h).
