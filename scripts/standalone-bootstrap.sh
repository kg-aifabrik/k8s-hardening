#!/usr/bin/env bash
# Bootstrap a 1-CP + 2-worker kubeadm cluster on already-provisioned
# Ubuntu VMs, then leave it ready for ./harden.py to run from the CP.
#
# Run from your workstation. Each node is reached as `root` over SSH.
# The VMs must already exist, be reachable on the given IPs, and have
# your SSH public key in /root/.ssh/authorized_keys (DigitalOcean does
# this automatically when you select an SSH key at droplet creation).
#
# Usage:
#   CP_IP=1.2.3.4 W1_IP=5.6.7.8 W2_IP=9.10.11.12 bash standalone-bootstrap.sh
#
# Optional env:
#   SSH_KEY              Path to private key for the initial root@<node>
#                        connections (default: ~/.ssh/id_ed25519)
#   REPO_URL             https://github.com/kg-aifabrik/k8s-hardening.git
#   FLANNEL_URL          override the default Flannel manifest URL
#   K8S_POD_CIDR         default 10.244.0.0/16 (matches Flannel default)
#   RUN_HARDEN           if "1", invokes ./harden.py all on cp at the end
#
# What it does:
#   1. Verifies SSH to all 3 hosts.
#   2. Copies + runs prep-node.sh on each (sets hostname, installs
#      containerd + kubeadm v1.35).
#   3. Runs `kubeadm init` on cp with the public IP in the cert SANs.
#   4. Installs Flannel CNI.
#   5. Joins w1 and w2.
#   6. Waits for all 3 nodes Ready.
#   7. Sets up cp -> w1/w2 SSH (cp generates a key, distributes pubkey).
#   8. Installs python/ansible/git/kubescape on cp.
#   9. Clones this repo on cp and writes the multi-node hosts.ini.
#  10. Optionally runs `./harden.py all`.
set -euo pipefail

: "${CP_IP:?set CP_IP to the control-plane public IP}"
: "${W1_IP:?set W1_IP to worker 1 public IP}"
: "${W2_IP:?set W2_IP to worker 2 public IP}"

SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
REPO_URL="${REPO_URL:-https://github.com/kg-aifabrik/k8s-hardening.git}"
FLANNEL_URL="${FLANNEL_URL:-https://raw.githubusercontent.com/flannel-io/flannel/v0.25.1/Documentation/kube-flannel.yml}"
K8S_POD_CIDR="${K8S_POD_CIDR:-10.244.0.0/16}"
RUN_HARDEN="${RUN_HARDEN:-0}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREP_NODE_SH="${SCRIPT_DIR}/prep-node.sh"
INSTALL_KUBESCAPE_SH="${SCRIPT_DIR}/install-kubescape.sh"
[[ -f "$PREP_NODE_SH" ]] || { echo "missing $PREP_NODE_SH" >&2; exit 1; }
[[ -f "$INSTALL_KUBESCAPE_SH" ]] || { echo "missing $INSTALL_KUBESCAPE_SH" >&2; exit 1; }

