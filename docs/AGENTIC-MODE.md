# Agentic Mode

This document is written for **a future Claude Code session (or other
autonomous coding agent)** asked to validate this framework or audit
a Kubernetes cluster's CIS posture. The human's request will look
like one of:

- *"Generate a security posture report for my cluster"* — **Mission
  type A (Assess)**. Read-only. No changes. See "Mission A" below.
- *"Run the hardening pipeline against this cluster"* — **Mission
  type B (Harden)**. kubeconfig already exists.
- *"Spin up a K8s cluster on DigitalOcean and run the hardening test"*
  — **Mission type C (Provision + harden)**. No cluster yet.

Read the relevant section, execute the steps, report results, tear
down what you provisioned. **Do not ask the user for permission on
each step** — they've delegated execution. Do ask if you discover
ambiguity in the *request*, not in the *procedure*.

---

## Mission, success criteria, budget

Per-mission success rubric:

| Mission | Success looks like |
|---------|--------------------|
| **A — Assess** | `reports/assess_<ts>/assessment.md` exists, lists pass/fail/warn per CIS control, with remediation. Cluster unchanged. |
| **B — Harden existing** | `reports/post_<ts>/delta.md` shows kube-bench Δ ≥ +8 pts and kubescape Δ ≥ +2 pts (for standalone kubeadm; managed K8s deltas will be smaller because Tier 2 is skipped). All nodes Ready post-Tier 2. **Plus** workload-verify checkpoints CP1 and CP2 both PASS for every tenant — see Workload Validation Harness below. |
| **C — Provision + harden** | Same as B, plus `reports/samples/<topology>-<YYYYMMDD>/` committed (including workload-verify reports) and **all** provisioned DO resources destroyed. |

Universal constraints:

| Item | Value |
|------|-------|
| **Soft budget** | Don't exceed **$1 in DO spend** without checking back. Premium Intel droplets (used for the May 2026 reference run) run at ~$0.083/hr for the 3-node trio; a clean run takes 30-45 min wall time. |
| **Hard requirement** | Tear down every droplet, firewall, volume, and SSH key entry you created. Even on failure. **Especially** on failure. (Only applies to Mission C.) |
| **Don't auto-commit posture reports** | Mission A reports may contain a cluster fingerprint the user doesn't want public. Commit only if the user explicitly asked. Mission B/C commits the hardening *delta*, which is generic enough to publish. |

## Inputs you need from the user

| Input | Required when | Notes |
|-------|---------------|-------|
| DigitalOcean API token | Mission C (provision) | Personal Access Token with **Droplet, SSH key, Firewall** read+write. Don't `doctl auth init` (that writes the token to disk) — use `DIGITALOCEAN_ACCESS_TOKEN=<token> doctl …` inline per command. Remind the user to revoke at the end. |
| `KUBECONFIG` path | Missions A and B (existing cluster) | Confirm it points where the user expects: `kubectl config current-context` + `kubectl get nodes`. |
| SSH key | Mission C | Will use `~/.ssh/id_ed25519.pub` by default. Create with `ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519` if missing. |
| Workstation public IP | Mission C | `curl -s https://ifconfig.me`. Goes into the firewall rules. **Re-fetch every session** — residential ISPs rotate. |

---

## Decision tree

```
Does the user just want a posture report (no changes)?
├── Yes → Mission A (Assess only)
└── No  ↓
    Does the user have an existing cluster (KUBECONFIG, EKS/GKE/AKS, kubeadm)?
    ├── Yes → Mission B (Harden existing)
    └── No  → Mission C (Provision on DO + harden)
```

