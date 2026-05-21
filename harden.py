#!/usr/bin/env python3
"""
k8s-cis-hardening orchestrator

Workflow:
  1. baseline  - run kube-bench + kubescape, save initial report
  2. tier1     - apply cluster-level kubectl manifests (PSS, NetworkPolicy, Kyverno, RBAC)
  3. tier2     - run Ansible playbook for node-level fixes (API server, kubelet, etcd, file perms)
  4. validate  - re-run scans, diff against baseline, write delta report
  5. all       - run 1->4 in sequence

Designed for: fresh upstream Kubernetes cluster, no workloads.
NOT designed for: clusters with production traffic (some Tier 2 fixes restart control plane).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
SCAN_DIR = ROOT / "scan"
TIER1_DIR = ROOT / "tier1-manifests"
TIER2_DIR = ROOT / "tier2-ansible"
WORKLOADS_DIR = ROOT / "workloads"
REPORT_DIR = ROOT / "reports"
KYVERNO_VERSION = "v1.12.5"  # pinned; bump deliberately
KYVERNO_INSTALL_URL = (
    f"https://github.com/kyverno/kyverno/releases/download/{KYVERNO_VERSION}/install.yaml"
)
# Pinned upstream URLs for admin v1 cluster add-ons. These are
# upstream-published release manifests; vendoring them locally would
# add thousands of lines to the repo with no benefit.
CERT_MANAGER_VERSION = "v1.15.3"
CERT_MANAGER_URL = (
    f"https://github.com/cert-manager/cert-manager/releases/download/"
    f"{CERT_MANAGER_VERSION}/cert-manager.yaml"
)
METRICS_SERVER_VERSION = "v0.7.2"
METRICS_SERVER_URL = (
    f"https://github.com/kubernetes-sigs/metrics-server/releases/download/"
    f"{METRICS_SERVER_VERSION}/components.yaml"
)
# Storage provisioner for vanilla kubeadm clusters. Managed K8s
# (EKS / GKE / AKS) ships its own default StorageClass — we apply
# local-path-provisioner unconditionally (kubectl apply is idempotent)
# but only promote it to default if no other default already exists.
LOCAL_PATH_PROVISIONER_VERSION = "v0.0.31"
LOCAL_PATH_PROVISIONER_URL = (
    f"https://raw.githubusercontent.com/rancher/local-path-provisioner/"
    f"{LOCAL_PATH_PROVISIONER_VERSION}/deploy/local-path-storage.yaml"
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] [{level}] {msg}", flush=True)


def run(cmd: list[str], check: bool = True, capture: bool = False,
        env: Optional[dict] = None) -> subprocess.CompletedProcess:
    log(f"$ {' '.join(cmd)}", "EXEC")
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
        env={**os.environ, **(env or {})},
    )


def require(binaries: list[str]) -> None:
    missing = [b for b in binaries if subprocess.run(
        ["which", b], capture_output=True).returncode != 0]
    if missing:
        log(f"missing required binaries: {missing}", "FATAL")
        sys.exit(2)


def timestamped_dir(parent: Path, prefix: str) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    d = parent / f"{prefix}_{ts}"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# scanners
# ---------------------------------------------------------------------------

@dataclass
class ScanResult:
    tool: str
    pass_count: int = 0
    fail_count: int = 0
    warn_count: int = 0
    score: float = 0.0
    raw_path: Optional[Path] = None
    failures: list[dict] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.pass_count + self.fail_count + self.warn_count


# kube-bench Control IDs that only apply to the control-plane node.
# kube-bench numbers CIS sections 1..5: 1=Master (apiserver/kcm/scheduler),
# 2=Etcd, 3=Control Plane Configuration, 4=Worker Node, 5=Policies.
# When a worker pod runs these sections it just reports "file not found"
# failures — we drop them so workers only contribute node + policies
# coverage.
_CP_ONLY_IDS = {"1", "2", "3"}


def _is_control_plane_node(node_name: str) -> bool:
    cp = run(
        ["kubectl", "get", "node", node_name,
         "-o", "jsonpath={.metadata.labels}"],
        check=False, capture=True,
    )
    return "node-role.kubernetes.io/control-plane" in cp.stdout


def run_kube_bench(outdir: Path) -> ScanResult:
    """
    Runs kube-bench as a DaemonSet so both control-plane and worker nodes
    are scanned (a Job lands on one pod and misses master controls on a
    real cluster where the CP carries a NoSchedule taint).

    Per-pod JSON is collected and aggregated. Control sections that only
    exist on the CP (master/etcd/controlplane) are only counted from the
    CP pod; node/policies sections are counted from every pod.
    """
    log("running kube-bench (DaemonSet)...")
    job_manifest = SCAN_DIR / "kube-bench-job.yaml"
    run(["kubectl", "apply", "-f", str(job_manifest)])

    # Wait for the DaemonSet to roll out across all nodes.
    run(["kubectl", "-n", "kube-bench-scan", "rollout", "status",
         "ds/kube-bench", "--timeout=300s"], check=False)

    # Enumerate pods with their node names.
    cp = run(
        ["kubectl", "-n", "kube-bench-scan", "get", "pods",
         "-l", "app=kube-bench", "-o", "json"],
        check=False, capture=True,
    )
    try:
        items = json.loads(cp.stdout).get("items", [])
    except json.JSONDecodeError:
        items = []
    pods = [(it["metadata"]["name"], it["spec"].get("nodeName", ""))
            for it in items]
    if not pods:
        log("kube-bench DaemonSet produced no pods", "ERROR")
        return ScanResult(tool="kube-bench")

    result = ScanResult(tool="kube-bench")
    seen = set()  # (test_id, scope) so we don't double-count node checks
    per_pod_raw: dict[str, dict] = {}

    for pod_name, node_name in pods:
        # Poll until the kube-bench JSON parses (it emits a single object,
        # then `sleep 86400` keeps the pod alive).
        deadline = time.time() + 600
        data = None
        while time.time() < deadline:
            raw = run(["kubectl", "-n", "kube-bench-scan", "logs", pod_name],
                      check=False, capture=True).stdout
            try:
                data = json.loads(raw)
                break
            except json.JSONDecodeError:
                time.sleep(5)
        if data is None:
            log(f"pod {pod_name} on {node_name} never produced parseable JSON",
                "WARN")
            continue
        per_pod_raw[pod_name] = {"node": node_name, "data": data}

        is_cp = _is_control_plane_node(node_name)
        for ctrl in data.get("Controls", []):
            cid = str(ctrl.get("id") or "")
            cp_only = cid in _CP_ONLY_IDS
            if cp_only and not is_cp:
                # Worker can't see CP files; skip the empty/failing section.
                continue
            for test in ctrl.get("tests", []):
                for r in test.get("results", []):
                    test_id = r.get("test_number", "")
                    # Node + policies checks repeat across pods; dedupe to
                    # the first occurrence.
                    if not cp_only:
                        key = (test_id,)
                        if key in seen:
                            continue
                        seen.add(key)
                    status = r.get("status")
                    if status == "PASS":
                        result.pass_count += 1
                    elif status == "FAIL":
                        result.fail_count += 1
                        result.failures.append({
                            "id": test_id,
                            "desc": r.get("test_desc"),
                            "node": node_name,
                            "remediation": r.get("remediation", "").strip(),
                        })
                    elif status == "WARN":
                        result.warn_count += 1

    raw_path = outdir / "kube-bench.json"
    raw_path.write_text(json.dumps(per_pod_raw, indent=2))
    result.raw_path = raw_path

    if result.total:
        result.score = round(100 * result.pass_count / result.total, 1)

    # cleanup
    run(["kubectl", "delete", "-f", str(job_manifest), "--ignore-not-found"],
        check=False)
    return result


def run_kubescape(outdir: Path) -> ScanResult:
    """
    Runs kubescape locally against the current kubeconfig context.
    Requires `kubescape` binary on PATH.
    """
    log("running kubescape (framework=cis-v1.10.0)...")
    raw_path = outdir / "kubescape.json"
    cp = run(
        ["kubescape", "scan", "framework", "cis-v1.10.0",
         "--format", "json", "--output", str(raw_path)],
        check=False, capture=True,
    )
    if cp.returncode not in (0, 1):  # 1 = findings present
        log(f"kubescape exited unexpectedly: {cp.returncode}", "WARN")

    result = ScanResult(tool="kubescape", raw_path=raw_path)
    if not raw_path.exists():
        return result
    try:
        data = json.loads(raw_path.read_text())
        summary = data.get("summaryDetails", {})
        controls = summary.get("controls", {})
        for cid, c in controls.items():
            status = c.get("status", "")
            if status == "passed":
                result.pass_count += 1
            elif status == "failed":
                result.fail_count += 1
                result.failures.append({
                    "id": cid,
                    "desc": c.get("name", ""),
                    "remediation": (
                        f"compliance {c.get('complianceScore', '?')}% - "
                        f"see `kubescape scan control {cid}`"),
                })
            else:  # skipped / irrelevant / etc.
                result.warn_count += 1
        # Prefer kubescape's own compliance score; fall back to pass ratio.
        cs = summary.get("complianceScore")
        if isinstance(cs, (int, float)):
            result.score = round(cs, 1)
        elif result.total:
            result.score = round(100 * result.pass_count / result.total, 1)
    except (json.JSONDecodeError, KeyError, AttributeError, TypeError) as e:
        log(f"could not parse kubescape JSON: {e}", "WARN")
    return result


# ---------------------------------------------------------------------------
# phases
# ---------------------------------------------------------------------------

def _scan_and_report(outdir: Path, filename: str, title: str,
                     phase_label: str) -> dict[str, ScanResult]:
    log(f"=== PHASE: {phase_label} ===")
    results = {
        "kube-bench": run_kube_bench(outdir),
        "kubescape":  run_kubescape(outdir),
    }
    write_report(outdir / filename, title, results)
    write_scores(outdir, results)
    return results


def phase_baseline(outdir: Path) -> dict[str, ScanResult]:
    return _scan_and_report(outdir, "baseline.md",
                            "Baseline CIS Scan", "baseline")


def phase_assess(outdir: Path) -> dict[str, ScanResult]:
    """
    Same scans as `baseline` but framed as a standalone posture
    audit: writes assessment.md instead of baseline.md and lands
    under reports/assess_<ts>/. No subsequent hardening expected.
    """
    return _scan_and_report(outdir, "assessment.md",
                            "Cluster Security Posture Assessment",
                            "assess")


def phase_tier1() -> None:
    log("=== PHASE: tier1 (cluster manifests) ===")

    # Kyverno must be installed before its policies.
    # Server-side apply is required: the Kyverno CRDs exceed the 262144-byte
    # limit on the client-side last-applied-configuration annotation.
    log("installing Kyverno...")
    run(["kubectl", "apply", "--server-side", "--force-conflicts",
         "-f", KYVERNO_INSTALL_URL])
    log("waiting for Kyverno to be ready...")
    run(["kubectl", "-n", "kyverno", "wait", "--for=condition=Available",
         "deployment", "--all", "--timeout=300s"])

    # Apply ordered manifests
    manifests = sorted(TIER1_DIR.glob("*.yaml"))
    for m in manifests:
        run(["kubectl", "apply", "-f", str(m)])

    # Kyverno policies last
    policies = sorted((TIER1_DIR / "kyverno-policies").glob("*.yaml"))
    for p in policies:
        run(["kubectl", "apply", "-f", str(p)])

    log(f"tier1 applied: {len(manifests)} manifests + {len(policies)} policies")


def phase_tier2(inventory: str, become_pass: Optional[str] = None) -> None:
    log("=== PHASE: tier2 (node-level via Ansible) ===")
    inv = Path(inventory)
    if not inv.exists():
        log(f"inventory not found: {inventory}", "FATAL")
        sys.exit(2)

    cmd = [
        "ansible-playbook",
        "-i", str(inv),
        str(TIER2_DIR / "playbook.yml"),
    ]
    env = {}
    if become_pass:
        env["ANSIBLE_BECOME_PASS"] = become_pass
    run(cmd, env=env)


def wait_for_apiserver(timeout: int = 300) -> None:
    """
    Block until /healthz returns ok. Tier 2 patches static-pod manifests
    on the control plane, which causes the kubelet to restart
    kube-apiserver; the API briefly returns "connection refused" during
    the window between old pod termination and new pod readiness.
    Running validate immediately after Tier 2 reliably hit this race.
    """
    log("waiting for API server to become healthy...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        cp = run(["kubectl", "get", "--raw=/healthz"],
                 check=False, capture=True)
        if cp.returncode == 0 and "ok" in cp.stdout:
            log("API server healthy")
            return
        time.sleep(5)
    log(f"API server did not become healthy within {timeout}s; "
        "proceeding anyway", "WARN")


def wait_for_kyverno(timeout: int = 300) -> None:
    """
    If Kyverno is installed, wait until its admission Deployment is
    Available. Otherwise validate's `kubectl apply` of the kube-bench
    DaemonSet fails with "failed calling webhook ... connection refused"
    because the kube-apiserver came back faster than Kyverno's pods.
    Skips silently when the `kyverno` namespace doesn't exist.
    """
    cp = run(["kubectl", "get", "ns", "kyverno"], check=False, capture=True)
    if cp.returncode != 0:
        return  # Kyverno not installed (e.g., --skip-tier2 path on a fresh cluster)
    log("waiting for Kyverno admission webhook to become ready...")
    run(["kubectl", "-n", "kyverno", "wait", "--for=condition=Available",
         "deployment/kyverno-admission-controller",
         f"--timeout={timeout}s"], check=False)


def phase_validate(baseline_dir: Path, outdir: Path) -> None:
    log("=== PHASE: validate ===")
    wait_for_apiserver()
    wait_for_kyverno()
    results = {
        "kube-bench": run_kube_bench(outdir),
        "kubescape":  run_kubescape(outdir),
    }
    write_report(outdir / "post-hardening.md",
                 "Post-Hardening CIS Scan", results)
    write_scores(outdir, results)
    write_diff(baseline_dir, outdir, results)


# ---------------------------------------------------------------------------
# workload validation harness  (issue #1)
# ---------------------------------------------------------------------------

def _kubectl_apply_stdin(yaml_text: str, env: Optional[dict] = None) -> None:
    """Apply rendered YAML to the cluster via `kubectl apply -f -`."""
    log("$ kubectl apply -f -  (stdin: rendered template)", "EXEC")
    full_env = {**os.environ, **(env or {})}
    p = subprocess.Popen(["kubectl", "apply", "-f", "-"],
                         stdin=subprocess.PIPE, text=True, env=full_env)
    p.communicate(yaml_text)
    if p.returncode != 0:
        raise subprocess.CalledProcessError(p.returncode, "kubectl apply")


def create_tenant(tenant: str, namespace: Optional[str] = None) -> Path:
    """
    Idempotently create a tenant boundary (namespace + ServiceAccount +
    Role + RoleBinding + long-lived token Secret) and write a
    kubeconfig file scoped to the tenant's deployer SA. Returns the
    kubeconfig path.

    Subsequent `kubectl` calls using `KUBECONFIG=<path>` then act as
    the tenant — they can deploy into their namespace and nowhere else.
    """
    import base64
    ns = namespace or tenant
    log(f"creating tenant {tenant} (namespace {ns})...")

    tmpl = WORKLOADS_DIR / "tenant" / "_rbac" / "tenant.yaml.tmpl"
    rendered = (tmpl.read_text()
                .replace("__NS__", ns)
                .replace("__TENANT__", tenant))
    _kubectl_apply_stdin(rendered)

    # Wait for the SA token Secret to be populated by the
    # token-controller (k8s 1.24+ requires the Secret to exist with the
    # right annotation, then the controller fills in `token` + `ca.crt`).
    deadline = time.time() + 120
    token = ca_b64 = None
    while time.time() < deadline:
        cp = run(["kubectl", "-n", ns, "get", "secret",
                  "tenant-deployer-token", "-o", "json"],
                 check=False, capture=True)
        if cp.returncode == 0:
            data = json.loads(cp.stdout).get("data", {}) or {}
            token_b64 = data.get("token", "")
            ca_b64 = data.get("ca.crt", "")
            if token_b64 and ca_b64:
                token = base64.b64decode(token_b64).decode()
                break
        time.sleep(2)
    if not token or not ca_b64:
        raise RuntimeError(
            f"tenant-deployer-token never populated in ns {ns}; "
            "check that the token-controller is running")

    server = run(["kubectl", "config", "view", "--minify",
                  "-o", "jsonpath={.clusters[0].cluster.server}"],
                 capture=True).stdout.strip()
    if not server:
        raise RuntimeError("could not read cluster server URL from "
                           "the current kubeconfig")

    REPORT_DIR.mkdir(exist_ok=True)
    kubeconfig_path = REPORT_DIR / f"kubeconfig-{tenant}.yaml"
    kubeconfig_path.write_text(
        f"apiVersion: v1\n"
        f"kind: Config\n"
        f"clusters:\n"
        f"  - name: cluster\n"
        f"    cluster:\n"
        f"      server: {server}\n"
        f"      certificate-authority-data: {ca_b64}\n"
        f"contexts:\n"
        f"  - name: {tenant}\n"
        f"    context:\n"
        f"      cluster: cluster\n"
        f"      namespace: {ns}\n"
        f"      user: {tenant}-deployer\n"
        f"current-context: {tenant}\n"
        f"users:\n"
        f"  - name: {tenant}-deployer\n"
        f"    user:\n"
        f"      token: {token}\n"
    )
    kubeconfig_path.chmod(0o600)
    log(f"wrote tenant kubeconfig: {kubeconfig_path}")
    return kubeconfig_path


def phase_workload_deploy(kind: str, version: str,
                          tenant: Optional[str] = None,
                          kubeconfig: Optional[Path] = None) -> None:
    """
    Deploy a layer of workloads.
      kind=admin,  version=v1     → cert-manager + ClusterIssuer +
                                    metrics-server + node-debug DS
      kind=admin,  version=v2     → logging ns + PolicyException +
                                    promtail DS
      kind=tenant, version=v1     → 8 tenant workloads (requires
                                    --tenant + --kubeconfig)
      kind=tenant, version=harness→ background-job (same requirements)
    """
    log(f"=== PHASE: workload-deploy {kind}/{version} "
        f"{('tenant=' + tenant) if tenant else ''} ===")

    if kind == "admin" and version == "v1":
        # local-path-provisioner: gives kubeadm clusters a working
        # default StorageClass so the db StatefulSet's PVC can bind.
        # On managed K8s it's harmless (won't override the cloud
        # provisioner unless we explicitly promote it).
        log("installing local-path-provisioner")
        run(["kubectl", "apply", "-f", LOCAL_PATH_PROVISIONER_URL])
        # Promote to default only if no default already exists.
        sc_default = run(
            ["kubectl", "get", "storageclass", "-o",
             "jsonpath={range .items[?(@.metadata.annotations."
             "storageclass\\.kubernetes\\.io/is-default-class==\"true\")]}"
             "{.metadata.name}{\"\\n\"}{end}"],
            check=False, capture=True).stdout.strip()
        if not sc_default:
            log("no default StorageClass found; promoting local-path")
            run(["kubectl", "patch", "storageclass", "local-path",
                 "--type", "merge", "-p",
                 '{"metadata":{"annotations":'
                 '{"storageclass.kubernetes.io/is-default-class":"true"}}}'],
                check=False)
        else:
            log(f"keeping existing default StorageClass ({sc_default})")

        # cert-manager: install the bundled manifest then wait for the
        # admission webhook backend to come up. Until it's ready,
        # subsequent ClusterIssuer/Certificate apply attempts will fail
        # with "failed calling webhook".
        log("installing cert-manager (~30s for CRDs + webhook)")
        run(["kubectl", "apply", "-f", CERT_MANAGER_URL,
             "--server-side", "--force-conflicts"])
        run(["kubectl", "-n", "cert-manager", "wait",
             "--for=condition=Available", "deployment", "--all",
             "--timeout=300s"])

        log("installing metrics-server")
        # metrics-server requires the kubelet's serving cert to be
        # signed by the cluster CA. On a vanilla kubeadm cluster the
        # kubelet uses self-signed certs by default, so we patch in
        # --kubelet-insecure-tls. Same patch as the upstream "HA"
        # variant uses; safe on a single-CP kubeadm cluster.
        run(["kubectl", "apply", "-f", METRICS_SERVER_URL])
        run(["kubectl", "-n", "kube-system", "patch", "deployment",
             "metrics-server", "--type", "json", "-p",
             '[{"op":"add","path":"/spec/template/spec/containers/0/args/-",'
             '"value":"--kubelet-insecure-tls"}]'], check=False)

        # ClusterIssuer + node-debug DS — local YAML files. The
        # ClusterIssuer apply specifically can race with cert-manager's
        # webhook coming up; retry it for ~2 min.
        for f in sorted((WORKLOADS_DIR / "admin" / "v1").glob("*.yaml")):
            deadline = time.time() + 120
            while True:
                cp = run(["kubectl", "apply", "-f", str(f)],
                         check=False, capture=True)
                if cp.returncode == 0:
                    break
                if time.time() > deadline:
                    log(f"could not apply {f.name}: {cp.stderr}", "ERROR")
                    raise subprocess.CalledProcessError(
                        cp.returncode, cp.args, cp.stdout, cp.stderr)
                log(f"retrying apply of {f.name} (webhook may be warming)",
                    "WARN")
                time.sleep(10)

    elif kind == "admin" and version == "v2":
        for f in sorted((WORKLOADS_DIR / "admin" / "v2").glob("*.yaml")):
            run(["kubectl", "apply", "-f", str(f)])

    elif kind == "tenant":
        if not tenant or not kubeconfig:
            raise ValueError("tenant workloads need --tenant and --kubeconfig")
        env = {"KUBECONFIG": str(kubeconfig)}
        subdir = WORKLOADS_DIR / "tenant" / version
        if not subdir.is_dir():
            raise ValueError(
                f"unknown tenant workload set: {version} "
                f"(expected one of: v1, harness)")
        for f in sorted(subdir.glob("*.yaml")):
            run(["kubectl", "apply", "-f", str(f)], env=env)
    else:
        raise ValueError(f"unknown workload kind/version: {kind}/{version}")


def wait_for_tenant_workloads(namespace: str,
                              kubeconfig: Optional[Path] = None,
                              timeout: int = 600) -> None:
    """
    Wait for every tenant workload's pods to report Ready. Uses the
    tenant's kubeconfig so we exercise the actual auth path too.
    """
    log(f"waiting for tenant workloads in {namespace} to be Ready...")
    env = {"KUBECONFIG": str(kubeconfig)} if kubeconfig else {}
    # `kubectl wait` returns immediately for resources that don't
    # exist yet; we list-then-wait per app label so the timeout
    # actually applies to readiness, not to existence.
    for app in ("web", "api", "db", "cache", "queue-worker"):
        run(["kubectl", "-n", namespace, "wait", "--for=condition=Ready",
             "pod", "-l", f"app={app}", f"--timeout={timeout}s"],
            check=False, env=env)
    # CronJob doesn't get a Ready pod until it fires — we don't block on it.


def phase_workload_verify(namespace: str, outdir: Path,
                          label: str = "") -> bool:
    """
    Apply the L1+L2+L3 verify Job into `namespace`, wait for it to
    complete (or fail), collect logs into a per-namespace Markdown
    report. Returns True if all checks passed.
    """
    log(f"=== PHASE: workload-verify {namespace} "
        f"{('(' + label + ')') if label else ''} ===")

    tmpl = WORKLOADS_DIR / "verify" / "verify-job.yaml.tmpl"
    rendered = tmpl.read_text().replace("__NS__", namespace)

    # Idempotent: nuke a prior Job so apply can recreate it cleanly.
    run(["kubectl", "-n", namespace, "delete", "job",
         "workload-verify", "--ignore-not-found"],
        check=False)
    _kubectl_apply_stdin(rendered)

    # Wait up to 10 minutes for completion (Job has its own
    # `ttlSecondsAfterFinished: 600` so it disappears later).
    cp = run(["kubectl", "-n", namespace, "wait",
              "--for=condition=complete", "job/workload-verify",
              "--timeout=600s"], check=False, capture=True)
    succeeded = (cp.returncode == 0)

    # Collect logs regardless of outcome.
    pods_cp = run(["kubectl", "-n", namespace, "get", "pods",
                   "-l", "app=workload-verify", "-o",
                   "jsonpath={.items[0].metadata.name}"],
                  check=False, capture=True)
    pod = pods_cp.stdout.strip()
    job_logs = ""
    if pod:
        job_logs = run(["kubectl", "-n", namespace, "logs", pod],
                       check=False, capture=True).stdout

    suffix = f"-{label}" if label else ""
    out = outdir / f"workload-verify-{namespace}{suffix}.md"
    out.write_text(
        f"# Workload Verify — {namespace}"
        + (f" ({label})" if label else "")
        + f"\n\nStatus: **{'PASS' if succeeded else 'FAIL'}**\n\n"
        + f"## Job output\n\n```\n{job_logs.strip()}\n```\n"
    )
    log(f"wrote {out}  [{'PASS' if succeeded else 'FAIL'}]")
    return succeeded


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def write_scores(outdir: Path, results: dict[str, ScanResult]) -> None:
    """Persist a small machine-readable summary so validate can diff later."""
    scores = {
        name: {
            "score": r.score,
            "pass": r.pass_count,
            "fail": r.fail_count,
            "warn": r.warn_count,
        }
        for name, r in results.items()
    }
    (outdir / "scores.json").write_text(json.dumps(scores, indent=2))


def read_scores(report_dir: Path) -> dict[str, dict]:
    f = report_dir / "scores.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except json.JSONDecodeError:
        return {}


def write_report(path: Path, title: str, results: dict[str, ScanResult]) -> None:
    lines = [f"# {title}", "",
             f"Generated: {datetime.now(timezone.utc).isoformat()}", "",
             "## Summary", "",
             "| Tool | Pass | Fail | Warn | Score |",
             "|------|------|------|------|-------|"]
    for name, r in results.items():
        lines.append(
            f"| {name} | {r.pass_count} | {r.fail_count} | {r.warn_count} | {r.score}% |"
        )
    for name, r in results.items():
        if not r.failures:
            continue
        lines += ["", f"## {name} - Failed Controls ({len(r.failures)})", ""]
        for f in r.failures[:200]:  # cap for sanity
            lines.append(f"### {f['id']} - {f['desc']}")
            if f.get("remediation"):
                lines += ["", "**Remediation:**", "", "```",
                          str(f["remediation"]), "```", ""]
    path.write_text("\n".join(lines))
    log(f"wrote {path}")


def write_diff(baseline_dir: Path, post_dir: Path,
               post_results: dict[str, ScanResult]) -> None:
    baseline_report = baseline_dir / "baseline.md"
    post_report = post_dir / "post-hardening.md"
    out = post_dir / "delta.md"
    baseline_scores = read_scores(baseline_dir)
    if not baseline_scores:
        log(f"no scores.json in {baseline_dir}; delta will show baseline as N/A",
            "WARN")
    lines = ["# Hardening Delta", "",
             f"Baseline: {baseline_report}",
             f"Post:     {post_report}", "",
             "| Tool | Score Before | Score After | Delta |",
             "|------|--------------|-------------|-------|"]
    for name, r_post in post_results.items():
        b = baseline_scores.get(name)
        if b is not None:
            before = f"{b['score']}%"
            delta = round(r_post.score - b["score"], 1)
            delta_str = f"{'+' if delta >= 0 else ''}{delta} pts"
        else:
            before = "N/A"
            delta_str = "-"
        lines.append(
            f"| {name} | {before} | {r_post.score}% | {delta_str} |")
    out.write_text("\n".join(lines))
    log(f"wrote {out}")


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="k8s CIS hardening orchestrator")
    p.add_argument("phase", choices=["assess", "baseline", "tier1",
                                     "tier2", "validate",
                                     "create-tenant", "workload-deploy",
                                     "workload-verify", "all"],
                   help=(
                       "assess = posture report only, no changes. "
                       "baseline = same scan but framed as the snapshot "
                       "before tier1/tier2. "
                       "create-tenant = ns + SA + RBAC + kubeconfig. "
                       "workload-deploy = deploy admin or tenant workloads. "
                       "workload-verify = run L1+L2+L3 Job in a namespace."))
    p.add_argument("--inventory", default=str(TIER2_DIR / "inventory" / "hosts.ini"),
                   help="Ansible inventory for tier2")
    p.add_argument("--baseline-dir", help="for validate: path to baseline report dir")
    p.add_argument("--skip-tier2", action="store_true",
                   help="for 'all': skip node-level fixes")
    p.add_argument("--tenant", help="tenant name (for create-tenant / "
                                    "tenant workload-deploy)")
    p.add_argument("--kind", choices=["admin", "tenant"],
                   help="workload kind (for workload-deploy)")
    p.add_argument("--version", help="workload version: v1, v2, or harness "
                                     "(for workload-deploy)")
    p.add_argument("--namespace", help="target namespace (for workload-verify)")
    p.add_argument("--kubeconfig", help="kubeconfig file path (for tenant "
                                        "workload-deploy)")
    args = p.parse_args()

    require(["kubectl"])
    if args.phase in ("assess", "baseline", "validate", "all"):
        require(["kubescape"])
    if args.phase in ("tier2", "all") and not args.skip_tier2:
        require(["ansible-playbook"])

    REPORT_DIR.mkdir(exist_ok=True)

    if args.phase == "assess":
        phase_assess(timestamped_dir(REPORT_DIR, "assess"))
    elif args.phase == "baseline":
        phase_baseline(timestamped_dir(REPORT_DIR, "baseline"))
    elif args.phase == "tier1":
        phase_tier1()
    elif args.phase == "tier2":
        phase_tier2(args.inventory)
    elif args.phase == "validate":
        if not args.baseline_dir:
            log("--baseline-dir required for validate", "FATAL")
            return 2
        phase_validate(Path(args.baseline_dir),
                       timestamped_dir(REPORT_DIR, "post"))

    elif args.phase == "create-tenant":
        if not args.tenant:
            log("--tenant required", "FATAL")
            return 2
        create_tenant(args.tenant)

    elif args.phase == "workload-deploy":
        if not args.kind or not args.version:
            log("--kind and --version required", "FATAL")
            return 2
        kc = Path(args.kubeconfig) if args.kubeconfig else None
        phase_workload_deploy(args.kind, args.version,
                              tenant=args.tenant, kubeconfig=kc)

    elif args.phase == "workload-verify":
        if not args.namespace:
            log("--namespace required", "FATAL")
            return 2
        ok = phase_workload_verify(args.namespace,
                                   timestamped_dir(REPORT_DIR, "workload"))
        return 0 if ok else 1

    elif args.phase == "all":
        # Full 15-step pipeline from issue #1. Workload validation is
        # mandatory; tearing it apart with flags would defeat the
        # "prove hardening doesn't break real apps" promise.
        b = timestamped_dir(REPORT_DIR, "baseline")
        w = timestamped_dir(REPORT_DIR, "workload")

        # 1. Admin v1 (cert-manager, ClusterIssuer, metrics-server,
        #    node-debug DS).
        phase_workload_deploy("admin", "v1")

        # 2. Tenant-a: create the boundary, deploy 8 workloads,
        #    wait Ready.
        ka = create_tenant("tenant-a")
        phase_workload_deploy("tenant", "v1", tenant="tenant-a",
                              kubeconfig=ka)
        wait_for_tenant_workloads("tenant-a", kubeconfig=ka)

        # 3. Pre-hardening verify — if this already fails, hardening
        #    isn't the cause and we should stop.
        if not phase_workload_verify("tenant-a", w, label="pre-hardening"):
            log("pre-hardening verify failed; aborting before harden",
                "FATAL")
            return 2

        # 4. Baseline scan.
        phase_baseline(b)

        # 5. Tier 1.
        phase_tier1()

        # 6. Tier 2 (unless --skip-tier2 on managed K8s).
        if not args.skip_tier2:
            phase_tier2(args.inventory)

        # 7. Wait for the control plane + Kyverno after Tier 2.
        wait_for_apiserver()
        wait_for_kyverno()

        # 8. CHECKPOINT 1: tenant-a workloads still healthy.
        cp1 = phase_workload_verify("tenant-a", w,
                                    label="post-hardening-cp1")

        # 9. Admin v2 (logging ns + PolicyException + promtail DS).
        phase_workload_deploy("admin", "v2")

        # 10. Tenant-b: create boundary, deploy same 8 workloads under
        #     the now-active Kyverno policies.
        kb = create_tenant("tenant-b")
        phase_workload_deploy("tenant", "v1", tenant="tenant-b",
                              kubeconfig=kb)
        wait_for_tenant_workloads("tenant-b", kubeconfig=kb)

        # 11. Tenant-a harness: add background-job to existing tenant.
        phase_workload_deploy("tenant", "harness", tenant="tenant-a",
                              kubeconfig=ka)

        # 12. CHECKPOINT 2: tenant-a (with harness), tenant-b.
        cp2_a = phase_workload_verify("tenant-a", w,
                                      label="post-hardening-cp2")
        cp2_b = phase_workload_verify("tenant-b", w,
                                      label="post-hardening-cp2")

        # 13. CIS validate scan.
        phase_validate(b, timestamped_dir(REPORT_DIR, "post"))

        # Final per-checkpoint summary log line so the orchestrator's
        # final exit status reflects workload-validation health.
        log(f"CHECKPOINT 1 (existing tenant survives hardening): "
            f"{'PASS' if cp1 else 'FAIL'}")
        log(f"CHECKPOINT 2 (new tenant + harness post-hardening): "
            f"a={'PASS' if cp2_a else 'FAIL'} "
            f"b={'PASS' if cp2_b else 'FAIL'}")
        if not (cp1 and cp2_a and cp2_b):
            log("one or more workload checkpoints FAILED — read "
                "reports/workload_*/ for per-namespace logs", "ERROR")
            return 1

    log("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