SSH_OPTS=(-i "$SSH_KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
ssh_node() { ssh "${SSH_OPTS[@]}" "root@$1" "${@:2}"; }
scp_to_node() { scp "${SSH_OPTS[@]}" "$1" "root@$2:$3"; }

# Freshly-created cloud VMs often respond on TCP 22 with "connection
# refused" for the first 30-60s while cloud-init runs and sshd starts.
# Poll until each host actually answers an SSH command.
wait_for_ssh() {
  local ip="$1"
  local deadline=$(( $(date +%s) + 300 ))
  while [ $(date +%s) -lt $deadline ]; do
    if ssh "${SSH_OPTS[@]}" -o BatchMode=yes "root@$ip" true 2>/dev/null; then
      return 0
    fi
    sleep 5
  done
  echo "SSH never came up on $ip after 300s" >&2
  return 1
}

step() { printf '\n==== %s ====\n' "$*"; }

step "1/10 Waiting for SSH on all 3 hosts"
for ip in "$CP_IP" "$W1_IP" "$W2_IP"; do
  echo -n "  $ip ... "
  wait_for_ssh "$ip" || exit 1
  ssh_node "$ip" "hostname && uname -m"
done

step "2/10 Running prep-node.sh on every host (parallel)"
# Pair each IP with its desired hostname. Two parallel arrays beat an
# associative array here: bash treats dots in IP-shaped keys as
# arithmetic operators and silently mangles the lookups.
IPS=("$CP_IP" "$W1_IP" "$W2_IP")
HOSTNAMES=(cp w1 w2)
pids=()
for i in 0 1 2; do
  ip="${IPS[$i]}"
  name="${HOSTNAMES[$i]}"
  (
    scp_to_node "$PREP_NODE_SH" "$ip" /tmp/prep-node.sh
    ssh_node "$ip" "bash /tmp/prep-node.sh ${name}"
  ) > "/tmp/prep-${name}.log" 2>&1 &
  pids+=("$!")
done
fail=0
for pid in "${pids[@]}"; do wait "$pid" || fail=1; done
if [[ $fail -ne 0 ]]; then
  echo "prep-node.sh failed on at least one host; logs in /tmp/prep-*.log" >&2
  tail -20 /tmp/prep-cp.log /tmp/prep-w1.log /tmp/prep-w2.log >&2 2>/dev/null || true
  exit 1
fi

step "3/10 kubeadm init on cp ($CP_IP)"
if ssh_node "$CP_IP" "test -f /etc/kubernetes/admin.conf"; then
  echo "  cp already initialized (admin.conf exists) — skipping kubeadm init"
else
  ssh_node "$CP_IP" "kubeadm init \
    --pod-network-cidr=${K8S_POD_CIDR} \
    --apiserver-advertise-address=${CP_IP} \
    --apiserver-cert-extra-sans=${CP_IP}"
fi
ssh_node "$CP_IP" 'mkdir -p /root/.kube && cp -f /etc/kubernetes/admin.conf /root/.kube/config'

step "4/10 Installing Flannel CNI (kubectl apply is idempotent)"
ssh_node "$CP_IP" "kubectl apply -f ${FLANNEL_URL}"

step "5/10 Joining workers"
JOIN_CMD=$(ssh_node "$CP_IP" "kubeadm token create --print-join-command")
echo "Join command: $JOIN_CMD"
for ip in "$W1_IP" "$W2_IP"; do
  if ssh_node "$ip" "test -f /etc/kubernetes/kubelet.conf"; then
    echo "  $ip already joined (kubelet.conf exists) — skipping"
  else
    ssh_node "$ip" "$JOIN_CMD" &
  fi
done
wait

step "6/10 Waiting for all 3 nodes Ready"
ssh_node "$CP_IP" 'for i in $(seq 1 60); do
  R=$(kubectl get nodes --no-headers 2>/dev/null | grep -c " Ready ")
  echo "Ready: $R/3"
  [ "$R" = "3" ] && break
  sleep 5
done
kubectl get nodes'

step "7/10 Setting up cp -> workers SSH"
ssh_node "$CP_IP" '[ -f /root/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -N "" -f /root/.ssh/id_ed25519'
CP_PUB=$(ssh_node "$CP_IP" 'cat /root/.ssh/id_ed25519.pub')
for ip in "$W1_IP" "$W2_IP"; do
  ssh_node "$ip" "grep -qF '$CP_PUB' /root/.ssh/authorized_keys || echo '$CP_PUB' >> /root/.ssh/authorized_keys"
done
ssh_node "$CP_IP" "ssh -o StrictHostKeyChecking=accept-new root@${W1_IP} hostname && \
                   ssh -o StrictHostKeyChecking=accept-new root@${W2_IP} hostname"

step "8/10 Installing python/ansible/git/kubescape on cp"
ssh_node "$CP_IP" 'DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-pip ansible-core git'
scp_to_node "$INSTALL_KUBESCAPE_SH" "$CP_IP" /tmp/install-kubescape.sh
ssh_node "$CP_IP" 'bash /tmp/install-kubescape.sh'

step "9/10 Cloning repo and writing hosts.ini on cp"
ssh_node "$CP_IP" "[ -d /root/k8s-hardening ] || git clone ${REPO_URL} /root/k8s-hardening"
ssh_node "$CP_IP" "cat > /root/k8s-hardening/tier2-ansible/inventory/hosts.ini <<EOF
[control_plane]
cp ansible_host=${CP_IP} ansible_connection=local

[workers]
w1 ansible_host=${W1_IP}
w2 ansible_host=${W2_IP}

[all:vars]
ansible_user=root
ansible_ssh_private_key_file=/root/.ssh/id_ed25519
ansible_python_interpreter=/usr/bin/python3
ansible_ssh_common_args='-o StrictHostKeyChecking=no'
EOF
ansible -i /root/k8s-hardening/tier2-ansible/inventory/hosts.ini all -m ping"

if [[ "$RUN_HARDEN" = "1" ]]; then
  step "10/10 Running ./harden.py all"
  ssh_node "$CP_IP" 'cd /root/k8s-hardening && ./harden.py all --inventory tier2-ansible/inventory/hosts.ini'
else
  step "10/10 Skipping ./harden.py (set RUN_HARDEN=1 to invoke automatically)"
  echo
  echo "Cluster is ready. To run the pipeline:"
  echo "  ssh root@${CP_IP} 'cd /root/k8s-hardening && ./harden.py all --inventory tier2-ansible/inventory/hosts.ini'"
fi

echo
echo "Done. Reports (if generated) will land under /root/k8s-hardening/reports on cp."
echo "Pull them back with:"
echo "  scp -r root@${CP_IP}:/root/k8s-hardening/reports ./reports-standalone"
