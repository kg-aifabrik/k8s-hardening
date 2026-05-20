# Agentic Mode

This document is written for **a future Claude Code session (or other
autonomous coding agent)** asked to validate this framework end-to-end
against a Kubernetes cluster. The human's request will look like one
of:

- *"Run the hardening pipeline against this cluster"* (kubeconfig
  already exists)
- *"Spin up a K8s cluster on DigitalOcean and run the hardening test"*
  (no cluster yet)

Both paths are handled below. Read the relevant section, execute the
steps, report the delta, tear down what you provisioned. **Do not ask
the user for permission on each step** — they've delegated execution.
Do ask if you discover ambiguity in the *request*, not in the
*procedure*.

---

## Mission, success criteria, budget

| Item | Value |
|------|-------|
| **Mission** | Produce a `delta.md` report showing before/after CIS scores from a single end-to-end run of `./harden.py all`. |
| **Success** | `kube-bench` baseline → post delta is **at least +8 pts**, `kubescape` delta is **at least +2 pts**. Cluster reaches `Ready` on every node post-Tier 2. Reports are committed to git under `reports/samples/<topology>-<YYYYMMDD>/`. |
| **Soft budget** | Don't exceed **$1 in DO spend** without checking back. Premium Intel droplets (used for the May 2026 reference run) run at ~$0.083/hr for the 3-node trio; a clean run takes 30-45 min wall time. |
| **Hard requirement** | Tear down every droplet, firewall, volume, and SSH key entry you created. Even on failure. **Especially** on failure. |

## Inputs you need from the user

| Input | Required when | Notes |
|-------|---------------|-------|
| DigitalOcean API token | Path B (provision) | Personal Access Token with **Droplet, SSH key, Firewall** read+write. Don't `doctl auth init` (that writes the token to disk) — use `DIGITALOCEAN_ACCESS_TOKEN=<token> doctl …` inline per command. Remind the user to revoke at the end. |
| `KUBECONFIG` path | Path A (existing cluster) | Confirm it points where the user expects: `kubectl config current-context` + `kubectl get nodes`. |
| SSH key | Path B | Will use `~/.ssh/id_ed25519.pub` by default. Create with `ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519` if missing. |
| Workstation public IP | Path B | `curl -s https://ifconfig.me`. Goes into the firewall rules. **Re-fetch every session** — residential ISPs rotate. |

---

## Decision tree

```
User request mentions an existing cluster, KUBECONFIG, EKS/GKE/AKS, kubeadm cluster?
├── Yes → Path A (Existing cluster)
└── No  → Path B (Provision on DO)
```

