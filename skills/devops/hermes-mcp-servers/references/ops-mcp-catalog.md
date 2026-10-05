# Read-only switches for common ops MCP servers

Use when adding an ops MCP beyond kubernetes/opensearch. Each server needs **its own read-only
switch + `tools.include` + a read-only backend account** — a whitelist alone only hides tools.
Status below = what was verified against the README; attach in a lab and try a write before trusting it.

| Server (pin) | Run | Read-only switch | Backend account | Notes |
|---|---|---|---|---|
| `prometheus-mcp-server` (pab1it0) | `uvx prometheus-mcp-server@<v>` | none needed (PromQL can't write) | basic/bearer via `PROMETHEUS_USERNAME/PASSWORD/TOKEN` | `PROMETHEUS_DISABLE_LINKS=True` saves tokens; tools: `execute_query`, `execute_range_query`, `list_metrics`, `get_metric_metadata`, `get_targets`. Thanos Query / vmselect URLs work (Prometheus API) |
| `mcp-grafana` | `uvx mcp-grafana@<v> --disable-write` | `--disable-write` (also drops raw-SQL query tools) | service account, Viewer role | one server covers Prometheus/Loki/ES datasources; many optional groups via `--enabled-tools` |
| `argocd-mcp` | `npx -y argocd-mcp@<v> stdio` | env `MCP_READ_ONLY="true"` | API token of a get/list-only role | include `list_applications`, `get_application`, `get_application_resource_tree`, `get_application_managed_resources`, `get_resource_events`; never `sync_/create_/delete_/run_resource_action` |
| `amq-mcp-server-rabbitmq` (amazon-mq) | `uvx amq-mcp-server-rabbitmq@<v> --v4 --tool-groups read observability health` | omit `--allow-mutative-tools` | user with `monitoring` tag | `RABBITMQ_MANAGEMENT_ENDPOINT=https://user:pass@host:443` (secret in URL → `.env` only). **SSRF guard blocks localhost/private IPs**, so it cannot reach a kind cluster on localhost |

No dedicated MCP worth attaching: fluentd/fluent-bit (use their Prometheus metrics), Loki (via Grafana MCP).
Find latest versions: `npm view <pkg> version`, PyPI JSON `https://pypi.org/pypi/<pkg>/json`.
READMEs: fetch `raw.githubusercontent.com/<org>/<repo>/main/README.md` with web_extract and grep the
saved full text for `read.?only|disable-write|mutative`.
