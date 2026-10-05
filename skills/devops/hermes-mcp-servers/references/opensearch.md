# OpenSearch MCP (`opensearch-mcp-server-py`, run via `uvx`)

Known-good read-only block:

```yaml
  opensearch:
    command: uvx
    args: [opensearch-mcp-server-py]
    env:
      OPENSEARCH_URL: ${OPENSEARCH_URL}          # in ~/.hermes/.env
      OPENSEARCH_NO_AUTH: "true"                 # only when the security plugin is off
      OPENSEARCH_SETTINGS_ALLOW_WRITE: "false"   # server-side write block (default is true!)
    tools:
      include: [ListIndexTool, IndexMappingTool, SearchIndexTool, GetShardsTool, ClusterHealthTool]
      prompts: false
      resources: false
```

- **Core tools enabled by default**: ListIndex, IndexMapping, SearchIndex, GetShards, ClusterHealth,
  Count, Msearch, Explain, GenericOpenSearchApi. Always exclude `GenericOpenSearchApiTool` (any
  method/path). Count/Msearch/Explain are read-only and fine to add if asked.
- **Check whether auth exists** before asking for a read-only account: on k8s, read the pod env
  (`kubectl get pod <os-pod> -o jsonpath='{.spec.containers[0].env}'`, filter out passwords).
  `DISABLE_SECURITY_PLUGIN=true` means no users/roles exist → `OPENSEARCH_NO_AUTH`, and the cluster
  cannot enforce read-only; say so. With security on: `OPENSEARCH_USERNAME`/`OPENSEARCH_PASSWORD` in
  `.env` for an account with `readall` + `cluster_monitor`, and drop `OPENSEARCH_NO_AUTH`.
- **TLS**: private CA → `OPENSEARCH_CA_CERT_PATH`; `OPENSEARCH_SSL_VERIFY="false"` only as a last
  resort. mTLS: `OPENSEARCH_CLIENT_CERT_PATH`/`_KEY_PATH`.
- **Dynamic connection** (per-call `opensearch_url`) is off once `OPENSEARCH_URL` is set; leave it
  off for a single-cluster setup.
- **First `uvx` run downloads the package** and can exceed the default connect timeout; add
  `connect_timeout: 180` if the first connect times out.
- In-cluster OpenSearch (headless `svc/opensearch:9200`) needs a port-forward from the host; it dies
  with the session that started it — tell the user to keep their own or expose it (Ingress, below).
- Quick reachability check: `curl -s http://127.0.0.1:9200` and `/_cat/indices?v`.

## Exposing in-cluster OpenSearch via Ingress (nginx + Basic Auth)

Use when the security plugin is off and the user wants a stable URL instead of a port-forward.
Never expose it without auth: kind maps host port 80 on `0.0.0.0`, so the Ingress is reachable from
the whole LAN and an unauthenticated OpenSearch lets anyone delete indices — offer Basic Auth first.

1. **Find who owns the namespace.** `kubectl -n argocd get applications -o custom-columns=...`;
   if an Application with `selfHeal/prune` targets the namespace, add the Ingress manifest to that
   gitops repo (and its `kustomization.yaml`) and push — a `kubectl apply` would be reverted. Read
   the existing manifest comments for author intent (e.g. "do not expose") and update them.
2. **Follow the existing Ingress convention** (`kubectl get ingress -A`): here `host: localhost`,
   class `nginx`, path prefixes with `rewrite-target: /$2` and `path: /opensearch(/|$)(.*)`.
3. **Create the auth Secret out-of-band** (never in git; ArgoCD does not prune resources it did not
   create):
   `kubectl -n logging create secret generic opensearch-basic-auth --from-literal=auth="hermes-readonly:$(openssl passwd -apr1 "$PW")"`
   Generate `$PW` randomly and write it only to `~/.hermes/.env` (chmod 600).
4. Annotations: `nginx.ingress.kubernetes.io/auth-type: basic`, `auth-secret: opensearch-basic-auth`,
   `auth-realm`. Force ArgoCD sync with
   `kubectl -n argocd annotate application <app> argocd.argoproj.io/refresh=hard --overwrite`.
5. **Verify**: no creds → 401, wrong password → 401, right → 200 on `/_cluster/health`; also hit the
   LAN IP with `-H 'Host: localhost'` → 401.
6. MCP env: `OPENSEARCH_URL=http://localhost/opensearch` (a path-prefixed URL works with the client),
   `OPENSEARCH_USERNAME`/`OPENSEARCH_PASSWORD` as `${VAR}`, drop `OPENSEARCH_NO_AUTH`, keep
   `OPENSEARCH_SETTINGS_ALLOW_WRITE: "false"`. Stop the now-unneeded port-forward.
7. Tell the user: Basic Auth is a gate, not read-only (the same creds can write via curl); plain
   http sends the password in clear on the LAN — use TLS for anything beyond a lab.
