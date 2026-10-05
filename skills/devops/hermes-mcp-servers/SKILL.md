---
name: hermes-mcp-servers
description: "Use when adding or changing Hermes MCP servers."
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [mcp, hermes, config, opensearch, kubernetes, read-only, desktop]
    related_skills: [kubernetes-agent-access, hermes-agent]
---

# Wiring MCP servers into Hermes (read-only ops servers)

Covers the generic procedure for any stdio MCP server under `mcp_servers:`, plus the user's standing
shape: ops servers (Kubernetes, OpenSearch, ...) are exposed **read-only**, with a tool whitelist,
secrets in `$HERMES_HOME/.env`, and config shipped by the KOA profile distribution.
Kubernetes specifics (RBAC SA kubeconfig, terminal kubectl): `kubernetes-agent-access`.
OpenSearch specifics: `references/opensearch.md`.
Read-only switches for Prometheus/Grafana/Argo CD/RabbitMQ MCPs: `references/ops-mcp-catalog.md`.
Choosing which MCPs to attach per cluster (KOA discovery/catalog): `cluster-incident-analysis`.

## When to Use

- Adding, replacing, or removing an entry under `mcp_servers:` (k8s, OpenSearch, any stdio server).
- Restricting an MCP server to read-only / a tool whitelist.
- Checking whether an MCP change actually reloaded in the desktop app.

## Always-on rules

- **Inspect before replacing — or before claiming anything about the current setup.**
  `$HERMES_HOME/config.yaml` may already hold earlier sessions' work
  (MCP blocks, `k8s/` RBAC manifests, generated kubeconfigs, `docs/`). Read `config.yaml`, README,
  `git log --oneline` and check the referenced files (e.g. `KUBECONFIG_PATH` target, `.env` key names)
  before saying something is "probably admin" or "not set up" — a guess from defaults is often wrong
  here. Show the user the existing block + where it came from before overwriting; ask on conflict.
- **The live config is the source of truth for docs.** When a draft plan/guide (shared chat, design
  doc) differs from what is actually configured, write the guide from the verified live values into
  the repo's `docs/` and list the deltas explicitly (server package, SA/namespace names, env var,
  tool names, versions). Real Hermes tool names are `mcp__<server>__<tool>` (not `mcp_<server>_<tool>`
  as some docs say) — confirm from the session's tool list.
- **Pin MCP package versions** in `args` (`pkg@x.y.z`). Find the installed version from
  `~/.npm/_npx/*/node_modules/<pkg>/package.json` (npx) or
  `uvx --from <pkg> python -c "import importlib.metadata as m;print(m.version('<pkg>'))"` (uvx).
  `serverInfo.version` in the `initialize` reply can be the framework version, not the package
  (opensearch-mcp-server-py 0.11.0 reports 1.30.0).
- **Write config only via `hermes config set`** (terminal). The file tools refuse `config.yaml` —
  including a symlink's *target* in a repo — so don't stop at the
  refusal and hand the edit to the user; run `hermes config set` yourself. Set a whole
  server in one call with a YAML flow mapping:
  `hermes config set mcp_servers.<name> '{command: uvx, args: [pkg], env: {K: "v"}, tools: {include: [A, B], prompts: false, resources: false}}'`
  Single fields work too: `hermes config set mcp_servers.<name>.args '["-y", "pkg@1.2.3"]'`,
  `hermes config set mcp_servers.<name>.tools.prompts false`. It writes through the symlink and keeps
  it — confirm with `ls -la "$HERMES_HOME/config.yaml"` and a diff (only the intended lines changed).
  For a profile, run it with that profile's home: `HERMES_HOME=~/.hermes/profiles/<p> ~/.local/bin/hermes config set ...`
  (or `hermes -p <p> config set ...`).
- **Env values must be strings.** `env.X true` stores a YAML bool and `env.X '"true"'` stores literal
  quote characters. Set the env block as a mapping (`env '{X: "true"}'`) and verify with
  `python3 -c "import yaml;print(yaml.safe_load(open(...)))"`.
- **Secrets go in `$HERMES_HOME/.env`** (chmod 600), referenced as `${VAR}` in config. Commit only a
  `.env.example` — put comments on their own lines, never after a value (dotenv keeps them).
- **Always set `tools.prompts: false` and `tools.resources: false`** unless needed; otherwise Hermes
  adds `list_resources`/`read_resource`/`list_prompts`/`get_prompt` on top of `include`.
- **Prefer the server's own read-only switch plus `tools.include`.** A whitelist alone only hides
  tools; drop any "call arbitrary API" tool (e.g. OpenSearch `GenericOpenSearchApiTool`).

## Procedure

1. **Probe the backend first** (is it reachable from the host? auth on? TLS?). In-cluster services
   need `kubectl -n <ns> port-forward svc/<svc> <port>:<port>` run with `terminal(background=true)`.
2. **Discover real tool names** before writing `include` — names vary by server version:
   add the server without `include`, run `hermes mcp test <name>` (lists *all* discovered tools and
   proves connect + auth), then set `tools.include`. `hermes mcp list` shows "N selected".
