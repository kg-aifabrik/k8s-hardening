#!/usr/bin/env bash
# Spin up the Lima single-node smoke-test environment on macOS.
#
# Run from the repo root or anywhere — paths are resolved relative
# to this script.
#
# Usage:
#   bash scripts/lima-up.sh             # provision + bootstrap, don't run pipeline
#   RUN_HARDEN=1 bash scripts/lima-up.sh # also run ./harden.py all at the end
#
# What it does:
#   1. limactl start using .lima/k8s-harden.yaml (idempotent).
#   2. Copies prep-node.sh + install-kubescape.sh into the VM.
#   3. Runs prep-node.sh (containerd + kubeadm v1.35).
#   4. kubeadm init, untaint, install Flannel.
#   5. apt installs ansible/git; installs kubescape.
#   6. Clones repo + writes the single-node hosts.ini in the VM.
#   7. Optionally runs ./harden.py all.
set -euo pipefail

VM_NAME="${VM_NAME:-k8s-harden}"
REPO_URL="${REPO_URL:-https://github.com/kg-aifabrik/k8s-hardening.git}"
RUN_HARDEN="${RUN_HARDEN:-0}"
FLANNEL_URL="${FLANNEL_URL:-https://raw.githubusercontent.com/flannel-io/flannel/v0.25.1/Documentation/kube-flannel.yml}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LIMA_YAML="${REPO_ROOT}/.lima/k8s-harden.yaml"
[[ -f "$LIMA_YAML" ]] || { echo "missing $LIMA_YAML" >&2; exit 1; }

command -v limactl >/dev/null || { echo "limactl not on PATH; brew install lima" >&2; exit 1; }

step() { printf '\n==== %s ====\n' "$*"; }

step "1/7 Starting Lima VM ${VM_NAME}"
if limactl list -q | grep -qx "${VM_NAME}"; then
  echo "VM ${VM_NAME} already exists; reusing"
  limactl start "${VM_NAME}" 2>/dev/null || true
else
  limactl start --tty=false --name "${VM_NAME}" "${LIMA_YAML}"
fi

step "2/7 Copying scripts into VM"
limactl cp "${SCRIPT_DIR}/prep-node.sh" "${VM_NAME}:/tmp/prep-node.sh"
limactl cp "${SCRIPT_DIR}/install-kubescape.sh" "${VM_NAME}:/tmp/install-kubescape.sh"

step "3/7 Running prep-node.sh in VM"
limactl shell "${VM_NAME}" -- sudo bash /tmp/prep-node.sh "${VM_NAME}"

step "4/7 kubeadm init + Flannel + untaint"
limactl shell "${VM_NAME}" -- bash -c "
set -euxo pipefail
if [ ! -f /etc/kubernetes/admin.conf ]; then
  sudo kubeadm init --pod-network-cidr=10.244.0.0/16 \
    --cri-socket=unix:///run/containerd/containerd.sock
fi
mkdir -p \$HOME/.kube
sudo cp -f /etc/kubernetes/admin.conf \$HOME/.kube/config
sudo chown \$(id -u):\$(id -g) \$HOME/.kube/config
kubectl taint nodes --all node-role.kubernetes.io/control-plane- 2>/dev/null || true
kubectl apply -f ${FLANNEL_URL}
for i in \$(seq 1 60); do
  kubectl get nodes 2>/dev/null | grep -q ' Ready ' && break
  sleep 5
done
kubectl get nodes
"

step "5/7 Installing ansible + kubescape"
limactl shell "${VM_NAME}" -- bash -c '
set -euxo pipefail
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3 ansible-core git
sudo bash /tmp/install-kubescape.sh
'

step "6/7 Cloning repo and writing single-node hosts.ini"
limactl shell "${VM_NAME}" -- bash -c "
set -euxo pipefail
[ -d \$HOME/k8s-hardening ] || git clone ${REPO_URL} \$HOME/k8s-hardening
cat > \$HOME/k8s-hardening/tier2-ansible/inventory/hosts.ini <<EOF
[control_plane]
localhost ansible_connection=local

[workers]
localhost ansible_connection=local

[all:vars]
ansible_python_interpreter=/usr/bin/python3
EOF
ansible -i \$HOME/k8s-hardening/tier2-ansible/inventory/hosts.ini all -m ping
"

if [[ "$RUN_HARDEN" = "1" ]]; then
  step "7/7 Running ./harden.py all"
  limactl shell "${VM_NAME}" -- bash -c '
cd $HOME/k8s-hardening
./harden.py all --inventory tier2-ansible/inventory/hosts.ini
'
else
  step "7/7 Skipping ./harden.py (set RUN_HARDEN=1 to invoke automatically)"
  echo
  echo "VM is ready. Run the pipeline:"
  echo "  limactl shell ${VM_NAME} -- bash -c 'cd \$HOME/k8s-hardening && ./harden.py all --inventory tier2-ansible/inventory/hosts.ini'"
fi

echo
echo "Reports (when generated) will live in the VM at \$HOME/k8s-hardening/reports."
echo "Pull them back with:"
echo "  limactl cp -r ${VM_NAME}:k8s-hardening/reports ./reports-lima"
echo
echo "Tear down with: bash scripts/lima-down.sh"
