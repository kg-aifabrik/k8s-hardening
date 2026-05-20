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

## Observed scores

Scores from running this framework end-to-end on real kubeadm v1.29
clusters. The score formula is `pass / (pass + fail + warn) * 100` —
i.e., kube-bench `WARN` results (mostly manual-review CIS items) count
in the denominator. If you exclude warns you'll see higher numbers.

| Topology                          | kube-bench (baseline → post) | kubescape (baseline → post) |
|-----------------------------------|------------------------------|------------------------------|
| Single node (Lima, untainted CP)  | 46.9% → 58.5% (+11.6)        | 46.4% → 50.2% (+3.8)         |
| 3 nodes (DigitalOcean, tainted CP)| 56.6% → 68.9% (+12.3)        | 46.4% → 50.2% (+3.8)         |

The kubescape delta is topology-independent (kubescape reads cluster
state via the API). The kube-bench numbers differ because the scanner
is a DaemonSet that hits every node, and the aggregator dedupes
node-level checks across pods — see [`scan/kube-bench-job.yaml`](scan/kube-bench-job.yaml)
and `run_kube_bench` in [`harden.py`](harden.py).

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

## Running on Oracle Cloud Free Tier (single node, ~$0)

The cheapest real (self-managed, kubeadm) cluster: one **Always Free
Ampere A1** VM. Tier 2 needs SSH+sudo to control-plane nodes, so managed
K8s and `kind` don't work — a throwaway single-node kubeadm box does.
One node covers all CIS targets (master/etcd/node/policies).

Everything below is arm64-clean: kube-bench (`aquasec/kube-bench`),
Kyverno, and the `kubescape` CLI all ship arm64 builds.

### 1. Provision the VM (OCI console)

- Compute → Instances → Create.
- Image: **Ubuntu 22.04**. Shape: **VM.Standard.A1.Flex**,
  Always Free eligible (use up to 4 OCPU / 24 GB; 2 OCPU / 12 GB is plenty).
- Add your **SSH public key**.
- Networking: keep the auto-created VCN; note the **public IP**.
- After boot, open the API/SSH ports (VCN → Security List, or run
  harden.py *on the node* so only port 22 is needed):
  ingress TCP `22` and `6443` from your IP.

### 2. Bootstrap a single-node kubeadm cluster

SSH in (`ssh ubuntu@<public-ip>`) and run:

```bash
# containerd + kubeadm/kubelet/kubectl (pin to a 1.29.x line)
sudo apt-get update && sudo apt-get install -y containerd apt-transport-https
sudo mkdir -p /etc/containerd && containerd config default \
  | sudo tee /etc/containerd/config.toml >/dev/null
sudo sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
sudo systemctl restart containerd
sudo swapoff -a
# (install kubeadm/kubelet/kubectl per kubernetes.io for v1.29; arm64 repo)

sudo kubeadm init --pod-network-cidr=10.244.0.0/16
mkdir -p ~/.kube && sudo cp /etc/kubernetes/admin.conf ~/.kube/config \
  && sudo chown $(id -u):$(id -g) ~/.kube/config

# single node: let it run workloads
kubectl taint nodes --all node-role.kubernetes.io/control-plane-
# a CNI (Flannel matches the pod-cidr above)
kubectl apply -f https://raw.githubusercontent.com/flannel-io/flannel/v0.25.1/Documentation/kube-flannel.yml
```

### 3. Run the hardening toolset *on the node*

Running it on the box itself means Ansible uses a local connection — no
extra SSH wiring, and only port 22 needs to be open.

```bash
sudo apt-get install -y python3 ansible-core
# kubescape CLI (arm64)
curl -s https://raw.githubusercontent.com/kubescape/kubescape/master/install.sh | /bin/bash

git clone https://github.com/kgajjala/k8s-hardening.git && cd k8s-hardening
cp tier2-ansible/inventory/hosts.ini.example tier2-ansible/inventory/hosts.ini
# point the inventory at localhost in BOTH groups (the playbook targets
# `control_plane` and `all` separately). Replace hosts.ini contents with:
#   [control_plane]
#   localhost ansible_connection=local
#
#   [workers]
#   localhost ansible_connection=local
#
#   [all:vars]
#   ansible_python_interpreter=/usr/bin/python3
./harden.py all --inventory tier2-ansible/inventory/hosts.ini
```

Reports land in `reports/`. `scp` them back, or read
`reports/post_*/delta.md` on the node.

### 4. Destroy

This framework targets fresh throwaway clusters — don't keep it. In the
OCI console, **terminate the instance** when done (Always Free has no
hourly cost, but leaving a hardened-then-abandoned box around is the
opposite of the point).

## Running on DigitalOcean (3 nodes, ~$0.30 for a test run)