3. **Smoke-test a real call** with a raw stdio probe (initialize → wait for id 1 →
   `notifications/initialized` → `tools/call`) using the same env the config will pass.
   macOS has no `timeout`; hold stdin open with a subshell instead:
   `(echo "$INIT_JSON"; sleep 10) | ENV=... npx -y <pkg>@<ver> 2>err.log | head -c 300`.
   The opensearch server's stderr also logs `Enabled tools: [...]` — a quick way to see defaults.
   If the MCP tools are already registered in this session, just call them (`tool_call`) instead.
4. **Apply to the running app.**
   - Desktop has **no `/reload-mcp`** slash command. Reload happens when saving in
     Capabilities → MCP, or on full app restart (⌘Q, relaunch). The background reconciler only
     adds/removes servers by *name*; a changed config for an existing name is not reloaded.
   - CLI/TUI: `/reload-mcp`.
   - **The messaging gateway runs its own MCP server processes**, separate from the desktop app's.
     An app restart leaves them on the old config. Check the parent PID of each MCP process. If it is
     `hermes gateway run`, also run `hermes gateway restart`. This restart kills running gateway
     agents, so it goes through approval.
   - **Run gateway commands with the user's launcher (`~/.local/bin/hermes`), not bare `hermes`.**
     In the agent terminal, `hermes` can resolve to a different install copy
     (`~/.hermes/installs/.../venv/bin/hermes`). `gateway restart/start` rewrites
     `~/Library/LaunchAgents/ai.hermes.gateway.plist` for whichever install runs it, so the wrong copy
     leaves a launchd job that keeps exiting (`last exit code = 1`). First check that
     `hermes --version` shows `Install directory: ~/.hermes/hermes-agent`. If the plist already got
     broken, re-run `~/.local/bin/hermes gateway start`. That rewrites the plist back.
   - Verify the gateway: `launchctl print gui/$(id -u)/ai.hermes.gateway | grep -E 'state|last exit'`
     should show `running` and `(never exited)`. `ps` should show fresh MCP children under the new
     gateway PID.
   - In replies and in repo docs/guides, give the desktop path (restart or Capabilities → MCP save)
     and mark `/reload-mcp` as CLI/TUI-only — the user runs the desktop app, so a bare
     "/reload-mcp" instruction sends them to a command that doesn't exist there.
   - Opening a new chat does **not** reload changed config: it only shows the tool list of the
     server processes already running. Never tell the user "open a new chat to apply". Tell them to
     restart the app or save in Capabilities → MCP, then open a new chat to see the result.
   - The current conversation keeps its original tool list. When asked to "query via the MCP in this conversation", don't stop at "not available": run the
     same server with the same env through a raw stdio probe (step 3), show the real result, and say
     that's what you did and that a new conversation will have the tools natively.
5. **Verify the reload**: `ps -axo pid,lstart,command | grep <pkg>` (new process, old one gone) and
   `grep tools.mcp_tool ~/.hermes/logs/agent.log | tail` → `registered N tool(s)` with expected names.
   In a fresh chat, the quickest check is the deferred tool catalog. `tool_search`
   returns `total_available`, and the count drops by 4 once prompts/resources are gone. Also,
   `tool_describe` on the four names should return `not_found`. If
   `mcp__<server>__list_prompts`/`get_prompt`/`list_resources`/`read_resource` still appear after you
   set `prompts/resources: false`, the old process is still serving, so the reload did not happen.
   Report that instead of the test results as if they reflect the new config.
6. **Commit the config repo**: `git log` first (other sessions may have committed), `git diff`,
   `git grep -nI -E 'eyJ|password|sk-'`, commit, push.

## Config repo (KOA = profile distribution)

- The koa repo is a Hermes profile distribution (`distribution.yaml`, `SOUL.md`, `config.yaml`,
  `skills/`, `koa/`). Install: `hermes profile install github.com/leejw1212/koa --alias`; update:
  `hermes profile update koa` (keeps the installed `config.yaml` unless `--force-config`).
- `hermes config set` in the installed profile changes only that machine. To ship an MCP change to
  every machine, edit the repo's `config.yaml` and `koa/catalog.yaml`, commit, push, then
  `hermes profile update koa --force-config` (re-register local MCPs after that, or keep them in the
  repo's config).
- Before a history rewrite (force push), save `git bundle create <backup>.bundle --all`. Force push
  does not purge old commits from GitHub (still fetchable by SHA); recommend private visibility or
  repo recreation if old content matters.

## Pitfalls

- Killing a removed server's orphaned processes: stdio MCP children of a previous backend can
  survive; check `ps` for stale `npm exec`/`uvx` processes after removing a server.
- Exposing a backend for an MCP (Ingress/NodePort): check whether the namespace is GitOps-managed
  and whether the exposure is reachable beyond localhost before applying anything; ask the user to
  pick auth/exposure when the backend has no auth. Recipe: `references/opensearch.md`.
- `${HOME}` in config may stay literal (resolved from the secret scope); use the `${userHome}`
  context variable for paths.
