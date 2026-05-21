# Admin v1 — pre-hardening cluster bootstrap

Applied by the admin kubeconfig before Tier 1/2 run. Three workloads:

| File | Workload | Source |
|------|----------|--------|
| (URL) | metrics-server | Upstream `components.yaml`, pinned via `harden.py` constant `METRICS_SERVER_URL` |
| (URL) | cert-manager (CRDs + controller + webhook + cainjector) | Upstream `cert-manager.yaml`, pinned via `harden.py` constant `CERT_MANAGER_URL` |
| `10-selfsigned-clusterissuer.yaml` | self-signed `ClusterIssuer` | Local |
| `20-node-debug-ds.yaml` | per-node debug DaemonSet | Local |

`metrics-server` and `cert-manager` are vendored by URL rather than by
file because their manifests run to thousands of lines and the upstream
projects already publish pinned release URLs.
