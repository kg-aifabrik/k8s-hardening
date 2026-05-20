# Post-Hardening CIS Scan

Generated: 2026-05-20T19:55:10.111687+00:00

## Summary

| Tool | Pass | Fail | Warn | Score |
|------|------|------|------|-------|
| kube-bench | 76 | 9 | 45 | 58.5% |
| kubescape | 48 | 27 | 54 | 50.2% |

## kube-bench - Failed Controls (9)

### 4.1.1 - Ensure that the kubelet service file permissions are set to 600 or more restrictive (Automated)

**Remediation:**

```
Run the below command (based on the file location on your system) on the each worker node.
For example, chmod 600 /lib/systemd/system/kubelet.service
```

### 5.1.1 - Ensure that the cluster-admin role is only used where required (Automated)

**Remediation:**

```
Identify all clusterrolebindings to the cluster-admin role. Check if they are used and
if they need this role or if they could use a role with fewer privileges.
Where possible, first bind users to a lower privileged role and then remove the
clusterrolebinding to the cluster-admin role : kubectl delete clusterrolebinding [name]
Condition: is_compliant is false if rolename is not cluster-admin and rolebinding is cluster-admin.
```

### 5.1.2 - Minimize access to secrets (Automated)

**Remediation:**

```
Where possible, remove get, list and watch access to Secret objects in the cluster.
```

### 5.1.3 - Minimize wildcard use in Roles and ClusterRoles (Automated)

**Remediation:**

```
Where possible replace any use of wildcards ["*"] in roles and clusterroles with specific
objects or actions.
Condition: role_is_compliant is false if ["*"] is found in rules.
Condition: clusterrole_is_compliant is false if ["*"] is found in rules.
```

### 5.1.4 - Minimize access to create pods (Automated)

**Remediation:**

```
Where possible, remove create access to pod objects in the cluster.
```

### 5.1.5 - Ensure that default service accounts are not actively used (Automated)

**Remediation:**

```
Create explicit service accounts wherever a Kubernetes workload requires specific access
to the Kubernetes API server.
Modify the configuration of each default service account to include this value
`automountServiceAccountToken: false`.
```

### 5.1.6 - Ensure that Service Account Tokens are only mounted where necessary (Automated)

**Remediation:**

```
Modify the definition of ServiceAccounts and Pods which do not need to mount service
account tokens to disable it, with `automountServiceAccountToken: false`.
If both the ServiceAccount and the Pod's .spec specify a value for automountServiceAccountToken, the Pod spec takes precedence.
Condition: Pod is_compliant to true when
  - ServiceAccount is automountServiceAccountToken: false and Pod is automountServiceAccountToken: false or notset
  - ServiceAccount is automountServiceAccountToken: true notset and Pod is automountServiceAccountToken: false
```

### 1.1.12 - Ensure that the etcd data directory ownership is set to etcd:etcd (Automated)

**Remediation:**

```
On the etcd server node, get the etcd data directory, passed as an argument --data-dir,
from the command 'ps -ef | grep etcd'.
Run the below command (based on the etcd data directory found above).
For example, chown etcd:etcd /var/lib/etcd
```

### 1.2.5 - Ensure that the --kubelet-certificate-authority argument is set as appropriate (Automated)

**Remediation:**

```
Follow the Kubernetes documentation and setup the TLS connection between
the apiserver and kubelets. Then, edit the API server pod specification file
/etc/kubernetes/manifests/kube-apiserver.yaml on the control plane node and set the
--kubelet-certificate-authority parameter to the path to the cert file for the certificate authority.
--kubelet-certificate-authority=<ca-string>
```


## kubescape - Failed Controls (27)

### C-0041 - CIS-5.2.5 Minimize the admission of containers wishing to share the host network namespace

**Remediation:**

```
compliance 92.30769% - see `kubescape scan control C-0041`
```

### C-0185 - CIS-5.1.1 Ensure that the cluster-admin role is only used where required

**Remediation:**

```
compliance 98.24561% - see `kubescape scan control C-0185`
```

### C-0186 - CIS-5.1.2 Minimize access to secrets

**Remediation:**

```
compliance 91.13924% - see `kubescape scan control C-0186`
```

### C-0187 - CIS-5.1.3 Minimize wildcard use in Roles and ClusterRoles

