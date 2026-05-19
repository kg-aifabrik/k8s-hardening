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

## Repo layout

```
.
├── harden.py                         # orchestrator (Python)
├── scan/
│   └── kube-bench-job.yaml           # in-cluster scanner Job
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
├── reports/                          # generated; gitignored
└── docs/
    ├── ARCHITECTURE.md
    ├── TIER3-MANUAL.md
    └── ROLLBACK.md
```

## Prereqs

On the workstation running `harden.py`:

- `python` 3.10+
- `kubectl` (with kubeconfig pointing at the cluster)
- `kubescape` ([install](https://kubescape.io/docs/install-cli/)) — runs locally
- (kube-bench needs **no** local install — it runs in-cluster as a Job; see `scan/kube-bench-job.yaml`)
- `ansible-core` 2.14+
- SSH key + sudo access to all nodes (for Tier 2)

On the cluster:

- Upstream Kubernetes v1.28+
- kubeadm-installed (Tier 2 assumes static pod manifests in `/etc/kubernetes/manifests`)
- CNI installed and healthy
- No workloads beyond control plane

## Usage

### Quickstart - run everything

```bash
cp tier2-ansible/inventory/hosts.ini.example tier2-ansible/inventory/hosts.ini
$EDITOR tier2-ansible/inventory/hosts.ini
./harden.py all
```

Outputs land under `reports/baseline_<ts>/` and `reports/post_<ts>/`.

### Step by step

```bash
# 1. Baseline scan (5-10 min)
./harden.py baseline

# 2. Apply cluster-level manifests (1-2 min)
./harden.py tier1

# 3. Apply node-level fixes (5-15 min, restarts control plane)
./harden.py tier2 --inventory tier2-ansible/inventory/hosts.ini

# 4. Validate
./harden.py validate --baseline-dir reports/baseline_20260515T120000Z
```

### Skip Tier 2

If you want to run Tier 1 only (e.g., quick iteration without touching nodes):

```bash
./harden.py all --skip-tier2
```

## Expected scores

On a vanilla `kubeadm init` v1.29 cluster:

| Phase             | kube-bench score | kubescape CIS score |
|-------------------|------------------|---------------------|
| Baseline          | ~50%             | ~55%                |
| After Tier 1      | ~70%             | ~75%                |
| After Tier 2      | ~92%             | ~94%                |

> **Note on Tier 1 in isolation:** most of what `kube-bench` checks is
> node/control-plane configuration (CIS 1.x/4.x), which is **Tier 2** work.
> Tier 1 alone (PSS/NetworkPolicy/Kyverno/RBAC) barely moves the `kube-bench`
> number and can *lower* the `kubescape` number, because installing Kyverno
> adds workloads that themselves fail some CIS workload controls. The big
> jump comes from Tier 2. Run the full pipeline before judging the score.

> **Testing on kind:** the orchestrator's `baseline`/`tier1`/`validate`
> phases work against a `kind` cluster, but **Tier 2 cannot run on kind**:
> it drives nodes over SSH and kind "nodes" are containers with no sshd.
> Validate Tier 2 patch scripts by `docker exec`-ing them into the
> `*-control-plane` container instead.

The remaining 5-8% is **Tier 3 manual work** documented in `docs/TIER3-MANUAL.md`:

- Identity provider integration (OIDC) - replaces token-based admin
- Certificate rotation cadence
- Re-encryption of existing secrets after enabling KMS
- Image admission policy (signing verification - cosign)

## Rolling back

See `docs/ROLLBACK.md`. Tier 1 is fully reversible via `kubectl delete -f`.
Tier 2 leaves `.bak` files for kubelet config and timestamped originals
for each patched static pod manifest.

## Versioning

- Kyverno pinned at `v1.12.5` in `harden.py`. Bump deliberately.
- kube-bench image pinned in `scan/kube-bench-job.yaml`.
- Targets CIS Kubernetes Benchmark **v1.10.0** (for k8s 1.29).
