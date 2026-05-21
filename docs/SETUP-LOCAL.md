# Setup: Local (Rancher Desktop + kind)

Local development loop for iterating on the framework before
provisioning real cloud VMs. Uses **Rancher Desktop** for the
container runtime and **kind** for a multi-node Kubernetes cluster
inside Docker. Validated end-to-end on macOS arm64 — `./harden.py
all --skip-tier2` completes in ~2.5 minutes with all four workload
checkpoints PASS.

This is the *cheap* tier of the development pyramid. Use it before
spending money on DigitalOcean. See [SETUP-STANDALONE.md](SETUP-STANDALONE.md)
for the production-like multi-node DO setup.

## What this setup exercises

| Area                                                  | Covered |
|-------------------------------------------------------|---------|
| Tier 1 manifest application                           | ✅ |
| Kyverno install + policy admission                    | ✅ |
| Tenant RBAC + kubeconfig generation                   | ✅ |
| All 8 tenant workloads (web/api/db/cache/queue-worker/cron-pinger/migration/Certificate) | ✅ |
| Admin v2 + PolicyException                            | ✅ |
| L1+L2+L3 verify Job across 4 checkpoints              | ✅ |
| Multi-node CIS 4.x divergence (3 worker pods)         | ✅ |
| Image-existence preflight                             | ✅ |

## What this setup does NOT exercise

- **Tier 2 (Ansible over SSH).** kind "nodes" are containers without
  sshd; the framework's tier-2 patches can't apply. Run with
  `--skip-tier2`. To validate Tier 2 you need real VMs — see
  [SETUP-STANDALONE.md](SETUP-STANDALONE.md).
- **Real apiserver restart behavior.** kind's static pod handling is
  the same in principle but the timing windows we hit on production
  clusters (kubelet livenessProbe failures, aggregator races) rarely
  show up here.

The single most common gotcha caught by this loop: **Kyverno admission
policies vs. workload security context drift.** If a manifest doesn't
match the policies you'll find out in seconds locally, not 10 minutes
into a cloud run.

## Prereqs

- macOS (Apple Silicon or Intel)
- Homebrew

## 1. Install Rancher Desktop

```bash
brew install --cask rancher
open -a "Rancher Desktop"
```

On first launch:

- **Container Engine:** select **`dockerd (moby)`** (NOT `containerd`).
  kind requires a Docker-compatible socket; the default `containerd`
  mode is incompatible.
- **Kubernetes:** Rancher Desktop ships k3s by default. **Disable
  the bundled k3s** — we use `kind` instead for upstream kubeadm
  parity. This frees ~1 GB of VM RAM.
- **Virtual Machine → Resources:** bump to at least **4 CPU / 6 GB
  RAM**. The default 2 CPU / 6 GB is workable but slow; the kind
  pipeline tests above ran on a fresh Mac install with that floor.

Rancher Desktop installs its CLI tools (docker, kubectl, helm,
nerdctl) into `~/.rd/bin`. Add that to your shell PATH:

```bash
echo 'export PATH="$HOME/.rd/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

Confirm the Docker daemon is responsive:

```bash
docker version
# Server: Docker Engine - Community
#  Version:          29.x.x (or similar)
```

## 2. Install kind + the rest of the toolchain

```bash
brew install kind kubescape kube-linter python@3.11 ansible
```

`ansible` is only needed for the Tier 2 phase against real VMs. The
local kind loop runs with `--skip-tier2`, but installing it now means
you can switch to a DO run from the same workstation without
re-tooling.

## 3. Create a 3-node kind cluster

```bash
cat > /tmp/kind-config.yaml <<'EOF'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
  - role: worker
  - role: worker
EOF

kind create cluster --name k8s-harden-dev --config /tmp/kind-config.yaml --wait 180s
kubectl get nodes
```

Three nodes, K8s v1.35.x. Same version as the DO scripts install,
so the local loop and the production loop test the same K8s API.

## 4. Run the full pipeline

```bash
./harden.py all --skip-tier2
```

Expected wall time: ~2-3 minutes. Expected output:

```
CHECKPOINT 1 (existing tenant survives hardening): PASS
CHECKPOINT 2 (new tenant + harness post-hardening): a=PASS b=PASS
```

Reports under `reports/baseline_<ts>/`, `reports/post_<ts>/`,
`reports/workload_<ts>/`.

You can also drive individual phases for tighter iteration:

```bash
./harden.py check-images
./harden.py workload-deploy --kind admin   --version v1
./harden.py create-tenant   --tenant tenant-a
./harden.py workload-deploy --kind tenant  --version v1 \
    --tenant tenant-a --kubeconfig reports/kubeconfig-tenant-a.yaml
./harden.py workload-verify --namespace tenant-a
```

## 5. Tear down

```bash
kind delete cluster --name k8s-harden-dev
```

Rancher Desktop's VM stays running between sessions; you don't need
to quit the app unless you're freeing RAM.

## When local passes, promote to DO

The kind cluster catches most workload-level bugs (manifest typos,
Kyverno admission misses, image-tag drift, PVC binding issues) but
can't catch:

- Tier 2 Ansible playbook bugs against real apiserver/kubelet
- Real apiserver restart timing
- CIS score deltas that only Tier 2 can produce

Once `./harden.py all --skip-tier2` is green locally, follow
[SETUP-STANDALONE.md](SETUP-STANDALONE.md) for the DigitalOcean
3-droplet run with the full Tier 2 enabled.