If the user provides a DO token but also has a `KUBECONFIG` set, ask
once which they want — the procedures differ in important ways
(Mission B skips Tier 2 if it's a managed cluster).

---

## Mission A: Assessment only — read-only posture report

For requests like *"What's my CIS posture?"*, *"Generate a security
report on this cluster"*, *"Which controls do we fail?"*.

**No cluster modifications.** No Tier 1 manifests, no Ansible
playbook, no Kyverno install, no apiserver restart. Just the two
scanners + a per-control pass/fail/warn report with remediation.

### A1. Verify cluster access

```bash
kubectl config current-context
kubectl get nodes
kubectl version --short
```

### A2. Install kubescape

```bash
sudo bash scripts/install-kubescape.sh
```

### A3. Run the assessment

```bash
./harden.py assess
```

Output lands in `reports/assess_<ts>/`:
- `assessment.md` — Markdown report (summary table + failed controls
  with remediation)
- `scores.json` — machine-readable counts
- `kube-bench.json` / `kubescape.json` — raw scanner output

### A4. Provider-specific scanners (optional but recommended for EKS/GKE)

For managed K8s, before step A3 swap the kube-bench benchmark in
`scan/kube-bench-job.yaml`:
- EKS: add `--benchmark eks-1.5.0` to the kube-bench args
- GKE: add `--benchmark gke-1.6.0`

See [SETUP-HYPERSCALER.md](SETUP-HYPERSCALER.md) for details.

### A5. Hand back to the user

```
Cluster: <context>, <node count> nodes, <k8s version>
kube-bench: <pass> pass / <fail> fail / <warn> warn (<score>%)
kubescape:  <pass> pass / <fail> fail / <warn> warn (<score>%)
Report:     reports/assess_<ts>/assessment.md
```

Optionally commit `reports/assess_<ts>/{assessment.md,scores.json}`
to `reports/samples/assess-<context>-<YYYYMMDD>/` if the user asked
for the report to be tracked. **Do not** auto-commit when the user
asked for "just a report" — they may not want their cluster's
posture leaking into a public repo.

---

## Mission B: Validate against an existing cluster

### B1. Identify the cluster type

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
  [SETUP-LOCAL.md](SETUP-LOCAL.md) for kind caveats. For others, prefer
  `--skip-tier2` and document the limitation.

### B2. Install pre-reqs locally

```bash
sudo bash scripts/install-kubescape.sh      # auto-detects arch
python3 --version                            # 3.10+
ansible-playbook --version                   # only if running Tier 2
kubectl version --short                      # client must reach the server
```

### B3. Run the pipeline

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

### B4. Report and commit

Same as Mission C (see C7). **Do not** make changes to the user's
existing cluster beyond what `harden.py` does. **Do not** tear down
their cluster.

---

## Mission C: Provision on DigitalOcean and harden

### C1. Pre-flight

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

### C2. Ensure the SSH key is registered in DigitalOcean

```bash
DIGITALOCEAN_ACCESS_TOKEN=<token> doctl compute ssh-key list --format ID,Name,FingerPrint
```

If your key's fingerprint isn't there, import it:

```bash
DIGITALOCEAN_ACCESS_TOKEN=<token> doctl compute ssh-key import \
  agentic-mode --public-key-file ~/.ssh/id_ed25519.pub
```

Save the resulting ID — you'll need it at droplet-create time.

### C3. Create the firewall

```bash
MAC_IP=$(curl -s https://ifconfig.me)
DIGITALOCEAN_ACCESS_TOKEN=<token> doctl compute firewall create --name k8s-fw \
  --tag-names k8s-cluster \
  --inbound-rules "protocol:tcp,ports:22,address:${MAC_IP}/32 protocol:tcp,ports:6443,address:${MAC_IP}/32 protocol:tcp,ports:1-65535,tag:k8s-cluster protocol:udp,ports:1-65535,tag:k8s-cluster protocol:icmp,tag:k8s-cluster" \
  --outbound-rules "protocol:tcp,ports:1-65535,address:0.0.0.0/0,address:::/0 protocol:udp,ports:1-65535,address:0.0.0.0/0,address:::/0 protocol:icmp,address:0.0.0.0/0,address:::/0"
```

### C4. Choose a droplet size — gotcha

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

### C5. Run the bootstrap

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

### C6. Pull reports

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

### C7. Commit reports

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

### C8. Tear down — **mandatory**

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

### C9. Tell the user to revoke the token

End your response with a literal line like:

> **Revoke your DO token at https://cloud.digitalocean.com/account/api/tokens — I won't need it again.**

Don't bury it in a paragraph.

---

## Workload Validation Harness (Missions B and C)

Since [issue #1](https://github.com/AI-Fabrik/k8s-hardening/issues/1)
landed, `./harden.py all` runs a mandatory workload-validation
harness around the CIS pipeline. The agent doesn't need to invoke
this manually — it's baked into `all` — but it's worth understanding
what to expect in the output, especially when triaging failures.

The pipeline order is documented in
[issue #1](https://github.com/AI-Fabrik/k8s-hardening/issues/1) and
in the rewritten `phase_all` of [`harden.py`](../harden.py). Two
checkpoints:

- **CP1 — Pre-existing tenant survives.** After Tier 1 + Tier 2,
  re-runs the verify Job in `tenant-a`. If this fails it means the
  hardening either evicted tenant pods, broke intra-cluster
  networking (NetworkPolicy too strict), or made Kyverno admit a
  pod template that was previously fine.
- **CP2 — New tenants admit, harness deploys.** Creates `tenant-b`,
  deploys the same 8 workloads under the now-active Kyverno policies,
  adds a `background-job` to `tenant-a`. If CP2 fails on tenant-b,
  the Kyverno policies are blocking legitimate tenant workloads. If
  it fails on tenant-a's harness alone, an admission rule is racing.

### Output artifacts

Under `reports/workload_<ts>/`:

- `workload-verify-tenant-a-pre-hardening.md`
- `workload-verify-tenant-a-post-hardening-cp1.md`
- `workload-verify-tenant-a-post-hardening-cp2.md`
- `workload-verify-tenant-b-post-hardening-cp2.md`

Each contains the in-pod log of the verify Job. Triage from there.

### Failure-mode shortcuts

| Symptom (in workload-verify-*.md) | Likely cause |
|-----------------------------------|--------------|
| `fail: web Service unreachable or non-200` | nginx-unprivileged image failed to start (most often: PSS restricted block on a security-context drift) |
| `fail: db round-trip failed` | postgres pod still initializing — verify Job ran before db reached Ready. Re-run the verify phase manually: `./harden.py workload-verify --namespace tenant-a` |
| `fail: queue-worker has not pushed anything` | queue-worker can't reach cache; check cache pod status + NetworkPolicy default-deny may need a tenant-internal allow rule |
| `fail: cron-pinger has not recorded a success yet` | CronJob hasn't fired yet (it runs every 1 min) — wait, or check whether the policy-exception is admitting the cron pod |
| tenant-b deploy fails with `denied by` ... | Kyverno policy refused — read the message; we may need to add the workload pattern to an exception |

### Adding a workload

If the user requests adding a new workload to the harness:

1. Drop the YAML under the appropriate path (`workloads/tenant/v1/`
   for a tenant workload, etc.). Match the security-context shape of
   existing files — failure will be in Kyverno admission, not in the
   container itself.
2. If verification is needed, extend the `L3` section of
   `workloads/verify/verify-job.yaml.tmpl` with a new check.
3. Run `./harden.py workload-deploy --kind tenant --version v1
   --tenant tenant-a --kubeconfig reports/kubeconfig-tenant-a.yaml`
   to test in isolation.

---

## Image drift (pre-flight image-existence check)

`./harden.py all` and `./harden.py check-images` both run an image
pre-flight that queries Docker Hub for every `image:` line under
`workloads/` and `scan/`. Image vendors quietly disappear (Bitnami
pulled all their public docker.io tags in mid-2025 with no warning
— see commit 746327c). If the pre-flight reports a 404, **don't run
the rest of the pipeline** — you'll waste 30+ min deploying pods
that crash on ImagePullBackOff.

### Agent behavior on a 404

When you're the agent and the pre-flight fails:

1. For each missing image, find an officially-published alternative:
   - Containers from vendors who left docker.io: check whether they
     have a `bitnamilegacy/*` mirror, a Quay.io presence, or a paid
     successor (we use official images upstream — `library/postgres`,
     `library/redis` etc. — to avoid this entirely going forward).
   - Image with a renamed tag: scan Docker Hub's tag list for a
     close match (`postgres:16-alpine` → `postgres:16.2-alpine`).
   - Image that moved registries: switch the prefix (`docker.io/foo/x`
     → `ghcr.io/foo/x` if that's where they republished).
2. Update the affected `workloads/**/*.yaml` files, adjusting env
   vars / data paths / runAsUser to match the new image (these
   often differ between vendors — e.g., Bitnami's postgres ran as
   uid 1001 with `POSTGRESQL_*` env vars, official postgres runs as
   uid 70 with `POSTGRES_*`).
3. **Stop and ask the user before committing.** Show:
   - the diff of YAML changes,
   - the affected security-context / env-var adjustments,
   - whether you've tested locally (likely not — pre-flight only
     checks existence, not behavior),
   - your confidence that the substitution is functionally
     equivalent.
4. On approval: commit with a focused message documenting the
   vendor break, push, then re-run `./harden.py check-images` to
   confirm, then `./harden.py all`.

### Human behavior on a 404

If you're not an agent, the failure message lists which files need
edits. Either fix them by hand or pull the latest from `main` —
maintenance commits often land within hours of a vendor disappearing.



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

**Mission A (Assess):** 4-line summary.

1. Cluster: `<context>`, `<n>` nodes, k8s `<version>`
2. kube-bench: `<pass>/<fail>/<warn>` (`<score>%`)
3. kubescape: `<pass>/<fail>/<warn>` (`<score>%`)
4. Report path: `reports/assess_<ts>/assessment.md`

**Missions B and C (Harden):** 8-line summary.

1. Cluster topology (e.g., 1 CP + 2 workers, kubeadm v1.29, DO Premium Intel)
2. Baseline scores (kube-bench %, kubescape %)
3. Post scores (kube-bench %, kubescape %)
4. Delta (+X.X, +X.X)
5. Workload CP1 (pre-existing tenant survives): PASS / FAIL
6. Workload CP2 (new tenant + harness): PASS / FAIL
7. Reports committed at `reports/samples/...` (git SHA) — Mission C only
8. Droplets torn down + token-revocation reminder to user — Mission C only

If you can't produce all the lines for your mission, you're not done
— finish or hand back to the human with a precise "I got to step N,
here's what's left."
