#!/usr/bin/env bash
# Stop and delete the Lima VM. Reports inside the VM are lost — pull
# them first with `limactl cp -r <vm>:k8s-hardening/reports ./reports-lima`.
set -euo pipefail
VM_NAME="${VM_NAME:-k8s-harden}"
limactl stop "${VM_NAME}" 2>/dev/null || true
limactl delete "${VM_NAME}" 2>/dev/null || true
echo "VM ${VM_NAME} stopped and deleted."