If the user provides a DO token but also has a `KUBECONFIG` set, ask
once which they want — the procedures differ in important ways
(Path A skips Tier 2 if it's a managed cluster).

---

## Path A: Validate against an existing cluster

### A1. Identify the cluster type

```bash
kubectl version --short
kubectl get nodes -o wide
kubectl config current-context
```

Look at the node names / labels:

- **EKS / GKE / AKS** → managed K8s. Tier 2 cannot run.
  Use `./harden.py all --skip-tier2`. Set the kube-bench benchmark
  to the provider-specific one (see
  [SETUP-HYPERSCALER.md](SETUP-HYPERSCALER.md)).
- **kubeadm** (node names you control, SSH access available) →
  Tier 2 should work.
  Write a proper inventory and run `./harden.py all`.
- **Anything else** (k3s, OpenShift, kind) → check
  [SETUP-LIMA.md](SETUP-LIMA.md) for kind caveats. For others, prefer
  `--skip-tier2` and document the limitation.

### A2. Install pre-reqs locally

```bash
sudo bash scripts/install-kubescape.sh      # auto-detects arch
python3 --version                            # 3.10+
ansible-playbook --version                   # only if running Tier 2
kubectl version --short                      # client must reach the server
```

### A3. Run the pipeline

For **managed K8s** (EKS/GKE/AKS):

```bash
./harden.py all --skip-tier2
```

For **self-managed kubeadm** (with SSH access):

Write `tier2-ansible/inventory/hosts.ini` with one entry per node in
the right groups. Confirm Ansible reaches every host:

```bash
ansible -i tier2-ansible/inventory/hosts.ini all -m ping
./harden.py all --inventory tier2-ansible/inventory/hosts.ini
```

### A4. Report and commit

Same as Path B (see B7). **Do not** make changes to the user's
existing cluster beyond what `harden.py` does. **Do not** tear down
their cluster.

---

## Path B: Provision on DigitalOcean and validate

### B1. Pre-flight

```bash
# Verify tools
brew install --formula doctl       # macOS
doctl version
which kubectl ansible-playbook git || true

# Verify SSH key
[ -f ~/.ssh/id_ed25519.pub ] || ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
cat ~/.ssh/id_ed25519.pub

# Verify token
DIGITALOCEAN_ACCESS_TOKEN=<token> doctl account get
```

### B2. Ensure the SSH key is registered in DigitalOcean

```bash
DIGITALOCEAN_ACCESS_TOKEN=<token> doctl compute ssh-key list --format ID,Name,FingerPrint
```

If your key's fingerprint isn't there, import it:

```bash
DIGITALOCEAN_ACCESS_TOKEN=<token> doctl compute ssh-key import \
  agentic-mode --public-key-file ~/.ssh/id_ed25519.pub
```

Save the resulting ID — you'll need it at droplet-create time.

### B3. Create the firewall

```bash
MAC_IP=$(curl -s https://ifconfig.me)
DIGITALOCEAN_ACCESS_TOKEN=<token> doctl compute firewall create --name k8s-fw \
  --tag-names k8s-cluster \
  --inbound-rules "protocol:tcp,ports:22,address:${MAC_IP}/32 protocol:tcp,ports:6443,address:${MAC_IP}/32 protocol:tcp,ports:1-65535,tag:k8s-cluster protocol:udp,ports:1-65535,tag:k8s-cluster protocol:icmp,tag:k8s-cluster" \
  --outbound-rules "protocol:tcp,ports:1-65535,address:0.0.0.0/0,address:::/0 protocol:udp,ports:1-65535,address:0.0.0.0/0,address:::/0 protocol:icmp,address:0.0.0.0/0,address:::/0"
```

### B4. Choose a droplet size — gotcha

The Premium AMD slugs (`s-2vcpu-4gb-amd`, `s-1vcpu-2gb-amd`) are
**tier-restricted on new accounts** and fail with `422 This size is
currently restricted, please open a ticket to increase your account
tier`. Try Premium Intel first; fall back to Basic.

Decision order:
1. `s-2vcpu-4gb-intel` + 2x `s-1vcpu-2gb-intel` (~$0.083/hr)
2. `s-2vcpu-4gb` (Basic) + 2x `s-1vcpu-2gb` (Basic) (~$0.071/hr)

```bash
DIGITALOCEAN_ACCESS_TOKEN=<token> doctl compute droplet create cp \
  --image ubuntu-22-04-x64 --size s-2vcpu-4gb-intel \
  --region <pick one near you> --ssh-keys <SSH_KEY_ID> \
  --tag-names k8s-cluster,cp --wait \
  --format Name,PublicIPv4 &
DIGITALOCEAN_ACCESS_TOKEN=<token> doctl compute droplet create w1 \
  --image ubuntu-22-04-x64 --size s-1vcpu-2gb-intel \
  --region <same> --ssh-keys <SSH_KEY_ID> \
  --tag-names k8s-cluster,worker --wait \
  --format Name,PublicIPv4 &
DIGITALOCEAN_ACCESS_TOKEN=<token> doctl compute droplet create w2 \
  --image ubuntu-22-04-x64 --size s-1vcpu-2gb-intel \
  --region <same> --ssh-keys <SSH_KEY_ID> \
  --tag-names k8s-cluster,worker --wait \
  --format Name,PublicIPv4 &
wait
DIGITALOCEAN_ACCESS_TOKEN=<token> doctl compute droplet list --format Name,PublicIPv4,Status
```

Capture the three public IPs.

### B5. Run the bootstrap

```bash
CP_IP=<...> W1_IP=<...> W2_IP=<...> RUN_HARDEN=1 \
  bash scripts/standalone-bootstrap.sh > /tmp/agentic-run.log 2>&1
```

Run this in the background (it takes 15-30 min). Tail the log every
3-5 min:

```bash
grep -E "====|Error|FATAL" /tmp/agentic-run.log | tail -20
```

If the script exits non-zero, **read the section "Known gotchas"
below before assuming new bug**. Five separate failure modes were
already hit and fixed in earlier sessions; you may be re-encountering
one (e.g., on a different region) or a sixth.

### B6. Pull reports

When the script finishes successfully:

```bash
mkdir -p reports/samples/multinode-do-$(date -u +%Y%m%d)-premium-intel
LATEST_BASELINE=$(ssh root@$CP_IP 'ls -t /root/k8s-hardening/reports | grep baseline | head -1')
LATEST_POST=$(ssh root@$CP_IP 'ls -t /root/k8s-hardening/reports | grep ^post | head -1')
DEST=reports/samples/multinode-do-$(date -u +%Y%m%d)-premium-intel
mkdir -p "$DEST/$LATEST_BASELINE" "$DEST/$LATEST_POST"
scp root@$CP_IP:/root/k8s-hardening/reports/$LATEST_BASELINE/{baseline.md,scores.json} "$DEST/$LATEST_BASELINE/"
scp root@$CP_IP:/root/k8s-hardening/reports/$LATEST_POST/{delta.md,post-hardening.md,scores.json} "$DEST/$LATEST_POST/"
```

### B7. Commit reports

```bash
# Update reports/samples/README.md to add a row for the new sample.
# Then:
git add reports/samples/ docs/ scripts/ harden.py
git commit -m "Agentic-mode validation run on $(date -u +%Y-%m-%d): see reports/samples/..."
git push
```

If you fixed any bugs along the way, commit those separately first
with a focused message per bug (see commit `709b627` for an
example).

### B8. Tear down — **mandatory**

```bash
DIGITALOCEAN_ACCESS_TOKEN=<token> bash -c '
doctl compute droplet list --format ID --no-header | xargs doctl compute droplet delete --force
doctl compute firewall list --format ID --no-header | xargs doctl compute firewall delete --force
# Poll until droplets are gone (delete is async):
for i in 1 2 3 4 5 6; do
  COUNT=$(doctl compute droplet list --format ID --no-header | wc -l | tr -d " ")
  echo "Droplets remaining: $COUNT"
  [ "$COUNT" = "0" ] && break
  sleep 10
done
'
```

### B9. Tell the user to revoke the token

End your response with a literal line like:

> **Revoke your DO token at https://cloud.digitalocean.com/account/api/tokens — I won't need it again.**

Don't bury it in a paragraph.

---

## Known gotchas (don't waste cycles rediscovering)

From the May 2026 validation sessions. The scripts already work
around all five; this list is for diagnosing *new* environments
where one of these patterns might recur in a slightly different form.

| Gotcha | Symptom | Where it's handled |
|--------|---------|---------------------|
| Cold droplet SSH not ready | First SSH attempt: `connection refused` on port 22 | `wait_for_ssh` in `standalone-bootstrap.sh` |
| Cold droplet apt lock | `prep-node.sh` fails: `Could not get lock /var/lib/apt/lists/lock` | `cloud-init status --wait` at top of `prep-node.sh` |
| Bash arithmetic on IP keys | `prep-node.sh` runs with empty hostname argument | Indexed arrays `IPS=(...)` + `HOSTNAMES=(...)` in `standalone-bootstrap.sh` |
| `curl ... \| grep -m1` + `pipefail` | Script exits 23 silently mid-run | Materialize curl response before parsing in `install-kubescape.sh` |
| Kyverno admission webhook not ready post-Tier-2 | `validate` fails: `failed calling webhook ... connection refused` | `wait_for_kyverno()` in `harden.py` |

If you hit a **new** failure mode:

1. Reproduce it without rerunning the full pipeline (you have a
   partial cluster — use it).
2. Identify the smallest fix.
3. Apply, commit (focused commit message), re-run only the
   downstream phases.
4. Update this gotchas table.

---

## Failure modes and how to react

| Symptom | First thing to try |
|---------|---------------------|
| `doctl ... 422 size restricted` | Drop Premium AMD → Premium Intel → Basic, in that order. |
| Bootstrap script exits early, log ends mid-`apt-get` | Check `/tmp/prep-*.log` on the host — likely a transient apt mirror failure or cloud-init race. Re-run the script (idempotent). |
| `kubeadm init` errors with `Port 2379 in use` | The CP was already initialized. Re-run the script — idempotency check skips `init`. |
| `harden.py validate` fails with `connection refused` to the API | Apiserver still restarting. `wait_for_apiserver` should handle this — if it doesn't, the kubelet on cp may be stuck. SSH in, check `journalctl -u kubelet -n 50`. |
| kube-bench DaemonSet pods stuck in `ContainerCreating` | Stale `cni0` from a prior cluster on the same VMs. `ip link delete cni0; ip link delete flannel.1` on each node, then `kubectl delete pod -n kube-flannel --all`. |
| All three nodes pass Ready but scores didn't move | Tier 2 silently no-op'd. Check `ansible-playbook` output in the bootstrap log — every play should have `changed: ...` lines on first run. |

## What "done" looks like

You should be able to summarize the run in 6 lines:

1. Cluster topology (1 CP + 2 workers, kubeadm v1.29, DO Premium Intel)
2. Baseline scores (kube-bench %, kubescape %)
3. Post scores (kube-bench %, kubescape %)
4. Delta (+X.X, +X.X)
5. Reports committed at `reports/samples/...` (git SHA)
6. Droplets torn down, token-revocation reminder to user

If you can't produce all 6, you're not done — finish or hand back to
the human with a precise "I got to step N, here's what's left."
