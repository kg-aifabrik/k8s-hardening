# Setup: Managed K8s (EKS / GKE)

This framework was designed for self-managed kubeadm clusters. On a
managed control plane (EKS, GKE, AKS) you can still get value from the
Tier 1 manifests and the scanning pipeline, but **Tier 2 cannot run**
— there is no SSH access to the control-plane nodes, and the provider
owns the static pod manifests and kubelet configuration.

That's not a deficiency of the managed services. EKS/GKE/AKS already
apply the CIS 1.x/2.x/3.x controls (master, etcd, control plane
configuration) as part of their hardened baseline. The provider's
shared-responsibility line moves: you're responsible for *workload*
posture (CIS 5.x) and *some* worker-node posture (CIS 4.x); the
provider is responsible for the rest.

This guide covers two providers: **Amazon EKS** and **Google GKE**.

## What works, what doesn't

| Component               | EKS / GKE | Notes |
|-------------------------|-----------|-------|
| `baseline` scan         | ✅ partial | Use provider-specific kube-bench benchmark; master/etcd checks are skipped |
| Tier 1 manifests        | ✅        | PSS, NetworkPolicy, Kyverno, RBAC, default-SA mount |
| Kyverno install         | ✅        | But: some Kyverno policies in this repo overlap with provider-native policies (see below) |
| Tier 2 Ansible playbook | ❌        | No SSH to control plane; some kubelet config is also provider-managed on managed node pools |
| `validate` scan         | ✅ partial | Same scope as baseline |
| Tier 3 manual items     | Partial   | Most CIS 1.x cert rotation / KMS items are provider-managed; OIDC and image signing remain your job |

Run the framework with:

```bash
./harden.py all --skip-tier2
```

## Provider-specific scanners

### kube-bench benchmarks

`scan/kube-bench-job.yaml` defaults to auto-detection, which on a
managed cluster picks the wrong target. Override with the
provider's benchmark name when applying:

| Provider | kube-bench benchmark        | kubescape framework        |
|----------|------------------------------|-----------------------------|
| EKS      | `eks-1.5.0` (or current EKS) | `cis-eks-t1.2.0`            |
| GKE      | `gke-1.6.0` (or current GKE) | `cis-v1.10.0` (no GKE-specific framework as of writing; CIS v1.10 covers GKE 1.29) |

To override the benchmark in our DaemonSet manifest, edit
[`scan/kube-bench-job.yaml`](../scan/kube-bench-job.yaml) and add
`--benchmark <name>` to the kube-bench `args:` block. Example for EKS:

```yaml
args:
  - |
    kube-bench run --json --benchmark eks-1.5.0
    sleep 86400
```

The DaemonSet will still hit every node. On EKS/GKE the API server
node is invisible, so the DaemonSet only runs on worker nodes — which
is exactly what you want for `eks-*` / `gke-*` benchmarks (they're
worker-only by design).

## Provider-specific additional controls

CIS itself does not capture provider-native security features. These
are what an audit on EKS/GKE will look at *in addition to* the CIS
benchmark.

### Amazon EKS

| Control                                       | Recommendation |
|-----------------------------------------------|----------------|
| **IAM authentication**                        | Use IAM-managed entries (`aws eks create-access-entry`) or the older `aws-auth` ConfigMap. Avoid long-lived static tokens. |
| **Pod-level IAM (IRSA / EKS Pod Identity)**   | Use IRSA or the newer EKS Pod Identity association; don't grant cluster-wide IAM to all pods via the node role. |
| **Secrets envelope encryption (KMS CMK)**     | Enable on cluster create: `--encryption-config provider=<KMS_ARN>`. Re-encrypt existing secrets after enabling (see [TIER3-MANUAL.md](TIER3-MANUAL.md)). |
| **Audit logging to CloudWatch**               | Enable *all five* log types on cluster: `api`, `audit`, `authenticator`, `controllerManager`, `scheduler`. |
| **Private API endpoint**                      | `--endpoint-public-access=false` for clusters that don't need internet-reachable kubectl. |
| **VPC CNI security groups for pods**          | Pin per-pod security groups via SecurityGroupPolicy CRD (requires the VPC CNI in pod-eni mode). |
| **Node AMI baseline**                         | Use EKS-optimized AMIs; rotate frequently. For tighter control use Bottlerocket. |
| **Inspector / GuardDuty / Image scanning**    | Out of scope for this framework but typically expected by auditors. |

### Google GKE

