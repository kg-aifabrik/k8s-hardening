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
REPORT_DIR = ROOT / "reports"
KYVERNO_VERSION = "v1.12.5"  # pinned; bump deliberately
KYVERNO_INSTALL_URL = (
    f"https://github.com/kyverno/kyverno/releases/download/{KYVERNO_VERSION}/install.yaml"
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
        deadline = time.time() + 300
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

def phase_baseline(outdir: Path) -> dict[str, ScanResult]:
    log("=== PHASE: baseline ===")
    results = {
        "kube-bench": run_kube_bench(outdir),
        "kubescape":  run_kubescape(outdir),
    }
    write_report(outdir / "baseline.md", "Baseline CIS Scan", results)
    write_scores(outdir, results)
    return results


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


def phase_validate(baseline_dir: Path, outdir: Path) -> None:
    log("=== PHASE: validate ===")
    results = {
        "kube-bench": run_kube_bench(outdir),
        "kubescape":  run_kubescape(outdir),
    }
    write_report(outdir / "post-hardening.md",
                 "Post-Hardening CIS Scan", results)
    write_scores(outdir, results)
    write_diff(baseline_dir, outdir, results)


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
    p.add_argument("phase", choices=["baseline", "tier1", "tier2",
                                     "validate", "all"])
    p.add_argument("--inventory", default=str(TIER2_DIR / "inventory" / "hosts.ini"),
                   help="Ansible inventory for tier2")
    p.add_argument("--baseline-dir", help="for validate: path to baseline report dir")
    p.add_argument("--skip-tier2", action="store_true",
                   help="for 'all': skip node-level fixes")
    args = p.parse_args()

    require(["kubectl"])
    if args.phase in ("baseline", "validate", "all"):
        require(["kubescape"])
    if args.phase in ("tier2", "all") and not args.skip_tier2:
        require(["ansible-playbook"])

    REPORT_DIR.mkdir(exist_ok=True)

    if args.phase == "baseline":
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
    elif args.phase == "all":
        b = timestamped_dir(REPORT_DIR, "baseline")
        phase_baseline(b)
        phase_tier1()
        if not args.skip_tier2:
            phase_tier2(args.inventory)
        phase_validate(b, timestamped_dir(REPORT_DIR, "post"))

    log("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
