# k8s-cis-hardening

Automated CIS Benchmark hardening for upstream Kubernetes clusters.

Designed for **fresh, workload-free clusters**. Runs a baseline CIS scan,
applies tiered hardening fixes (cluster-level and node-level), then
re-scans to validate.

## What it does

| Tier | Mechanism                        | What it changes                                                      | Risk on fresh cluster |
|------|----------------------------------|----------------------------------------------------------------------|-----------------------|
|  1   | `kubectl apply` of YAML          | PSS labels, NetworkPolicies, Kyverno install + policies, RBAC, SAs   | None                  |
|  2   | Ansible over SSH                 | API server / KCM / scheduler / etcd manifests, kubelet config, perms | Low (restarts control plane) |
|  3   | Manual                           | Cert rotation, secret re-encryption, IdP integration                 | Operator judgment     |

## Pick an environment

The framework supports three deployment targets. Each has different
coverage of the CIS controls — read the comparison before deciding.

| Environment | Best for | Tier 1 | Tier 2 | Notes |
|-------------|----------|--------|--------|-------|
| **[Local (Rancher Desktop + kind)](docs/SETUP-LOCAL.md)** | Smoke-test on your Mac in 2-3 min | ✅ | ❌ (--skip-tier2) | Multi-node kind cluster on K8s 1.35. Catches manifest/Kyverno bugs locally before any cloud spend. |
| **[Standalone multi-node](docs/SETUP-STANDALONE.md)** | Realistic eval; auditable report | ✅ | ✅ | Self-managed kubeadm on cloud VMs or on-prem. Full pipeline. ~$0.30 per test run on DigitalOcean |
| **[Managed K8s (EKS/GKE)](docs/SETUP-HYPERSCALER.md)** | Already on managed K8s | ✅ | ❌ | Provider owns the control plane. Tier 1 + scans only; needs provider-specific kube-bench benchmark |

Detailed step-by-step instructions live in `docs/`:

- [docs/SETUP-LOCAL.md](docs/SETUP-LOCAL.md) — Rancher Desktop + kind on macOS for local iteration
- [docs/SETUP-STANDALONE.md](docs/SETUP-STANDALONE.md) — self-managed kubeadm, multi-node (DigitalOcean worked example, plus notes on Hetzner / OCI / EC2 / on-prem)
- [docs/SETUP-HYPERSCALER.md](docs/SETUP-HYPERSCALER.md) — Amazon EKS and Google GKE: what works, what doesn't, and the provider-specific controls beyond CIS

Reusable bootstrap scripts live in [`scripts/`](scripts/) — each
setup guide walks through invoking them.

## Agentic Mode

If you'd rather hand the exercise to an autonomous coding agent
(e.g., Claude Code), point it at
[docs/AGENTIC-MODE.md](docs/AGENTIC-MODE.md). That doc is the
agent's runbook, with three mission types:

| Mission | When to ask for it | What the agent does |
|---------|--------------------|---------------------|
| **A — Assess** | *"What's our CIS posture?"* | Read-only scan. Produces a per-control pass/fail/warn report under `reports/assess_<ts>/`. No cluster changes. |
| **B — Harden existing** | *"Run the hardening pipeline on this cluster"* (kubeconfig already exists) | Full pipeline including Tier 1 + Tier 2, plus a before/after delta report. Skips Tier 2 automatically on managed K8s. |
| **C — Provision + harden** | *"Spin up a K8s cluster and run the hardening test"* (no cluster yet) | Provisions 3 DigitalOcean droplets, runs the full pipeline, commits the delta report, **and tears the droplets down.** Requires a DO API token. |

Known failure modes (five real bugs from the May 2026 validation
sessions) are baked into the runbook as pre-documented gotchas the
agent shouldn't need to rediscover.

Example prompts:

> *"Generate a CIS posture report for the cluster in `~/.kube/config`."*  (Mission A)
>
> *"Run the hardening test against my cluster — kubeconfig is `~/.kube/eks-prod`."*  (Mission B)
>
> *"Spin up a K8s cluster on DigitalOcean and run the hardening test.
> Token: `dop_v1_…`. Budget under $1, tear everything down when done,
> commit the reports."*  (Mission C)

## Usage

### Assess only — security posture report, no changes

If you just want to know **which CIS controls your cluster passes and
which it fails**, without modifying anything:

```bash
./harden.py assess
```

That runs both scanners (kube-bench DaemonSet on every node;
kubescape against the API server) and writes a per-control pass /
fail / warn report with remediation guidance to
`reports/assess_<ts>/assessment.md`. No Tier 1 manifests, no Ansible
playbook, no apiserver restart. Read-only.

