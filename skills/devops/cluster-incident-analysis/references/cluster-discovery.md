# Cluster discovery → MCP install (KOA first action)

When a cluster is connected, KOA first builds a **cluster profile** (our own format,
`koa.cluster-profile/v1`, unrelated to Hermes profiles or kubeconfig contexts), then decides which
MCP servers to attach. Code lives in `$HERMES_HOME/koa/`; doc `docs/koa-cluster-discovery.md`. Operating flow: `koa-cluster-discovery` skill.

```bash
cd "$HERMES_HOME"
python3 koa/discover.py --probe          # -> local/clusters/<name>.yaml (~1-2 s)
python3 koa/plan.py <name>               # plan only, changes nothing
python3 koa/plan.py <name> --apply       # register verified servers via ~/.local/bin/hermes config set
python3 koa/plan.py <name> --apply --with argocd   # candidates must be named explicitly
```
After `--apply`: restart desktop app (+ gateway if used).

## What discovery records
- k8s version, provider (node `providerID` scheme), runtime, node/pod counts, identity (`auth whoami`).
- Permissions: needed reads (pods, pods/log, events, nodes, services, ingresses, deployments,
  configmaps, nodes.metrics) and must-be-denied (secrets, pods/exec, pods/portforward,
  services/proxy, create/delete pods, patch deployments). Any allowed → `read_only: false`; plan refuses.
- Components by **container image regex + CRD API group** (`koa/catalog.yaml`, 23 kinds:
  metrics, logs, shippers, traces, gitops, MQ, cache, ingress). Per instance: ns/workload, image,
  pods/ready, selecting services + ports, external access URL from Ingress, `--probe` health GET result.
- Event retention from kube-apiserver `--event-ttl`; gaps list (no metrics, no alerting, short TTL,
  no external access URL, shipper self-health unknown, ...).

## Install gating (`catalog.yaml`)
- `verified` = attached in the lab, tool list and write-denial confirmed → `--apply` registers it.
- `candidate` = read-only switches known from README only → needs `--with <name>`.
  Per-server read-only switches: `hermes-mcp-servers` → `references/ops-mcp-catalog.md`.
- `requires_env` checked for presence in `$HERMES_HOME/.env` (never print values); missing → TODO with
  the profile's discovered URL as suggested value.
- Promote candidate → verified only after: read-only account issued, attached, tool list matches
  `include`, a write attempt is refused, then flip `status` and commit.

## Implementation pitfalls (apply to any discovery script)
- **`kubectl auth can-i VERB res/sub` means a resource *named* `sub`**, not a subresource
  (`get services/proxy` → yes for a read-only SA). Split and pass `--subresource sub`.
- **Don't infer event retention from the oldest remaining event** — events outlive the TTL after a
  laptop sleep/docker pause. Read `--event-ttl` from the `component=kube-apiserver` pod command
  (absent → 1h). Managed clusters hide the apiserver pod → record "unknown".
- "No external access" must be per component (all instances lack a URL), not per instance —
  multi-workload components (argocd) otherwise get flagged wrongly.
- Refuse to run with `~/.kube/config`; resolve paths before comparing.
- Ingress regex paths (`/x(/|$)(.*)`) → strip to the prefix before building URLs.
- Probe only observability health paths (`/-/healthy`, `/api/health`, `/healthz`, `/`), never app paths.
- Re-run discovery on a later day and diff the profile: time-dependent fields expose heuristic bugs.
- macOS system python is 3.9 — keep KOA scripts free of 3.10+ syntax (`match`, `X | Y` type unions).

## Known limits
SaaS observability (Datadog, Grafana Cloud) is invisible from the cluster; only Ingress URLs are
found (not LB/NodePort/VPN); mirrored image names need extra regexes; log index fields are not yet
profiled. Company-cluster profiles contain internal hostnames — decide before committing `clusters/`.
