# Architecture

## Design principles

1. **Idempotent.** Every phase can be re-run safely. Tier 1 manifests use
   declarative kubectl apply; Tier 2 scripts check current state before
   modifying.
2. **Tiered by risk.** Cluster-level changes (Tier 1) are safe and reversible.
   Node-level changes (Tier 2) restart control plane components.
   Anything beyond that is explicit Tier 3 manual work.
3. **Auditable.** Every fix has a CIS control reference in a comment.
   Reports diff baseline vs. post-hardening so an auditor can trace every change.
4. **Pinned versions.** No `:latest` tags. Kyverno, kube-bench, audit policy
   schema all pinned.

## Phase model

```
+-------------+    +---------+    +---------+    +-----------+
|  baseline   | -> |  tier1  | -> |  tier2  | -> | validate  |
|             |    |         |    |         |    |           |
| kube-bench  |    | kubectl |    | ansible |    | kube-bench|
| kubescape   |    | apply   |    | + ssh   |    | kubescape |
|   -> JSON   |    |  YAMLs  |    | + sudo  |    |   -> diff |
+-------------+    +---------+    +---------+    +-----------+
       |                                                |
       v                                                v
  baseline.md                                     post-hardening.md
                                                  delta.md
```

## What each tier owns

### Tier 1 (cluster-level, kubectl)

- **PSS labels** on namespaces (00)
- **Default-deny NetworkPolicies** (01)
- **ServiceAccount automount=false** on default SAs (02)
- **RBAC**: read-only ClusterRole defined for human ops (03)
- **Kyverno install** (orchestrator step, not a manifest)
- **Kyverno ClusterPolicies** enforcing:
  - No privileged containers (01)
  - No host namespaces (02)
  - No hostPath (03)
  - Run as non-root (04)
  - Drop ALL capabilities (05)
  - No privilege escalation (06)
  - Read-only root FS (07)
  - Seccomp RuntimeDefault (08)
  - Resource limits required (09)
  - Default SA disallowed for pods (10)

### Tier 2 (node-level, Ansible)

- **common/**: file permissions on `/etc/kubernetes/*`, PKI, kubelet config
- **api-server/**: CIS 1.2.x flags + audit policy + audit log dir
- **controller-manager/**: CIS 1.3.x flags
- **scheduler/**: CIS 1.4.x flags
- **etcd/**: encryption-at-rest config + manifest perms
- **kubelet/**: anonymous-auth=false, authorization webhook, readOnlyPort=0, etc.

### Tier 3 (manual)

See `TIER3-MANUAL.md`.

## Why Kyverno over Gatekeeper/OPA

- Native Kubernetes YAML (no Rego to learn)
- Built-in mutation (we use it for defaults in newer iterations)
- Smaller operational footprint (one Deployment vs. multi-pod OPA stack)
- For the operator's existing FastAPI/Python platform skill set, Kyverno is
  closer to "infra YAML" than "specialized policy language."

If your team already runs OPA Gatekeeper, the same controls map 1:1 to
ConstraintTemplates; swap the `kyverno-policies/` directory.

## Why not just use Kubescape's built-in fix?

Kubescape's `fix` subcommand patches scanned manifests in-place. That's fine
for a single repo of declarative YAML, but our cluster mixes:
- kubeadm-generated static pod manifests on each node
- Kubelet config on each node
- Cluster-level API resources

Kubescape can't touch node-local files over SSH. Ansible can. So we use
Kubescape for **what's broken** and Ansible for **how to fix node-level
things**.