OCI Always Free's ARM A1 capacity is heavily oversubscribed and `Out of
capacity` errors are common. DigitalOcean is the cleanest paid
alternative for a real multi-node test: 1 control plane + 2 workers
pro-rated hourly comes to a few cents per hour. This is also closer to
production than a single untainted node — Tier 2 has to drive Ansible
over real SSH, and the kube-bench DaemonSet has to handle a tainted
control plane.

### 1. Pick a region and create 3 droplets

In the DO console: **Create → Droplets**, all in the same region:

| Droplet | Plan                              | Hostname | Tags                    |
|---------|-----------------------------------|----------|-------------------------|
| `cp`    | Premium AMD, **2 vCPU / 4 GB**    | `cp`     | `k8s-cluster`, `cp`     |
| `w1`    | Premium AMD, **1 vCPU / 2 GB**    | `w1`     | `k8s-cluster`, `worker` |
| `w2`    | Premium AMD, **1 vCPU / 2 GB**    | `w2`     | `k8s-cluster`, `worker` |

All three should use the same Ubuntu 22.04+ image and the same SSH
key (`Settings → Security → SSH Keys` first if you haven't added one).
You can create `w1` and `w2` in one click with Quantity=2.

### 2. Cloud Firewall

**Networking → Firewalls → Create Firewall**. Name `k8s-fw`. Add
inbound rules:

| Type    | Protocol | Port      | Sources                |
|---------|----------|-----------|------------------------|
| Custom  | TCP      | 22        | your workstation IP    |
| Custom  | TCP      | 6443      | your workstation IP    |
| All TCP | TCP      | All ports | Tag: `k8s-cluster`     |
| All UDP | UDP      | All ports | Tag: `k8s-cluster`     |
| ICMP    | ICMP     | —         | Tag: `k8s-cluster`     |

Apply the firewall by typing tag `k8s-cluster` in **Apply to Droplets** —
it auto-attaches to all three droplets.

### 3. Bootstrap (run from your workstation)

```bash
# Replace with your droplet IPs.
CP=159.89.121.229; W1=159.203.25.165; W2=138.197.144.40

# Set proper hostnames + install kubeadm/containerd everywhere
for ip in $CP $W1 $W2; do scp <(bash <<'EOF'
# (paste the prep script - see git history of this repo's first OCI/DO run)
EOF
) root@$ip:/tmp/prep.sh; done
```

The repo doesn't ship a node-prep script — adapt the one in
`docs/ARCHITECTURE.md` (or `git log` this README for the
multi-node-DO bootstrap commands). In short, on each node:

```bash
# kernel modules + sysctls, containerd, kubelet/kubeadm/kubectl v1.29
# (see kubernetes.io official kubeadm install docs)
```

Then init the control plane and join workers:

```bash
ssh root@$CP "kubeadm init \
  --pod-network-cidr=10.244.0.0/16 \
  --apiserver-advertise-address=$CP \
  --apiserver-cert-extra-sans=$CP"
ssh root@$CP "cp /etc/kubernetes/admin.conf /root/.kube/config && \
  kubectl apply -f https://raw.githubusercontent.com/flannel-io/flannel/v0.25.1/Documentation/kube-flannel.yml"

JOIN=$(ssh root@$CP "kubeadm token create --print-join-command")
ssh root@$W1 "$JOIN"
ssh root@$W2 "$JOIN"
```

### 4. Hardening from the control plane

Tier 2 needs SSH from one host to all nodes. Simplest is to run the
whole pipeline *on the control plane*:

```bash
ssh root@$CP
apt-get install -y python3 ansible-core git
# install kubescape (x86_64):
LATEST=$(curl -sf https://api.github.com/repos/kubescape/kubescape/releases/latest | grep -m1 tag_name | cut -d\" -f4)
mkdir -p /root/.kubescape/bin
curl -sfL "https://github.com/kubescape/kubescape/releases/download/${LATEST}/kubescape_${LATEST#v}_linux_amd64.tar.gz" \
  | tar -xz -C /root/.kubescape/bin/ kubescape
ln -sf /root/.kubescape/bin/kubescape /usr/local/bin/kubescape

# Generate cp's SSH key and add it to w1 + w2
ssh-keygen -t ed25519 -N "" -f /root/.ssh/id_ed25519
CP_PUB=$(cat /root/.ssh/id_ed25519.pub)
ssh root@$W1 "echo '$CP_PUB' >> /root/.ssh/authorized_keys"
ssh root@$W2 "echo '$CP_PUB' >> /root/.ssh/authorized_keys"

git clone https://github.com/kg-aifabrik/k8s-hardening.git && cd k8s-hardening
cat > tier2-ansible/inventory/hosts.ini <<EOF
[control_plane]
cp ansible_host=$CP ansible_connection=local

[workers]
w1 ansible_host=$W1
w2 ansible_host=$W2

[all:vars]
ansible_user=root
ansible_ssh_private_key_file=/root/.ssh/id_ed25519
ansible_python_interpreter=/usr/bin/python3
ansible_ssh_common_args="-o StrictHostKeyChecking=no"
EOF

./harden.py all --inventory tier2-ansible/inventory/hosts.ini
```

Reports land under `reports/`. Numbers we observed on this exact
shape (3 droplets, vanilla kubeadm v1.29, May 2026):

| Tool       | Baseline | Post   | Delta  |
|------------|----------|--------|--------|
| kube-bench | 56.6%    | 68.9%  | +12.3  |
| kubescape  | 46.4%    | 50.2%  | +3.8   |

### 5. Destroy

Delete the three droplets and the firewall from the DO console. Per-hour
billing stops at delete.

## Rolling back

See `docs/ROLLBACK.md`. Tier 1 is fully reversible via `kubectl delete -f`.
Tier 2 leaves `.bak` files for kubelet config and timestamped originals
for each patched static pod manifest.

## Versioning

- Kyverno pinned at `v1.12.5` in `harden.py`. Bump deliberately.
- kube-bench image pinned in `scan/kube-bench-job.yaml`.
- Targets CIS Kubernetes Benchmark **v1.10.0** (for k8s 1.29).
