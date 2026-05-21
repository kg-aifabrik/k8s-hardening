# scripts/

Reusable shell scripts that automate the steps described in the
per-environment setup guides under [`docs/`](../docs/).

| Script | Where it runs | What it does |
|--------|---------------|--------------|
| [`prep-node.sh`](prep-node.sh) | On a target node (as root) | Set hostname, load kernel modules + sysctls, disable swap, install containerd + kubeadm/kubelet/kubectl v1.35. Idempotent. |
| [`install-kubescape.sh`](install-kubescape.sh) | On the host that will run `harden.py` (as root) | Download the kubescape CLI release binary and install to `/usr/local/bin`. Auto-detects OS (Linux/macOS) + arch (amd64/arm64). |
| [`lima-up.sh`](lima-up.sh) | macOS workstation | Full Lima single-node provisioning: launch VM, run `prep-node.sh`, `kubeadm init`, install Flannel, set up tooling, write inventory. |
| [`lima-down.sh`](lima-down.sh) | macOS workstation | Stop and delete the Lima VM. |
| [`standalone-bootstrap.sh`](standalone-bootstrap.sh) | Workstation with SSH to the 3 VMs | End-to-end bootstrap of a 1 CP + 2 worker kubeadm cluster on already-provisioned Ubuntu VMs (DigitalOcean, Hetzner, EC2, on-prem, etc.). |

## When to use which

- **First time on a Mac, want to try the framework:** use [`lima-up.sh`](lima-up.sh).
  Read [docs/SETUP-LIMA.md](../docs/SETUP-LIMA.md) for the caveats
  about what's actually exercised on a single node.
- **Realistic multi-node test on cloud VMs:** provision 3 Ubuntu
  VMs (any provider), then use [`standalone-bootstrap.sh`](standalone-bootstrap.sh).
  Read [docs/SETUP-STANDALONE.md](../docs/SETUP-STANDALONE.md) for
  provider-specific provisioning notes (DigitalOcean as the worked
  example, plus Hetzner / OCI / EC2 / on-prem).
- **Already on EKS or GKE:** there's no bootstrap script — your
  managed control plane was provisioned by the provider. Follow
  [docs/SETUP-HYPERSCALER.md](../docs/SETUP-HYPERSCALER.md), then
  use `install-kubescape.sh` on your workstation and run
  `./harden.py all --skip-tier2`.

## Quick usage

### Lima single-node (Mac)

```bash
brew install lima
bash scripts/lima-up.sh                  # provision + bootstrap, don't auto-run pipeline
RUN_HARDEN=1 bash scripts/lima-up.sh     # provision + bootstrap + ./harden.py all

# When you're done:
bash scripts/lima-down.sh
```

### Standalone multi-node (e.g., DigitalOcean)

After provisioning 3 Ubuntu droplets with SSH access (see
[docs/SETUP-STANDALONE.md](../docs/SETUP-STANDALONE.md)):

```bash
CP_IP=159.89.121.229 \
W1_IP=159.203.25.165 \
W2_IP=138.197.144.40 \
RUN_HARDEN=1 \
  bash scripts/standalone-bootstrap.sh
```

Drop `RUN_HARDEN=1` if you want to bootstrap the cluster but invoke
`./harden.py` manually later (useful for re-running just `validate`,
or for spreading the work across multiple terminals).

Once the script finishes, reports live on `cp` at
`/root/k8s-hardening/reports/`. Pull them back with:

```bash
scp -r root@$CP_IP:/root/k8s-hardening/reports ./reports-standalone
```

## Environment variables

All scripts honor these (defaults shown):

| Variable | Default | Used by |
|----------|---------|---------|
| `REPO_URL` | `https://github.com/kg-aifabrik/k8s-hardening.git` | `lima-up.sh`, `standalone-bootstrap.sh` |
| `FLANNEL_URL` | latest v0.25.1 manifest | both setup scripts |
| `K8S_POD_CIDR` | `10.244.0.0/16` (matches Flannel default) | `standalone-bootstrap.sh` |
| `RUN_HARDEN` | `0` | both setup scripts |
| `VM_NAME` | `k8s-harden` | Lima scripts |
| `SSH_KEY` | `~/.ssh/id_ed25519` | `standalone-bootstrap.sh` |

## What these scripts deliberately don't do

- **Provision VMs.** Cloud-provider provisioning is a one-shot,
  imperative process (point-and-click in the DO console, or
  `gcloud`/`aws`/`hcloud` CLI commands, or Terraform). Each provider
  is different enough that a shared script would either be very
  thin or very forked. The setup guides under [`docs/`](../docs/)
  walk through provisioning for each provider.

- **Tear down cloud VMs.** Same reason. `lima-down.sh` is the only
  teardown helper because Lima is uniform across Macs.

- **Manage hyperscaler clusters.** EKS/GKE provisioning belongs in
  your IaC (Terraform/Pulumi/eksctl). Once the cluster exists,
  `install-kubescape.sh` plus `./harden.py all --skip-tier2` is all
  you need from this repo.
