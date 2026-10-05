---
name: kubernetes-agent-access
description: "Use when scoping Hermes access to a Kubernetes cluster."
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [kubernetes, kubectl, mcp, rbac, security, read-only, kind]
---

# Kubernetes access for the agent (scoped, read-only)

Covers: an MCP server (`mcp-server-kubernetes`) for queries, a ServiceAccount kubeconfig whose RBAC
only allows reads, and the terminal, which is a separate path to the same cluster.

## When to Use

- Adding or changing the kubernetes MCP server in Hermes config.
- The user asks whether the agent can create, delete or modify cluster resources, or wants it
  limited to read-only.
- Sharing a scoped kubeconfig, or blocking terminal `kubectl` writes.
- Checking cluster or namespace status through the MCP tools.

## Always-on rules

- **RBAC is the security boundary.** MCP `tools.include` and `ALLOW_ONLY_NON_DESTRUCTIVE_TOOLS` only
  decide which tools are exposed. Pair them with a ServiceAccount token that the API server limits
  to reads, so a config change or a different tool can't write.
- **Look up live cluster state only through the read-only SA**: the MCP tools
  (`kubectl_get`/`describe`/`logs`) or terminal `kubectl` with the read-only kubeconfig (the same
  file the MCP uses; confirm `kubectl config current-context` ends in `-readonly` first). Terminal
  kubectl is fine and often better, since `grep`/`jsonpath` can filter output that MCP returns raw.
  Never use `~/.kube/config` (admin) or `--kubeconfig` overrides for analysis. When asked which
  path you used, say which tool and which kubeconfig/context.
- **Analysis is read-only end to end.** Don't send test traffic (e.g. `curl` to an app or ingress)
  or change anything in the cluster to prove a hypothesis without asking first: it's a write to
  the system being diagnosed. Workflow for incidents and cluster onboarding (discovery → MCP plan):
  `cluster-incident-analysis`.
- **Never paste a token into chat.** Share a kubeconfig as `MEDIA:/path` with mode 600. Warn that
  the token doesn't expire and say how to revoke it.
- **Keep tokens out of git.** The config repo holds only the RBAC manifest and the generator
  script. Before pushing, run `git grep -nI eyJ` to catch a JWT.
- Hermes config here is a git repo behind a symlink. Edit it with `hermes config set`, then
  `git diff`, commit and **push** when the user asks.

## Procedure

1. **Inspect.** Check the current `mcp_servers.kubernetes` block, `kubectl config current-context`,
   and `kubectl get nodes`.
2. **Create the SA and kubeconfig.** Copy `templates/readonly-rbac.yaml` to
   `<repo>/k8s/hermes-readonly.yaml` and `scripts/make-readonly-kubeconfig.sh` beside it, then run:
   `k8s/make-readonly-kubeconfig.sh <admin-context>` → `~/.kube/hermes-readonly.yaml`.
   - The `view` ClusterRole has no Secrets, exec or port-forward, and no cluster-scoped resources.
     Add a small ClusterRole for nodes, persistentvolumes and storageclasses, or `get nodes` fails.
   - The token Secret (`kubernetes.io/service-account-token`) is filled in asynchronously, so poll
     until `.data.token` exists.
3. **Verify RBAC before wiring it in.**
   ```bash
   RO=~/.kube/hermes-readonly.yaml
   for v in "list pods" "get nodes" "delete pods" "create deployments" "patch deployments" "create pods/exec" "get secrets"; do
     printf '%-22s %s\n' "$v" "$(kubectl --kubeconfig $RO auth can-i $v -A)"; done
   kubectl --kubeconfig $RO -n default delete pod <any-pod> --dry-run=server   # expect Forbidden
   ```
4. **Point the MCP at it.**
   `hermes config set mcp_servers.kubernetes.env.KUBECONFIG_PATH '${userHome}/.kube/hermes-readonly.yaml'`
   - Hermes passes a **filtered env** to stdio MCP children. A kubeconfig setting has to go in the
     server's `env:` block. A shell `KUBECONFIG` is not passed through.
   - Use `${userHome}`, a Hermes context variable that always resolves. `${HOME}` is resolved from the
     secret scope and may be left as a literal.
   - The server picks credentials in this order: `KUBECONFIG_YAML` > `KUBECONFIG_JSON` >
     `K8S_SERVER`+`K8S_TOKEN` > in-cluster > `KUBECONFIG_PATH` > `KUBECONFIG` > default. Any
     higher-priority var that is set overrides `KUBECONFIG_PATH`.
