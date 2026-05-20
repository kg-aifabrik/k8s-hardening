# Setup: Standalone multi-node kubeadm

This is what the framework was designed for: a self-managed kubeadm
cluster running on raw VMs (cloud or on-prem) where you control the
control-plane nodes and can SSH into them as root. All four phases
(`baseline`, `tier1`, `tier2`, `validate`) work, and the kube-bench
DaemonSet runs against a properly-tainted control plane.

The worked example below uses **DigitalOcean** because the UX is
simple and the cost is small. Provider notes at the end of this doc
cover Hetzner, OCI, AWS EC2, and on-prem.

## What this setup exercises

| Area                                                     | Covered |
|----------------------------------------------------------|---------|
| Tier 1 manifest application                              | ✅ |
| Tier 2 over real SSH (Ansible with key auth + privilege escalation) | ✅ |
| Tier 2 static-pod manifest patches on a tainted CP       | ✅ |
| Tier 2 kubelet config patches + restart, propagated to workers | ✅ |
| kube-bench DaemonSet across master + workers             | ✅ |
| Multi-node CIS 4.x divergence (each worker scanned separately) | ✅ |
| `kubescape` framework scan                               | ✅ |

This is the only setup where the framework's full design intent is
actually validated.

## Cost reference

Approximate per-hour cost for **1 CP (2 vCPU / 4 GB) + 2 workers
(1 vCPU / 2 GB)**, 50 GB boot volumes, in May 2026:

| Provider          | Per-hour | 1-day | Notes |
|-------------------|----------|-------|-------|
| Hetzner Cloud (ARM CAX11) | ~€0.018 | ~€0.43 | Cheapest; new accounts need ID verification (~1 day). |
| DigitalOcean (Premium AMD) | ~$0.054 | ~$1.30 | Used here. Pro-rated hourly. |
| Linode (Shared)   | ~$0.050  | ~$1.20 | Comparable to DO. |
| AWS EC2 (t4g.medium x3) | ~$0.10 | ~$2.40 | Plus EBS + data transfer. |
| Oracle Cloud (ARM A1) | $0 if Always Free capacity available | $0 | Capacity is heavily oversubscribed — `Out of capacity` errors are common. |
| On-prem (3 VMs on your own hardware) | $0 | $0 | If you have the iron and an L2 network between VMs. |

A full pipeline run (provision → bootstrap → harden → validate →
teardown) takes about 30 minutes of wall time, so even at AWS prices
a test run is well under $1.

---

# Worked example: DigitalOcean (3 droplets)

## 1. Generate / locate your SSH public key

On your workstation:

```bash
# If you don't have one yet:
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
cat ~/.ssh/id_ed25519.pub
```

Copy the `ssh-ed25519 AAAA...` line.

## 2. Add the key to DigitalOcean

DO console → **Settings → Security → SSH Keys → Add SSH Key**. Paste
the line, name it (e.g. `k8s-harden-workstation`).

## 3. Create the 3 droplets

DO console → **Create → Droplets**. All three go in the same region.

| Droplet | Plan                              | Hostname | Tags                    |
|---------|-----------------------------------|----------|-------------------------|
| `cp`    | Premium AMD, **2 vCPU / 4 GB**    | `cp`     | `k8s-cluster`, `cp`     |
| `w1`    | Premium AMD, **1 vCPU / 2 GB**    | `w1`     | `k8s-cluster`, `worker` |
| `w2`    | Premium AMD, **1 vCPU / 2 GB**    | `w2`     | `k8s-cluster`, `worker` |

For `w1`/`w2` you can set **Quantity = 2** and **Hostnames =
`w1,w2`** in one click. Image: Ubuntu 22.04 LTS (24.04 also works).
Auth: select the SSH key from Step 2.

Capture the **public IPv4** of each droplet — let's call them
`CP_IP`, `W1_IP`, `W2_IP`.

## 4. Create the Cloud Firewall

DO console → **Networking → Firewalls → Create Firewall**. Name
`k8s-fw`. Find your workstation's public IP first:

```bash
curl -s https://ifconfig.me
```

Inbound rules:

| Type    | Protocol | Port      | Sources                |
|---------|----------|-----------|------------------------|
| Custom  | TCP      | 22        | `<your-IP>/32`         |
| Custom  | TCP      | 6443      | `<your-IP>/32`         |
| All TCP | TCP      | All ports | Tag: `k8s-cluster`     |
| All UDP | UDP      | All ports | Tag: `k8s-cluster`     |
| ICMP    | ICMP     | —         | Tag: `k8s-cluster`     |

