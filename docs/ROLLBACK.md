# Rollback

## Tier 1 (cluster manifests)

Fully reversible:

```bash
# Remove Kyverno policies first (so admission stops enforcing)
kubectl delete -f tier1-manifests/kyverno-policies/

# Remove other manifests
kubectl delete -f tier1-manifests/03-rbac-hardening.yaml
kubectl delete -f tier1-manifests/02-disable-default-sa-automount.yaml  # re-creates default SAs without patch
kubectl delete -f tier1-manifests/01-default-deny-netpol.yaml
# Note: PSS namespace labels persist; remove with kubectl label

# Uninstall Kyverno
kubectl delete -f https://github.com/kyverno/kyverno/releases/download/v1.12.5/install.yaml
```

PSS namespace labels need a manual `kubectl label namespace <ns>
pod-security.kubernetes.io/enforce-` if you want to unset them.

## Tier 2 (node-level)

### Kubelet config

```bash
# on each node
cp /var/lib/kubelet/config.yaml.bak /var/lib/kubelet/config.yaml
systemctl restart kubelet
```

### Static pod manifests (apiserver, kcm, scheduler)

The patchers don't keep backups by default. Use kubeadm to regenerate:

```bash
# on each control plane node
kubeadm init phase control-plane all --config /var/lib/kubeadm-config.yaml
```

If you don't have the original kubeadm config, the safest path is to
extract the running config and remove the flags you added. The CIS_FLAGS
dict in each `patch_*.py` script is the canonical list of what was added.

### etcd encryption

```bash
# on each control plane node
# 1. Remove the --encryption-provider-config flag from kube-apiserver manifest
# 2. Remove the encryption-config volume + volumeMount
# 3. kubelet will restart the apiserver
# 4. Re-write existing secrets so they're re-stored unencrypted:
kubectl get secrets -A -o json | kubectl replace -f -
# 5. rm /etc/kubernetes/encryption-config.yaml
```

### File permissions (common role)

The `common/` role tightens permissions only. Loosening them isn't typically
desired, but if needed:

```bash
chmod 0644 /etc/kubernetes/admin.conf  # original default
# etc.
```

## Full reset

If the cluster is fresh and you'd rather start over than untangle changes:

```bash
# on each node
kubeadm reset --force
rm -rf /etc/kubernetes /var/lib/etcd /var/lib/kubelet
# then kubeadm init / kubeadm join from scratch
```
