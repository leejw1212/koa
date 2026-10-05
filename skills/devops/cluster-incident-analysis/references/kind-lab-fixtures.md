# kind-lab fixtures — adding components to exercise discovery / MCP paths

Discovery and plan code paths only get tested when the lab actually has the component
(metrics stack, Grafana, non-read-only SA, ...). Installing them is a **lab change with admin
credentials**: do it only when the user asks, then re-run `koa/discover.py --probe` + `koa/plan.py`
and compare before/after (components, probes, gaps, plan items).

## How to install (when the user says "install X on kind")
- Install **directly into the kind cluster with helm + admin context** —
  `--kubeconfig ~/.kube/config --kube-context kind-lab`. Don't detour into finding the GitOps repo
  or Argo CD apps first; the user means the local cluster. Mention afterwards that it is not under
  GitOps if the cluster is otherwise Argo-managed.
- Keep values in the repo (`lab/<component>/values.yaml` in the koa repo), pin the chart version,
  never commit generated secrets (Grafana admin password stays in the chart Secret).
- Check node headroom first (`docker stats --no-stream | grep lab-`): kind nodes share the laptop's
  Docker memory, so set small requests/limits and short retention.
- Expose via ingress-nginx under a sub-path on `localhost` so discovery finds an external URL.
- Verify: pods Ready, health URLs 200, Prometheus `/api/v1/targets` all `up` (print `lastError`
  for any down target).

## kube-prometheus-stack on kind (known-good values)
Template in repo: `lab/monitoring/values.yaml` (chart `prometheus-community/kube-prometheus-stack`).
- `kubeEtcd/kubeScheduler/kubeControllerManager/kubeProxy: {enabled: false}` — kind binds them to
  127.0.0.1, so they are always `down`.
- Prometheus under `/prometheus`: `prometheusSpec.routePrefix: /prometheus` + `externalUrl`, ingress
  `paths: [/prometheus]`, `pathType: Prefix`. Health: `/prometheus/-/healthy`.
- Grafana under `/grafana`: `grafana.ini.server.root_url: "%(protocol)s://%(domain)s/grafana"`,
  `serve_from_sub_path: true`, **and `grafana.serviceMonitor.path: /grafana/metrics`** — otherwise
  the scrape of `/metrics` is redirected to the sub-path and the target goes `down`.
- `serviceMonitorSelectorNilUsesHelmValues/podMonitorSelectorNilUsesHelmValues/ruleSelectorNilUsesHelmValues: false`
  so later ServiceMonitors (app, fluentd, rabbitmq) without the chart label are scraped.
- `prometheus-node-exporter.hostRootFsMount.enabled: false` on Docker Desktop kind (rslave mount
  propagation unsupported).
- The stack has no metrics-server → discovery still reports "metrics API 없음"; that is correct.
- Ingress has no auth in the lab; say so when reporting.