5. **Test the real MCP server.** If the kubernetes MCP tools are registered in this session, call
   `kubectl_get` for `pods` and for `secrets`; otherwise use the raw stdio probe in
   `hermes-mcp-servers` (Procedure step 3) with `KUBECONFIG_PATH` set. Expect pods listed and
   `secrets` Forbidden for `system:serviceaccount:hermes:hermes-readonly`. The SA name in the
   error is the proof the server uses the scoped kubeconfig, not admin.
6. **Update the repo.** Have `install.sh` warn when the kubeconfig is missing (don't run against a
   cluster automatically). Add a README section covering generation, revocation (delete the token
   Secret, re-run the script) and a `can-i` check. Commit and push.
7. **Tell the user** that the running app keeps the old MCP connection until it reloads. The desktop
   app has no `/reload-mcp`: restart the app, and if the user runs the messaging gateway, restart it
   too. A new chat alone does not reload. CLI/TUI use `/reload-mcp`. Verify the reload, and follow
   the generic MCP wiring rules (string env values, `prompts/resources: false`, tool-name discovery,
   gateway restart) in `hermes-mcp-servers`.
8. **Run the acceptance suite** (`references/validation-tests.md`): 4 reads must succeed, and delete
   and secrets must fail. Then check the terminal path separately.

## Blocking terminal kubectl

The terminal runs as the user, so it can read `~/.kube/config` (admin). Before changing anything,
measure the gap with read-only checks: `kubectl config current-context; kubectl auth can-i delete pods -A;
kubectl auth can-i list secrets -A`. The options below go from weakest to strongest.

0. **Default `KUBECONFIG` to the read-only file**, so that ordinary `kubectl` in the agent terminal
   uses the SA:
   - Create `<repo>/terminal/agent-env.sh` with `export KUBECONFIG="$HOME/.kube/hermes-readonly.yaml"`.
   - Set `hermes config set terminal.shell_init_files '["~/.profile","~/.bash_profile","~/.bashrc","<profile-dir>/terminal/agent-env.sh"]'`
     (KOA profile: `~/.hermes/profiles/koa/terminal/agent-env.sh`; shipped in its `config.yaml`).
   - An explicit list **turns off** the automatic sourcing of the three rc files
     (`auto_source_bashrc` applies only when the list is empty), so keep them in the list.
   - The file is sourced when a session builds its terminal env snapshot. A new chat picks it up;
     the current chat keeps its old env. To verify from the current chat, reproduce the real path:
     run `tools.environments.local._resolve_shell_init_files()` and `_prepend_shell_init()` with
     `~/.hermes/hermes-agent/venv/bin/python`, then `bash -l -c` the `can-i` checks. Expect context
     `*-readonly` and `no` for delete and secrets.
   - The path is absolute; if the profile is installed under another name, adjust it.
   - This only changes the default. It is **not a boundary**: `--kubeconfig ~/.kube/config` still
     gets admin. Say so in docs and replies.
1. **`approvals.deny`** blocks fnmatch globs even under `--yolo`:
   `hermes config set approvals.deny '["kubectl *delete*","kubectl *apply*","kubectl *create*","kubectl *edit*","kubectl *patch*","kubectl *scale*","kubectl *exec*","helm *"]'`.
   It only matches strings, so `curl` to the API or a client library gets around it. It prevents
   accidents, not misuse.
2. **Docker terminal backend.** Set `terminal.backend: docker` and mount only the read-only
   kubeconfig with `docker_volumes`, then point `docker_env.KUBECONFIG` at it. The admin credentials
   are never visible inside. For kind, `127.0.0.1:<port>` doesn't work from the container: rewrite it
   to `host.docker.internal` or join the `kind` network.
3. **Turn off the terminal toolset** for the profile (`hermes tools`). This is the strongest
   option, but it removes every other shell task too.

Recommend 0 (and 1) now, and 2 when the user needs a hard guarantee. Do what the user picks; don't stack extra layers.

## Pitfalls

- **MCP stdio probe:** wait for the `initialize` response (id 1) before sending
  `notifications/initialized` and `tools/call`. If you send everything at once, the server may never
  answer.
- **kind:** all nodes share the host kernel, so one Docker or Mac restart shows up as `Rebooted`
  events with the same boot id on every node, plus pod `SandboxChanged` restarts. Report it as a
  host restart, not as a node fault.