| Control                                       | Recommendation |
|-----------------------------------------------|----------------|
| **Workload Identity**                         | Replaces node-pool service accounts. Map K8s SAs → GCP SAs explicitly; don't grant the node's SA broad cloud roles. |
| **Binary Authorization**                      | Enforce signed-image policies at admit time. Use the GKE-native enforcer, or layer Kyverno-verify-images on top for finer policy. |
| **Shielded GKE Nodes**                        | Enable Secure Boot + vTPM at node-pool create. |
| **Application-layer secrets encryption (Cloud KMS)** | `--database-encryption-key`. Same re-encrypt-existing-secrets caveat as EKS. |
| **Private Cluster**                           | `--enable-private-nodes --enable-private-endpoint`. |
| **Network Policy (Calico)** or **Dataplane V2 (eBPF)** | Enabled at cluster create (`--enable-network-policy` or `--enable-dataplane-v2`). Tier 1's `default-deny` netpol still applies but needs an enforcing CNI. |
| **Confidential GKE Nodes (AMD SEV)**          | Per node pool. Workload-data confidentiality at rest in memory. |
| **Cloud Audit Logs**                          | Admin Activity is on by default; enable Data Access logs for the cluster project. |

## EKS step-by-step

### Prereqs

- `aws` CLI configured (`aws configure` or SSO)
- `kubectl` v1.29+
- `eksctl` for quick cluster creation (or use Terraform / CloudFormation)

### 1. Create a hardened cluster

```bash
KMS_ARN=$(aws kms describe-key --key-id alias/eks-secrets --query KeyMetadata.Arn --output text)

eksctl create cluster \
  --name k8s-harden \
  --region us-east-1 \
  --version 1.29 \
  --nodegroup-name workers \
  --node-type t3.medium \
  --nodes 2 \
  --node-private-networking \
  --secrets-encryption-key-arn "$KMS_ARN" \
  --with-oidc \
  --logging '{enable: ["api","audit","authenticator","controllerManager","scheduler"]}' \
  --endpoint-public-access \
  --endpoint-private-access
```

(Or use whatever your team already uses to provision EKS. The flags
above match what CIS / provider-specific controls expect.)

### 2. Update kubeconfig and verify

```bash
aws eks update-kubeconfig --name k8s-harden --region us-east-1
kubectl get nodes
```

### 3. Configure the framework for EKS

Edit [`scan/kube-bench-job.yaml`](../scan/kube-bench-job.yaml) and
add `--benchmark eks-1.5.0` to the kube-bench args (see snippet
above). For kubescape, the orchestrator already runs `cis-v1.10.0`;
optionally swap to the EKS framework after install:

```bash
kubescape list frameworks | grep -i eks
# update harden.py:run_kubescape() to pass cis-eks-t1.2.0 if you want
# EKS-specific scoring instead of generic CIS.
```

### 4. Run

```bash
./harden.py all --skip-tier2
```

You'll get baseline + Tier 1 + validate. Read `reports/post_*/delta.md`.

## GKE step-by-step

### Prereqs

- `gcloud` CLI authenticated to a project
- `kubectl` v1.29+

### 1. Create a hardened cluster

```bash
gcloud container clusters create k8s-harden \
  --release-channel=stable \
  --enable-private-nodes \
  --master-ipv4-cidr 172.16.0.32/28 \
  --enable-private-endpoint \
  --enable-master-authorized-networks \
  --master-authorized-networks "$(curl -s ifconfig.me)/32" \
  --shielded-secure-boot \
  --shielded-integrity-monitoring \
  --workload-pool="$(gcloud config get-value project).svc.id.goog" \
  --enable-dataplane-v2 \
  --binauthz-evaluation-mode=PROJECT_SINGLETON_POLICY_ENFORCE \
  --database-encryption-key=projects/$(gcloud config get-value project)/locations/global/keyRings/gke/cryptoKeys/secrets \
  --num-nodes 2 \
  --machine-type e2-standard-2 \
  --cluster-version 1.29
```

Substitute your KMS keyring path. Some of the flags (Binary
Authorization, Cloud KMS) require pre-provisioned resources in the
project.

### 2. Get kubeconfig

```bash
gcloud container clusters get-credentials k8s-harden
kubectl get nodes
```

### 3. Configure the framework for GKE

Edit `scan/kube-bench-job.yaml` and set `--benchmark gke-1.6.0` (or
current; check `kube-bench --help`).

### 4. Run

```bash
./harden.py all --skip-tier2
```

## Reading the report

On managed K8s the kube-bench delta is smaller than on standalone
because the master/etcd/CP sections are absent — there's less surface
for the framework to score. The kubescape delta tracks the same
Tier 1 workload posture changes regardless of topology.

The point of running this on a managed cluster isn't to chase a
percentage. It's to:

1. Confirm Tier 1 lands cleanly (no Kyverno admission conflicts with
   provider webhooks like the AWS LB controller or GKE Anthos
   add-ons).
2. Generate an auditable report that documents your *workload* CIS
   posture, which is your responsibility under the shared model.
