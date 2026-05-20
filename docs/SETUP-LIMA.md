# Setup: Local Lima single-node

Use this for a fast smoke test of the framework on your Mac. Nothing
to provision in the cloud, no costs, no SSH-key juggling. Tradeoff:
single node, control plane untainted, and Tier 2 runs Ansible over a
`local` connection instead of real SSH — so the multi-node behaviors
this framework was designed to exercise are only partially covered.

## What this setup exercises

| Area                              | Covered |
|-----------------------------------|---------|
| Tier 1 manifest application       | ✅ |
| Tier 2 static-pod manifest patches (apiserver, KCM, scheduler, etcd) | ✅ |
| Tier 2 kubelet config patches + restart | ✅ |
| Tier 2 file permissions on PKI / etcd data dir | ✅ |
| Kyverno install + policy admission | ✅ |
| kube-bench DaemonSet scoring      | ⚠️  Only one node — node and policies checks land on the same pod every time |
| kubescape framework scan          | ✅ |

## What this setup does NOT exercise

- **Ansible over real SSH.** The inventory uses `ansible_connection=local`,
  so privilege escalation and SSH key management are skipped.
- **Tainted control plane.** kubeadm puts a `NoSchedule` taint on the CP;
  for single-node we remove it so the DaemonSet scanner and CoreDNS land.
  The scheduling-aware code paths in the scanner aren't exercised.
- **Multi-node coordination.** Worker join, network partitions between
  nodes, intra-cluster firewall rules — none of this exists.
- **`kube-bench` divergence between nodes.** Aggregator only ever sees
  one node's worth of CIS 4.x (worker) results.

Promote to [SETUP-STANDALONE.md](SETUP-STANDALONE.md) before drawing
conclusions about a real environment.

## Prereqs

- macOS (Apple Silicon or Intel)
- Homebrew

## 1. Install Lima

```bash
brew install lima
limactl --version   # >= 2.x
```

> If you've used Multipass before: on macOS 26 (Tahoe) Multipass's
> bundled QEMU fails with a `host-arm-cpu.sme` property error. Lima
> uses Apple's Virtualization.framework via `vmType: vz` and works
> cleanly.

## 2. Launch the VM

The repo ships a Lima config at [`.lima/k8s-harden.yaml`](../.lima/k8s-harden.yaml):

```bash
limactl start --tty=false --name k8s-harden .lima/k8s-harden.yaml
```

Defaults: 4 vCPU, 6 GiB RAM, 30 GiB disk, swap disabled. Adjust the
YAML before the first `start` if your Mac is constrained.

## 3. Bootstrap kubeadm inside the VM

Open a shell on the VM and run the standard kubeadm v1.29 install:

```bash
limactl shell k8s-harden
```

Inside the VM (everything below runs as the lima user with sudo):

```bash
# Kernel modules + sysctls
sudo modprobe overlay br_netfilter
cat <<EOF | sudo tee /etc/modules-load.d/k8s.conf
overlay
br_netfilter
EOF
cat <<EOF | sudo tee /etc/sysctl.d/k8s.conf
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF
sudo sysctl --system

# containerd
sudo apt-get update && sudo apt-get install -y containerd apt-transport-https ca-certificates curl gpg
sudo mkdir -p /etc/containerd
containerd config default | sudo tee /etc/containerd/config.toml >/dev/null
sudo sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
sudo systemctl restart containerd

# kubeadm/kubelet/kubectl v1.29 (pin to a 1.29.x line)
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.29/deb/Release.key \
  | sudo gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.29/deb/ /' \
  | sudo tee /etc/apt/sources.list.d/kubernetes.list
sudo apt-get update && sudo apt-get install -y kubelet kubeadm kubectl
sudo apt-mark hold kubelet kubeadm kubectl

# Init the cluster
sudo kubeadm init --pod-network-cidr=10.244.0.0/16
mkdir -p $HOME/.kube
sudo cp /etc/kubernetes/admin.conf $HOME/.kube/config
sudo chown $(id -u):$(id -g) $HOME/.kube/config

# Single-node: untaint so workloads can land on the CP
kubectl taint nodes --all node-role.kubernetes.io/control-plane-

# CNI (Flannel matches --pod-network-cidr above)
kubectl apply -f https://raw.githubusercontent.com/flannel-io/flannel/v0.25.1/Documentation/kube-flannel.yml

# Wait for Ready
until kubectl get nodes 2>/dev/null | grep -q ' Ready '; do sleep 5; done
kubectl get nodes
```

## 4. Install the hardening toolchain inside the VM

```bash
sudo apt-get install -y python3 ansible-core git

# kubescape (arm64 on Apple Silicon, amd64 on Intel — install.sh auto-detects)
curl -s https://raw.githubusercontent.com/kubescape/kubescape/master/install.sh | /bin/bash
sudo ln -sf $HOME/.kubescape/bin/kubescape /usr/local/bin/kubescape

git clone https://github.com/kg-aifabrik/k8s-hardening.git
cd k8s-hardening
```

## 5. Write the inventory and run the pipeline

Single-node uses a `local` connection — the playbook still needs the
host to appear in both `[control_plane]` and `[workers]` because the
plays target each group separately:

```bash
cat > tier2-ansible/inventory/hosts.ini <<'EOF'
[control_plane]
localhost ansible_connection=local

[workers]
localhost ansible_connection=local

[all:vars]
ansible_python_interpreter=/usr/bin/python3
EOF

./harden.py all --inventory tier2-ansible/inventory/hosts.ini
```

Reports land under `reports/baseline_*/` and `reports/post_*/`. Read
`reports/post_*/delta.md` for the before/after summary.

## 6. Tear down

```bash
exit                              # leave VM shell
limactl stop k8s-harden
limactl delete k8s-harden
```

The `.lima/k8s-harden.yaml` template is preserved so you can spin a
fresh VM back up with the same `limactl start` command.

## Expected scores

For our reference run on a 4 vCPU / 6 GiB Lima VM, see the *Observed
scores* table in the [README](../README.md).
