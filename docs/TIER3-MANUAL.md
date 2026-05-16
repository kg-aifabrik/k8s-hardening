# Tier 3 - Manual operations

Items the automation deliberately leaves to a human.

## 1. Re-encrypt existing secrets after enabling KMS

Tier 2 enables encryption-at-rest. **New** secrets are encrypted immediately.
**Existing** secrets remain plaintext in etcd until re-written.

On a control plane node (or with admin kubeconfig):

```bash
kubectl get secrets --all-namespaces -o json \
  | kubectl replace -f -
```

This is a no-op functionally but triggers re-encryption.

## 2. OIDC integration

CIS 1.2.x calls for `--oidc-issuer-url`, `--oidc-client-id`, `--oidc-username-claim`.
These are org-specific (Okta/Auth0/Keycloak/Google Workspace).

Add to `tier2-ansible/roles/api-server/files/patch_apiserver.py` under
`CIS_FLAGS` once you've chosen an IdP:

```python
"--oidc-issuer-url":      "https://idp.example.com",
"--oidc-client-id":       "kubernetes",
"--oidc-username-claim":  "email",
"--oidc-groups-claim":    "groups",
```

Then re-run `./harden.py tier2`.

## 3. Image signature verification (cosign)

Add a Kyverno `verifyImages` policy after you have a signing pipeline:

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: verify-image-signatures
spec:
  validationFailureAction: Enforce
  rules:
    - name: verify-signed-by-org
      match:
        any:
          - resources:
              kinds: [Pod]
      verifyImages:
        - imageReferences: ["registry.example.com/*"]
          attestors:
            - entries:
                - keys:
                    publicKeys: |-
                      -----BEGIN PUBLIC KEY-----
                      ...
                      -----END PUBLIC KEY-----
```

## 4. Certificate rotation cadence

kubeadm certs expire annually. Set a calendar reminder + runbook:

```bash
kubeadm certs check-expiration
kubeadm certs renew all
systemctl restart kubelet
# restart static pods by touching their manifests
```

## 5. Network policy for kube-system

We deliberately *don't* default-deny kube-system in Tier 1 because CoreDNS,
kube-proxy, and the CNI need to function. Once your CNI is stable, add
per-component allow policies (CoreDNS in/out, kube-proxy node access, etc.)
and finally a default-deny in kube-system.

## 6. Audit log shipping

Tier 2 enables audit logging to `/var/log/kubernetes/audit.log` on each
control plane node. Ship those to your SIEM (Splunk, Elastic, Datadog,
CloudWatch via Fluent Bit, etc.). The orchestrator doesn't pick a destination.

## 7. Pod Security: kube-system to restricted

Tier 1 sets kube-system to `baseline` because some kubeadm components
need elevated permissions. Audit kube-system workloads with:

```bash
kubectl get events -n kube-system --field-selector reason=FailedCreate
```

Where feasible, swap components for restricted-compatible variants
(e.g., a CNI that doesn't need privileged), then label kube-system
`pod-security.kubernetes.io/enforce: restricted`.