**Remediation:**

```
compliance 98.73418% - see `kubescape scan control C-0187`
```

### C-0188 - CIS-5.1.4 Minimize access to create pods

**Remediation:**

```
compliance 98.73418% - see `kubescape scan control C-0188`
```

### C-0189 - CIS-5.1.5 Ensure that default service accounts are not actively used.

**Remediation:**

```
compliance 83.33333% - see `kubescape scan control C-0189`
```

### C-0190 - CIS-5.1.6 Ensure that Service Account Tokens are only mounted where necessary

**Remediation:**

```
compliance 76.666664% - see `kubescape scan control C-0190`
```

### C-0191 - CIS-5.1.8 Limit use of the Bind, Impersonate and Escalate permissions in the Kubernetes cluster

**Remediation:**

```
compliance 98.73418% - see `kubescape scan control C-0191`
```

### C-0193 - CIS-5.2.2 Minimize the admission of privileged containers

**Remediation:**

```
compliance 11.111112% - see `kubescape scan control C-0193`
```

### C-0197 - CIS-5.2.6 Minimize the admission of containers with allowPrivilegeEscalation

**Remediation:**

```
compliance 11.111112% - see `kubescape scan control C-0197`
```

### C-0198 - CIS-5.2.7 Minimize the admission of root containers

**Remediation:**

```
compliance 11.111112% - see `kubescape scan control C-0198`
```

### C-0199 - CIS-5.2.8 Minimize the admission of containers with the NET\_RAW capability

**Remediation:**

```
compliance 11.111112% - see `kubescape scan control C-0199`
```

### C-0200 - CIS-5.2.9 Minimize the admission of containers with added capabilities

**Remediation:**

```
compliance 11.111112% - see `kubescape scan control C-0200`
```

### C-0201 - CIS-5.2.10 Minimize the admission of containers with capabilities assigned

**Remediation:**

```
compliance 11.111112% - see `kubescape scan control C-0201`
```

### C-0202 - CIS-5.2.11 Minimize the admission of Windows HostProcess Containers

**Remediation:**

```
compliance 11.111112% - see `kubescape scan control C-0202`
```

### C-0203 - CIS-5.2.12 Minimize the admission of HostPath volumes

**Remediation:**

```
compliance 11.111112% - see `kubescape scan control C-0203`
```

### C-0204 - CIS-5.2.13 Minimize the admission of containers which use HostPorts

**Remediation:**

```
compliance 11.111112% - see `kubescape scan control C-0204`
```

### C-0206 - CIS-5.3.2 Ensure that all Namespaces have Network Policies defined

**Remediation:**

```
compliance 66.66667% - see `kubescape scan control C-0206`
```

### C-0209 - CIS-5.7.1 Create administrative boundaries between resources using namespaces

**Remediation:**

```
compliance 66.66667% - see `kubescape scan control C-0209`
```

### C-0210 - CIS-5.7.2 Ensure that the seccomp profile is set to docker/default in your pod definitions

**Remediation:**

```
compliance 92.30769% - see `kubescape scan control C-0210`
```

### C-0211 - CIS-5.7.3 Apply Security Context to Your Pods and Containers

**Remediation:**

```
compliance 23.076923% - see `kubescape scan control C-0211`
```

### C-0212 - CIS-5.7.4 The default namespace should not be used

**Remediation:**

```
compliance 97.98658% - see `kubescape scan control C-0212`
```

### C-0278 - CIS-5.1.9 Minimize access to create persistent volumes

**Remediation:**

```
compliance 98.73418% - see `kubescape scan control C-0278`
```

### C-0279 - CIS-5.1.10 Minimize access to the proxy sub-resource of nodes

**Remediation:**

```
compliance 94.936714% - see `kubescape scan control C-0279`
```

### C-0280 - CIS-5.1.11 Minimize access to the approval sub-resource of certificatesigningrequests objects

**Remediation:**

```
compliance 98.73418% - see `kubescape scan control C-0280`
```

### C-0281 - CIS-5.1.12 Minimize access to webhook configuration objects

**Remediation:**

```
compliance 96.20254% - see `kubescape scan control C-0281`
```

### C-0282 - CIS-5.1.13 Minimize access to the service account token creation

**Remediation:**

```
compliance 98.73418% - see `kubescape scan control C-0282`
```
