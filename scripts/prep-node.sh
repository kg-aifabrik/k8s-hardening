#!/usr/bin/env bash
# Prepare an Ubuntu 22.04 / 24.04 host to be a kubeadm v1.35 node.
#
# Idempotent: safe to re-run. Does NOT call `kubeadm init` or
# `kubeadm join` — those are environment-specific and live in the
# orchestration scripts.
#
# Usage (run as root, on the target node):
#   sudo bash prep-node.sh <new-hostname>
#
# Example:
#   sudo bash prep-node.sh cp
#   sudo bash prep-node.sh w1
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "prep-node.sh must run as root" >&2
  exit 1
fi
if [[ -z "${1:-}" ]]; then
  echo "usage: $0 <new-hostname>" >&2
  exit 2
fi

NEW_HOSTNAME="$1"
export DEBIAN_FRONTEND=noninteractive

# Freshly-booted cloud VMs (DO, EC2, etc.) run cloud-init's
# unattended-upgrades pass before they hand off to ordinary
# package management. Racing it produces "Could not get lock
# /var/lib/apt/lists/lock". Wait for cloud-init to finish.
if command -v cloud-init >/dev/null 2>&1; then
  echo "[0/6] Waiting for cloud-init to finish (up to 5 min)"
  cloud-init status --wait --long >/dev/null 2>&1 || true
fi

echo "[1/6] Setting hostname to ${NEW_HOSTNAME}"
hostnamectl set-hostname "${NEW_HOSTNAME}"
if grep -q "127.0.1.1" /etc/hosts; then
  sed -i "s/127.0.1.1.*/127.0.1.1 ${NEW_HOSTNAME}/" /etc/hosts
else
  echo "127.0.1.1 ${NEW_HOSTNAME}" >> /etc/hosts
fi

echo "[2/6] Loading kernel modules + sysctls"
modprobe overlay
modprobe br_netfilter
cat >/etc/modules-load.d/k8s.conf <<EOF
overlay
br_netfilter
EOF
cat >/etc/sysctl.d/k8s.conf <<EOF
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF
sysctl --system >/dev/null

echo "[3/6] Disabling swap"
swapoff -a || true
sed -i '/ swap / s/^/#/' /etc/fstab || true

echo "[4/6] Installing containerd"
apt-get update -y
apt-get install -y containerd apt-transport-https ca-certificates curl gpg
mkdir -p /etc/containerd
containerd config default > /etc/containerd/config.toml
sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
systemctl restart containerd
systemctl enable containerd >/dev/null 2>&1

echo "[5/6] Installing kubelet/kubeadm/kubectl v1.35"
mkdir -p /etc/apt/keyrings
if [[ ! -f /etc/apt/keyrings/kubernetes-apt-keyring.gpg ]]; then
  curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.35/deb/Release.key \
    | gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
fi
echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.35/deb/ /' \
  > /etc/apt/sources.list.d/kubernetes.list
apt-get update -y
apt-get install -y kubelet kubeadm kubectl
apt-mark hold kubelet kubeadm kubectl

echo "[6/6] Done. $(hostname) is ready for kubeadm init/join."