Outbound: leave default (all out).

**Apply to Droplets:** type tag `k8s-cluster` — the firewall
attaches to all three droplets automatically.

## 5. Verify SSH

```bash
for ip in $CP_IP $W1_IP $W2_IP; do
  ssh -o StrictHostKeyChecking=accept-new root@$ip "hostname && uname -m"
done
```

You should see three Ubuntu hosts. If a connection hangs, recheck the
firewall rules (and that your workstation IP hasn't changed).

## 6. Bootstrap kubeadm on all 3 nodes

Save this as `/tmp/prep-node.sh` on your workstation:

```bash
#!/bin/bash
# Bootstrap a kubeadm v1.29 node on Ubuntu 22.04/24.04.
# Args: $1 = new hostname (cp, w1, w2)
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

hostnamectl set-hostname "$1"
sed -i "s/127.0.1.1.*/127.0.1.1 $1/" /etc/hosts || echo "127.0.1.1 $1" >> /etc/hosts

modprobe overlay; modprobe br_netfilter
cat >/etc/modules-load.d/k8s.conf <<EOF
overlay
br_netfilter
EOF
cat >/etc/sysctl.d/k8s.conf <<EOF
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF
sysctl --system

swapoff -a; sed -i '/ swap / s/^/#/' /etc/fstab || true

apt-get update -y
apt-get install -y containerd apt-transport-https ca-certificates curl gpg
mkdir -p /etc/containerd
containerd config default > /etc/containerd/config.toml
sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
systemctl restart containerd
systemctl enable containerd

mkdir -p /etc/apt/keyrings
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.29/deb/Release.key \
  | gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.29/deb/ /' \
  > /etc/apt/sources.list.d/kubernetes.list
apt-get update -y
apt-get install -y kubelet kubeadm kubectl
apt-mark hold kubelet kubeadm kubectl
```

Ship it and run on each node:

```bash
for ip in $CP_IP $W1_IP $W2_IP; do scp /tmp/prep-node.sh root@$ip:/tmp/; done
ssh root@$CP_IP "bash /tmp/prep-node.sh cp"
ssh root@$W1_IP "bash /tmp/prep-node.sh w1"
ssh root@$W2_IP "bash /tmp/prep-node.sh w2"
```

## 7. `kubeadm init` on cp, join workers

The public IP must be in the kube-apiserver cert SANs so kubectl
from your workstation (and Ansible's intra-cluster kubectl calls)
work without TLS errors:

```bash
ssh root@$CP_IP "kubeadm init \
  --pod-network-cidr=10.244.0.0/16 \
  --apiserver-advertise-address=$CP_IP \
  --apiserver-cert-extra-sans=$CP_IP"

ssh root@$CP_IP "mkdir -p /root/.kube && cp /etc/kubernetes/admin.conf /root/.kube/config"

# Flannel CNI
ssh root@$CP_IP "kubectl apply -f https://raw.githubusercontent.com/flannel-io/flannel/v0.25.1/Documentation/kube-flannel.yml"

# Print join command, run on each worker
JOIN=$(ssh root@$CP_IP "kubeadm token create --print-join-command")
ssh root@$W1_IP "$JOIN"
ssh root@$W2_IP "$JOIN"
```

Wait for all three nodes Ready:

```bash
ssh root@$CP_IP "until [ \$(kubectl get nodes --no-headers | grep -c ' Ready ') = 3 ]; do sleep 5; done; kubectl get nodes"
```

## 8. Set up cp-to-workers SSH (so Ansible can drive the workers)

Ansible runs on the control plane. Generate a key on cp, then add it
to w1 / w2:

```bash
ssh root@$CP_IP 'ssh-keygen -t ed25519 -N "" -f /root/.ssh/id_ed25519'
CP_PUB=$(ssh root@$CP_IP 'cat /root/.ssh/id_ed25519.pub')
ssh root@$W1_IP "echo '$CP_PUB' >> /root/.ssh/authorized_keys"
ssh root@$W2_IP "echo '$CP_PUB' >> /root/.ssh/authorized_keys"

# Verify
ssh root@$CP_IP "ssh -o StrictHostKeyChecking=accept-new root@$W1_IP hostname && \
                 ssh -o StrictHostKeyChecking=accept-new root@$W2_IP hostname"
```

## 9. Install the hardening toolchain on cp

```bash
ssh root@$CP_IP 'apt-get install -y python3 python3-pip ansible-core git

# kubescape (amd64 here; replace arm64 if needed)
LATEST=$(curl -sf https://api.github.com/repos/kubescape/kubescape/releases/latest | grep -m1 tag_name | cut -d\" -f4)
mkdir -p /root/.kubescape/bin
curl -sfL "https://github.com/kubescape/kubescape/releases/download/${LATEST}/kubescape_${LATEST#v}_linux_amd64.tar.gz" \
  | tar -xz -C /root/.kubescape/bin/ kubescape
ln -sf /root/.kubescape/bin/kubescape /usr/local/bin/kubescape

git clone https://github.com/kg-aifabrik/k8s-hardening.git
'
```

## 10. Write the inventory and run

```bash
ssh root@$CP_IP "cat > /root/k8s-hardening/tier2-ansible/inventory/hosts.ini <<EOF
[control_plane]
cp ansible_host=$CP_IP ansible_connection=local

[workers]
w1 ansible_host=$W1_IP
w2 ansible_host=$W2_IP

[all:vars]
ansible_user=root
ansible_ssh_private_key_file=/root/.ssh/id_ed25519
ansible_python_interpreter=/usr/bin/python3
ansible_ssh_common_args='-o StrictHostKeyChecking=no'
EOF

cd /root/k8s-hardening && ./harden.py all --inventory tier2-ansible/inventory/hosts.ini"
```

Expect 10-20 minutes. The control plane briefly restarts during
Tier 2 (kubelet picks up the patched static-pod manifests); the
orchestrator now waits for `/healthz` before validate.

## 11. Collect reports

```bash
scp -r root@$CP_IP:/root/k8s-hardening/reports ./reports-do
open reports-do/post_*/delta.md
```

## 12. Tear down

DO console → **Droplets** → select all three → **Destroy**. Per-hour
billing stops the moment the droplets are destroyed. Don't forget
the Cloud Firewall (Networking → Firewalls → Delete).

---

# Other providers

The bootstrap script and harden.py inventory above are
provider-agnostic. The only things you change per provider are how
you provision VMs and how you configure the firewall.

## Hetzner Cloud (cheapest)

- `hcloud server create --type cax11 --image ubuntu-22.04 --location nbg1 --ssh-key <id> --name cp`
- 3× CAX11 ARM = ~€0.018/hr total. Pennies for a test session.
- Hetzner's per-server firewall has the same pattern: open 22/6443 from
  your IP, open all-protocols for source = your VPC / private network.
- Caveat: ARM, so use the `_arm64` kubescape binary in step 9.

## Oracle Cloud (free if you get capacity)

- ARM Always Free A1.Flex: 4 OCPU / 24 GB total free.
- VCN Wizard → "VCN with Internet Connectivity" gives you a public
  subnet automatically. Then add ingress rules: 22+6443 from your
  workstation, all-protocols from `10.0.0.0/16` for intra-VCN.
- The single biggest gotcha: ARM A1 capacity is heavily oversubscribed.
  `Out of capacity for shape VM.Standard.A1.Flex` is the norm.
  Upgrade to Pay-As-You-Go (still $0 for in-allowance usage) to
  jump the queue, or retry across availability domains.

## AWS EC2

- 3× `t4g.medium` (2 vCPU / 4 GB) in a single VPC subnet, public IPs
  assigned.
- Security group: 22 + 6443 from your IP, intra-group all-protocols.
- Use the AMI `ami-xxx Ubuntu 22.04 LTS arm64` (region-specific).
- Cost: ~$0.10/hr for the trio. Don't forget EBS storage and data
  transfer when teardown is delayed.

## On-prem / lab

- 3 Ubuntu 22.04 VMs on any hypervisor (Proxmox, ESXi, vSphere, KVM).
- L2 connectivity between VMs is mandatory (Flannel VXLAN, etcd
  gossip, kubelet → apiserver).
- No firewall config needed if the VMs are on a trusted lab LAN; the
  framework's [Tier 1 NetworkPolicy](../tier1-manifests/01-default-deny-netpol.yaml)
  still enforces in-cluster isolation.

## What you DON'T change

The Ansible playbook ([`tier2-ansible/playbook.yml`](../tier2-ansible/playbook.yml))
and the manifests apply identically across providers. If your VMs
run Ubuntu 22.04+ with kubeadm v1.29 and SSH-root-accessible, the
pipeline will work.
