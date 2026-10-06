---
name: cluster-incident-analysis
description: "Use when diagnosing K8s incidents or onboarding a cluster."
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [kubernetes, incident, root-cause, opensearch, fluentd, mcp, koa, read-only, discovery]
    related_skills: [kubernetes-agent-access, hermes-mcp-servers, systematic-debugging]
---

# Cluster incident analysis (KOA)

KOA is the user's K8s ops-analysis agent (toolkit in `$HERMES_HOME/koa/`, repo github.com/leejw1212/koa). The agent diagnoses a
cluster it does not own, using only read-only access. This skill covers the analysis workflow and
the onboarding step that comes before it: discovering what the cluster offers and wiring MCP servers
(`koa-cluster-discovery` skill; design notes in `references/cluster-discovery.md`). Adding lab components (Prometheus/Grafana, ...) to exercise
those paths on kind: `references/kind-lab-fixtures.md`.
Access setup (SA, kubeconfig, terminal lock): `kubernetes-agent-access`. MCP wiring: `hermes-mcp-servers`.

## When to Use

- The user reports a cluster/log-pipeline symptom (logs stopped, pods restarting, queue backing up)
  and wants the cause.
- A new cluster is connected and KOA must discover what it can use, or `koa/discover.py` /
  `koa/plan.py` / `koa/catalog.yaml` need changes.
- The user asks how to make the agent's root-cause analysis faster or more accurate.

## Always-on rules

- **Analyze only through the read-only path**: the kubernetes/opensearch MCP tools, or terminal
  `kubectl` with `~/.kube/hermes-readonly.yaml` (same SA as the MCP). Never `~/.kube/config` (admin).
  The point is to exercise the system being built, so admin shortcuts invalidate the exercise.
- **No writes to the diagnosed system without asking** — including test traffic (`curl` to an app
  or ingress with a marker request id). It is a write and changes the evidence. If a hypothesis can
  only be confirmed by a probe, say so and ask, or ask the user to send it.
- **Filter before it enters context.** `kubectl_logs` returns raw lines (a single web pod's healthz
  spam floods the context). Always pass `since`/`tail`; prefer terminal kubectl with `grep -v`,
  `wc -l`, `jsonpath` when you need counts or exclusions.
- **Say what you could not verify.** If the cluster lacks a signal (no metrics, short event TTL,
  no heartbeat), state the conclusion as an inference and name the missing signal.
- **KOA never changes the target cluster.** Each cluster's signal stack (metrics, log schema, event
  retention) is owned by its operators; KOA controls discovery, MCP tools, runbooks, process and
  evaluation. Answer improvement questions at that layer; report cluster gaps as analysis limits with
  workarounds, never as changes to make.
- **Go step by step.** The user prefers one verified increment at a time (build → run on kind-lab →
  document in `docs/` → commit) over a large plan executed at once; stop and summarize at each step.
  After each code change to `koa/`, re-run discover/plan on kind-lab and update
  `docs/koa-cluster-discovery.md` (bugs found, re-run results) in the same commit (dev checkout).
- **Lab changes vs. analysis.** Read-only rules govern *analysis*. When the user explicitly asks to
  install/change something in the local kind lab, do it directly with the admin context (helm/kubectl)
  — no detour through GitOps repos — and keep values files in the repo's `lab/`. This applies only
  to the user's own kind lab, never to a cluster KOA is analyzing.
- Reply in Korean, plain claims, tables for evidence; end with what's left / the next decision.

## Procedure

1. **Read the cluster profile first** (`$HERMES_HOME/local/clusters/<name>.yaml`, or `clusters/` in a
   dev checkout). If missing or stale, run discovery (`koa-cluster-discovery`). It tells you which tools exist, log index/fields, pipeline
   shape, and known gaps — so you don't re-derive structure from configmaps every time.
2. **Scope the impact**: what, since when (exact UTC timestamp), which namespaces/nodes. Convert
   times — fluentd/OpenSearch log in UTC, the user speaks KST. Don't stop to ask: default to the last
   hour and the whole cluster.
3. **Triage before any single query**: `python3 koa/triage.py [--since 3h] [--at <UTC>] [--ns a,b]`.
   It sweeps nodes, pods, events, workloads, endpoints and rollouts through the kubernetes MCP in one go
   (≈3 s) and prints ranked anomalies with onset, evidence and the change right before each, a change
   timeline, ready-to-run next queries, and what it could not see. Paste its tables to the user right
   away with your first 2–3 hypotheses (an early read beats a late perfect answer), then verify. Its
   JSON lives in `<results>/<cluster>.triage/` — reuse it instead of re-listing pods.
   It folds same-minute restarts on one node into a single "host event" row and demotes pods on
   NotReady nodes; trust that grouping before chasing each workload. With the MCP, ConfigMap edits
   show only as creation (no managedFields); `--source kubectl` sees edits.
4. **Check for a cluster-wide event at that time first**: pod `RESTARTS (Xh ago)` all the same age,
   container `lastState.terminated` with exit 255/`Unknown` at one timestamp, DNS errors
   (`Resolv::ResolvError`, `Name or service not known`) right after → host/cluster restart, not a
   component fault. On kind, a Docker/Mac restart looks exactly like this.
5. **Walk the pipeline hop by hop**, comparing what each hop should have vs. what arrived.
   For a log pipeline (app → forwarder → aggregator → OpenSearch):
   - OpenSearch: `max(@timestamp)` + terms agg by `pod` with `max` per pod — which sources stopped.
   - Source: count lines per pod since that timestamp (`kubectl logs --since-time=... | wc -l`).
   - **Subtract what the pipeline drops on purpose** (read the forwarder filters: healthz exclude,
     path/namespace scope). Source lines that are all filtered = no real traffic, not a stall.
   - Forwarder/aggregator logs: buffer flush failures, `retry succeeded`, `detached/recovered
     forwarding server`. Recovered + no later errors = pipeline healthy.
   - Only declare "collection stopped" when unfiltered source lines exist that never reached the index.
6. **Rank 3–5 hypotheses** with the evidence for/against each before concluding.
7. **Report**: conclusion + confidence, evidence table (source/tool per row), timeline, what could
   not be verified, recommended actions (not executed).

## Pitfalls

- "No documents since T" in a log index is ambiguous: traffic-free periods and stalled shippers look
  identical without a heartbeat. Step 4's source-vs-index comparison is the read-only way to tell.
- `fieldSelector status.phase!=Running` misses CrashLoopBackOff; list all pods and read RESTARTS.
- mcp-server-kubernetes 4.1.9 `kubectl_get` with `output: json` on a list returns only name/namespace/
  status/createdAt (it reshapes it). Use `output: yaml` for full objects (triage.py does). Its default
  output cap is 1 MB (`SPAWN_MAX_BUFFER`); triage raises it for its own process only.
- Remaining k8s events are not a retention measure — read kube-apiserver `--event-ttl` (default 1h).
