---
name: koa-cluster-discovery
description: "Use when onboarding a cluster to KOA: discover, report, attach MCPs."
version: 2.0.0
metadata:
  hermes:
    tags: [koa, kubernetes, mcp, discovery, onboarding]
    related_skills: [cluster-incident-analysis, hermes-mcp-servers, kubernetes-agent-access]
---

# KOA cluster onboarding: discover → report → attach MCPs with the user

KOA's toolkit lives in the profile directory, `$HERMES_HOME/koa/`, installed with
`hermes profile install`. In a git checkout used for development, the same scripts run from the
repo root. Commands below assume `cd "$HERMES_HOME"`. The scripts resolve every path themselves
(`koa/paths.py`): results go to `local/clusters/` when installed or `clusters/` in a dev checkout,
secrets are read from `$HERMES_HOME/.env`, and config writes go to `$HERMES_HOME/config.yaml`.
Docs: `docs/koa-cluster-discovery.md`, `koa/README.md`.

## Principle

KOA never changes the target cluster. Discovery only finds what is usable now; the only change KOA
makes is registering MCP servers in this profile. Never recommend cluster-side changes (enabling
exporters/ServiceMonitors, opening Ingress, installing event-exporter or metrics-server, ...).
Gaps are analysis limits, presented with the workaround using tools that exist now.

## Onboarding flow (user says "클러스터 확인하고 MCP 세팅해줘" or similar)

Go one step at a time and stop for the user where marked.

1. **Preflight (read-only).**
   ```bash
   cd "$HERMES_HOME"
   ls -la ~/.kube/hermes-readonly.yaml
   kubectl config current-context            # must end in -readonly (terminal defaults KUBECONFIG)
   for v in "list pods" "delete pods" "list secrets"; do printf '%-14s ' "$v"; kubectl auth can-i $v -A; done
   ```
   - No kubeconfig → stop. Explain that a cluster admin must create the read-only SA once:
     `k8s/hermes-readonly.yaml` + `k8s/make-readonly-kubeconfig.sh <admin-context>`
     (writes `~/.kube/hermes-readonly.yaml`). KOA does not run this itself.
   - `delete pods` or `list secrets` = yes → the account is not read-only; stop and report.
2. **Discover.** `python3 koa/discover.py --probe` (≈2 s; read-only API calls, GETs on health paths only).
   It writes the cluster profile + report and prints the report. It refuses `~/.kube/config`.
3. **Report in chat** (required, never just "ran it"):
   1. Components table (kind, location, outside URL + probe, Prometheus scraped?).
   2. MCP table (verified/candidate, current, decision, what's needed).
   3. Analysis limits: impact + KOA workaround (from `gap_advice.workaround` and component `fallback`).
   4. Next steps = MCP only: register now / needs credentials / unreachable (no outside URL).
   If a previous profile exists, call out what changed.
4. **Ask the user** (one clarify form): which MCPs to attach. For each one that needs credentials,
   ask them to put the values in `$HERMES_HOME/.env` themselves: `OPENSEARCH_*`,
   `GRAFANA_SERVICE_ACCOUNT_TOKEN` (Viewer SA), `ARGOCD_API_TOKEN` (role:readonly account).
   Suggest the URL discovery found. Never ask for a secret in chat and never print `.env` values;
   only check presence. `python3 koa/plan.py <name>` lists what's still missing.
5. **Register.** `python3 koa/plan.py <name> --apply [--with a,b]` (candidates need `--with`).
   It runs `~/.local/bin/hermes config set` with `HERMES_HOME` pointing at this profile.
6. **Verify each attached server** before calling it done:
   `python3 koa/check_mcp.py <name...>` starts the server over stdio, compares the real tool list
   with `include` and flags write-looking tools. Then make one real read call per server.
7. **Apply.** Tell the user to restart the desktop app (and run `~/.local/bin/hermes gateway restart`
   if they use the gateway), then open a new chat in the KOA profile and confirm there with
   `tool_search` and one call per MCP.

Advice text lives in `koa/catalog.yaml` (`gap_advice`, component `fallback`). Improve it there,
not in ad-hoc chat text.

## Promoting candidate → verified (catalog maintenance, dev checkout)

1. Get read-only creds. Real clusters: from the user. kind-lab: `python3 lab/kind-lab-tokens.py`
   (admin context, lab only), then `python3 lab/kind-lab-verify-tokens.py` (reads OK, writes 403/no;
   prefer can-i APIs over real write attempts).
2. `plan.py --apply --with <mcp>`, then `check_mcp.py <mcp>`, one real read, and one write attempt
   that must be refused (dry-run where possible).
3. Fix `include`, set `status: verified` with a dated comment in `koa/catalog.yaml`, update
   `docs/koa-cluster-discovery.md`, commit and push.

## Pitfalls

- `kubectl auth can-i get pods/log` treats `log` as a resource name; use `--subresource` (discover does).
- Event retention: read kube-apiserver `--event-ttl` (default 1h); remaining event age is unreliable
  (kind after a laptop sleep kept 11h-old events).
- catalog.yaml: a YAML value must not start with a quote and continue after it (`'x' y`) —
  validate with `python3 -c "import yaml;yaml.safe_load(open('koa/catalog.yaml'))"`.
- The rabbitmq MCP blocks localhost/private IPs (SSRF guard), so it can't be used on kind.
- argoproj-labs `argocd-mcp` 0.9.0 drops the base URL path, so it can't be used under a sub-path.
  The catalog uses `peopleforrester/mcp-k8s-observability-argocd-server` pinned by git SHA (keeps the
  path, `MCP_READ_ONLY`). Catalog flag `url_must_be_root` marks servers with this limitation.
- mcp-grafana `--disable-write` still exposes 62 tools incl. `alerting_manage_routing` and
  `grafana_api_request`; keep the curated `include` list.
- macOS system python is 3.9 — keep KOA scripts free of 3.10+ syntax.
