---
name: koa-cluster-discovery
description: "Use when onboarding a cluster to KOA: discover, report, attach MCPs."
version: 2.1.0
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

Run the whole flow without asking. MCP registration is automatic: whatever discovery found and
`.env` already has credentials for gets registered, verified and reported. Stop only when blocked
(no read-only kubeconfig, account not read-only).

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
4. **Register + verify, automatically.** `python3 koa/plan.py <name> --apply` (no confirmation).
   For every verified MCP whose component was found and whose `.env` keys are present it:
   1. checks the backend account is read-only (`koa/readonly.py`: permission queries only, no writes).
      `fail` = account can write/admin → not registered (and unregistered if it was);
      `warn` = backend can't restrict this account (e.g. OpenSearch without security plugin) →
      registered, MCP-layer blocking only, called out in the report;
   2. registers it (`~/.local/bin/hermes config set`, `HERMES_HOME` = this profile);
   3. starts it over stdio and runs one probe query (`koa/query.py --probe`).
   Results → `<name>.mcp.yaml` and report section 5. Candidates are never auto-registered
   (`--with <name>` only if the user asks).
5. **Report the result** as tables: section 5 (read-only verdict, registered, probe, exposed tools),
   then section 4 for what is still missing. For missing credentials, tell the user which keys to put
   in `$HERMES_HOME/.env` themselves (`OPENSEARCH_*`, `GRAFANA_SERVICE_ACCOUNT_TOKEN` = Viewer SA,
   `ARGOCD_API_TOKEN` = role:readonly account) with the URL discovery found, and that rerunning
   `plan.py --apply` attaches them. Never ask for a secret in chat; never print `.env` values.
6. **Apply.** If config changed, tell the user to restart the desktop app (and
   `~/.local/bin/hermes gateway restart` if they use the gateway), then open a new chat in this profile.

## Querying (after onboarding)

`python3 koa/query.py` lists named queries per registered MCP (from `koa/catalog.yaml` `queries`);
`python3 koa/query.py <mcp> <query> key=value ...` runs one through the same stdio server and
allowlist, e.g. `prometheus firing`, `prometheus restarts range=6h`, `kubernetes logs name=<pod> ns=<ns>`,
`opensearch search index='app-*' q='level:error'`, `argocd history name=<app>`. `--call <mcp> <tool> '<json>'`
calls any allowlisted tool directly. In a chat where the MCP tools are loaded, calling them directly is
equivalent; use query.py from the terminal, in scripts, or before the app has been restarted.
`python3 koa/query.py --probe` = one read per server as a health table.

Advice text lives in `koa/catalog.yaml` (`gap_advice`, component `fallback`). Improve it there,
not in ad-hoc chat text.

## 클러스터 지식 흐름 → 구성요소 매칭 (flow_nodes)

흐름(flow) 경로의 노드와 구성요소(component)는 정확 일치만 되던 걸, 부분 매칭으로 개선했다(커밋 8b2d65c).
- 매칭 대상은 구성요소 이름 + `aliases`(질문 매칭용) + `flow_nodes`(흐름 매칭 전용).
  노드가 그 이름과 같거나 한쪽이 다른 쪽을 부분 문자열로 포함하면 매칭(node_to_comps).
- 여러 구성요소가 같은 흐름 이름을 공유할 수 있다. 예: 흐름 노드 `Gateway` 는
  `flow_nodes: [Gateway]` 를 둔 접근 게이트웨이 구성요소 여러 개를 동시에 가리킨다(예: public/manager/admin 게이트웨이).
- `aliases` 는 질문에서 대상을 찾는 데 쓰므로 공통 이름을 넣으면 '질문에서 대상을 못 가린다' warn이 난다.
  공통 이름은 `flow_nodes` 에, 고유한 부르는 말은 `aliases` 에 넣는다.
- 질문이 여러 구성요소와 매칭되면(동점) plan 은 target 을 고정하지 않고 `candidates` 를 내보낸다.
  웹 UI 조사 요청에서 '대상을 직접 선택' 칩을 보여주고, 선택 시 확인 순서가 그 대상 기준으로 재구성된다.
  API: `POST /api/plan` body 에 `target` 을 넣으면(force_target) 그대로 고정.
- 구성요소 편집 폼에 '흐름 매칭 이름' 필드가 있어 flow_nodes 를 쉼표 구분으로 입력/저장한다(save_shards 가 보존).

## Promoting candidate → verified (catalog maintenance, dev checkout)

1. Get read-only creds. Real clusters: from the user. kind-lab: `python3 lab/kind-lab-tokens.py`
   (admin context, lab only), then `python3 lab/kind-lab-verify-tokens.py` (reads OK, writes 403/no;
   prefer can-i APIs over real write attempts).
2. `plan.py --apply --with <mcp>`, then `check_mcp.py <mcp>`, one real read, and one write attempt
   that must be refused (dry-run where possible).
3. Fix `include`, add `probe` (one cheap read) and `queries`, add a `check_<mcp>` to `koa/readonly.py`,
   set `status: verified` with a dated comment in `koa/catalog.yaml`, update
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
- Never run the `hermes` CLI (incl. `plan.py --apply`) with `HERMES_HOME` outside `~/.hermes`: it
  re-mints `~/.hermes/hermes-agent/.hermes/bin/hermes` against that folder's Python and every `hermes`
  command breaks. `paths.hermes_env()` refuses; test with a throwaway profile under
  `~/.hermes/profiles/`. Repair: run `ensure_install_launchers(repo, repo/.hermes/bin)` with the store
  Python from `~/.hermes/tools/python-*/bin/python3` and `HERMES_HOME` unset.
- Calico API server grants create/delete on `projectcalico.org` networkpolicies to every authenticated
  user (`calico-tiered-policy-passthrough`) and enforces via `tier.networkpolicies`; readonly.py checks
  the tier permission before calling it a write leak. Use SelfSubjectRulesReview per namespace +
  SelfSubjectAccessReview `reason` to find which binding grants a verb.
- OpenSearch with `DISABLE_SECURITY_PLUGIN=true` still lists `opensearch-security` in `_cat/plugins`;
  `/_plugins/_security/authinfo` returns 400 "no handler". Then there are no accounts server-side
  (Ingress basic auth only) → readonly verdict `warn`.
- mcp-grafana `list_alert_groups`/`get_alert_group` are Grafana OnCall tools (404 without OnCall);
  Prometheus rule state is in `alerting_rules_read` with `datasource_uid`, firing alerts via
  Prometheus `ALERTS{alertstate="firing"}`.
- opensearch `SearchIndexTool` takes `query_dsl` (not `query`); searching `*` fails on system indices
  without `@timestamp` — always pass an index pattern.