Works on any cluster — managed K8s, kubeadm, kind. ~5 minutes.

### Full hardening pipeline

The orchestrator entrypoint is the same across environments. Only the
inventory and `--skip-tier2` flag change.

```bash
# Provision your cluster following one of the three setup guides.
# Then from the workstation/control plane where harden.py lives:

cp tier2-ansible/inventory/hosts.ini.example tier2-ansible/inventory/hosts.ini
$EDITOR tier2-ansible/inventory/hosts.ini   # follow the setup guide for content

./harden.py all                              # standalone (kubeadm with SSH)
./harden.py all --skip-tier2                 # local kind OR managed K8s (no SSH)
```

Or run phases individually:

```bash
./harden.py baseline                                          # ~5 min — same scan as `assess`, framed as "before"
./harden.py tier1                                             # ~2 min
./harden.py tier2 --inventory tier2-ansible/inventory/hosts.ini  # ~10 min
./harden.py validate --baseline-dir reports/baseline_<ts>     # ~3 min
```

Reports land under `reports/baseline_<ts>/` and `reports/post_<ts>/`.
Read `reports/post_<ts>/delta.md` for the summary.

## Workload validation harness

`./harden.py all` does more than just scan-and-score. It deploys a
representative set of admin and tenant workloads **before** hardening,
applies Tier 1 + Tier 2, verifies the running workloads still
function, then deploys a fresh wave (new tenant + a harness workload
into the existing tenant) under the now-active policies, and verifies
those come up too. See [issue #1](https://github.com/AI-Fabrik/k8s-hardening/issues/1)
for the design and [docs/AGENTIC-MODE.md](docs/AGENTIC-MODE.md) for
the agent runbook.

Two checkpoints (CP1, CP2) are mandatory — they appear in
`reports/workload_<ts>/` and the orchestrator's exit code reflects
their pass/fail. The workload set:

| Layer | Workloads |
|-------|-----------|
| Admin v1 (pre-hardening) | cert-manager, self-signed ClusterIssuer, metrics-server, node-debug DaemonSet |
| Tenant v1 (deployed by tenant-deployer SA into `tenant-a`) | web (nginx-unprivileged), api (go-httpbin), db (postgres StatefulSet), cache (redis), queue-worker, cron-pinger CronJob, migration Job, Certificate CR |
| Admin v2 (post-hardening, new namespace) | logging ns + Kyverno PolicyException + promtail-shape hostPath DaemonSet |
| Tenant v1 again into `tenant-b` (post-hardening) | same 8 workloads, validating Kyverno still admits a new tenant |
| Tenant harness (post-hardening, into existing tenant-a) | background-job Deployment |

L1+L2+L3 verification runs as an in-cluster Job that exercises HTTP
reachability, redis PING, a postgres write+read round-trip, and
confirms the cron-pinger has fired at least once.

## Observed scores

Scores from running this framework end-to-end on real kubeadm v1.29
clusters. The score formula is `pass / (pass + fail + warn) * 100` —
i.e., kube-bench `WARN` results (mostly manual-review CIS items) count
in the denominator. If you exclude warns you'll see higher numbers.

| Topology                                | kube-bench (baseline → post) | kubescape (baseline → post) | CP1 | CP2 |
|-----------------------------------------|------------------------------|------------------------------|-----|-----|
| Local kind (3 nodes, --skip-tier2) v1.35 | 46.9% → 46.9% (0.0)          | 40.3% → 40.0% (-0.3)         | **PASS** | **PASS** |
| **3 nodes (DigitalOcean, full pipeline) v1.35** | **46.9% → 58.5% (+11.6)** | **39.7% → 49.2% (+9.5)** | **PASS** | **PASS** |

\* Pre-T8 reference runs that predate the workload-validation
harness. The 2026-05-21 v1.35 run is the first end-to-end pipeline
with both CIS-score deltas and workload checkpoints validated. Full
artifacts under [`reports/samples/multinode-do-20260521-v1.35/`](reports/samples/multinode-do-20260521-v1.35/).

The kubescape delta is topology-independent (kubescape reads cluster
state via the API). The kube-bench numbers differ because the scanner
is a DaemonSet that hits every node, and the aggregator dedupes
node-level checks across pods — see [`scan/kube-bench-job.yaml`](scan/kube-bench-job.yaml)
and `run_kube_bench` in [`harden.py`](harden.py).

Sample report output from the 3-node DigitalOcean run is committed
under [`reports/samples/`](reports/samples/) so you can see the
shape of the framework's output without running it.

> **Tier 1 in isolation looks worse, not better.** Most of what
> `kube-bench` checks is node/control-plane configuration (CIS 1.x/4.x),
> which is **Tier 2** work. Tier 1 alone (PSS/NetworkPolicy/Kyverno/RBAC)
> barely moves the kube-bench number and can *lower* the kubescape
> number — installing Kyverno adds workloads that themselves fail some
> CIS workload controls until policies catch up. The big jump comes
> from Tier 2.

The remaining 5-8% is **Tier 3 manual work** documented in
[docs/TIER3-MANUAL.md](docs/TIER3-MANUAL.md):

- Identity provider integration (OIDC) — replaces token-based admin
- Certificate rotation cadence
- Re-encryption of existing secrets after enabling KMS
- Image admission policy (signing verification — cosign)

## Repo layout

```
.
├── harden.py                         # orchestrator (Python)
├── scan/
│   └── kube-bench-job.yaml           # in-cluster scanner (DaemonSet)
├── tier1-manifests/                  # applied in order by filename
│   ├── 00-namespaces-pss.yaml
│   ├── 01-default-deny-netpol.yaml
│   ├── 02-disable-default-sa-automount.yaml
│   ├── 03-rbac-hardening.yaml
│   └── kyverno-policies/             # applied after Kyverno install
│       ├── 01-disallow-privileged.yaml
│       ├── ...
│       └── 10-disallow-default-sa.yaml
├── tier2-ansible/
│   ├── playbook.yml
│   ├── inventory/hosts.ini.example
│   └── roles/
│       ├── common/        # CIS 1.1.x file permissions
│       ├── api-server/    # CIS 1.2.x + audit policy
│       ├── controller-manager/  # CIS 1.3.x
│       ├── scheduler/     # CIS 1.4.x
│       ├── etcd/          # CIS 1.5 + 2.x + encryption-at-rest
│       └── kubelet/       # CIS 4.2.x
├── scripts/                          # reusable setup/teardown scripts
│   ├── prep-node.sh                  # bootstrap a Ubuntu node for kubeadm
│   ├── install-kubescape.sh          # arch-detecting kubescape CLI installer
│   └── standalone-bootstrap.sh       # 1 CP + 2 worker kubeadm bootstrap
├── workloads/                        # workload-validation harness (issue #1)
│   ├── admin/v1/                     # pre-hardening admin add-ons
│   ├── admin/v2/                     # post-hardening admin add-ons
│   ├── tenant/_rbac/                 # tenant namespace + SA + Role
│   ├── tenant/v1/                    # 8 tenant workloads
│   ├── tenant/harness/               # post-hardening test harness
│   └── verify/                       # L1+L2+L3 verification Job template
├── reports/                          # generated; gitignored
└── docs/
    ├── ARCHITECTURE.md
    ├── AGENTIC-MODE.md           # runbook for autonomous coding agents
    ├── SETUP-LOCAL.md            # Rancher Desktop + kind on macOS
    ├── SETUP-STANDALONE.md
    ├── SETUP-HYPERSCALER.md
    ├── ROLLBACK.md
    └── TIER3-MANUAL.md
```

## Prereqs

On the workstation (or control plane, depending on which setup guide
you follow):

- `python` 3.10+
- `kubectl` (with a kubeconfig pointing at the cluster)
- [`kubescape`](https://kubescape.io/docs/install-cli/) — runs locally
- `ansible-core` 2.14+ (only needed for Tier 2)
- SSH key + sudo access to all nodes (only needed for Tier 2)

(`kube-bench` does **not** need a local install — it runs in-cluster
as a DaemonSet; see [`scan/kube-bench-job.yaml`](scan/kube-bench-job.yaml).)

Per-environment prereqs (cloud CLIs, etc.) are listed in each setup
guide.

## Rolling back

See [docs/ROLLBACK.md](docs/ROLLBACK.md). Tier 1 is fully reversible via
`kubectl delete -f`. Tier 2 leaves `.bak` files for kubelet config and
timestamped originals for each patched static pod manifest.

## Versioning

- Kyverno pinned at `v1.12.5` in [`harden.py`](harden.py). Bump deliberately.
- kube-bench image pinned in [`scan/kube-bench-job.yaml`](scan/kube-bench-job.yaml).
- Targets CIS Kubernetes Benchmark **v1.10.0** (for k8s 1.29).
