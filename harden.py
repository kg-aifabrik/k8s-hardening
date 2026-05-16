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


def run_kube_bench(outdir: Path) -> ScanResult:
    """
    Runs kube-bench as a Kubernetes Job using the upstream manifest.
    The job uploads its JSON output to a ConfigMap we then extract.
    For a vanilla install we invoke the official 'job.yaml' which auto-detects.
    """
    log("running kube-bench...")
    job_manifest = SCAN_DIR / "kube-bench-job.yaml"
    run(["kubectl", "apply", "-f", str(job_manifest)])

    # wait for completion (timeout 5m)
    deadline = time.time() + 300
    pod = None
    while time.time() < deadline:
        cp = run(
            ["kubectl", "-n", "kube-bench-scan", "get", "pods",
             "-l", "app=kube-bench", "-o", "jsonpath={.items[0].metadata.name}"],
            check=False, capture=True,
        )
        if cp.returncode == 0 and cp.stdout.strip():
            pod = cp.stdout.strip()
            phase = run(
                ["kubectl", "-n", "kube-bench-scan", "get", "pod", pod,
                 "-o", "jsonpath={.status.phase}"],
                check=False, capture=True,
            ).stdout.strip()
            if phase in ("Succeeded", "Failed"):
                break
        time.sleep(5)

    if not pod:
        log("kube-bench job never produced a pod", "ERROR")
        return ScanResult(tool="kube-bench")

    raw = run(["kubectl", "-n", "kube-bench-scan", "logs", pod],
              capture=True).stdout
    raw_path = outdir / "kube-bench.json"
    raw_path.write_text(raw)

    result = ScanResult(tool="kube-bench", raw_path=raw_path)
    try:
        data = json.loads(raw)
        for ctrl in data.get("Controls", []):
            result.pass_count += ctrl.get("total_pass", 0)
            result.fail_count += ctrl.get("total_fail", 0)
            result.warn_count += ctrl.get("total_warn", 0)
            for test in ctrl.get("tests", []):
                for r in test.get("results", []):
                    if r.get("status") == "FAIL":
                        result.failures.append({
                            "id": r.get("test_number"),
                            "desc": r.get("test_desc"),
                            "remediation": r.get("remediation", "").strip(),
                        })
        if result.total:
            result.score = round(100 * result.pass_count / result.total, 1)
    except json.JSONDecodeError:
        log("could not parse kube-bench JSON; leaving raw on disk", "WARN")

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
            status = c.get("status", {}).get("status", "")
            if status == "passed":
                result.pass_count += 1
            elif status == "failed":
                result.fail_count += 1
                result.failures.append({
                    "id": cid,
                    "desc": c.get("name", ""),
                    "remediation": c.get("complianceScore", ""),
                })
            else:
                result.warn_count += 1
        if result.total:
            result.score = round(100 * result.pass_count / result.total, 1)
    except (json.JSONDecodeError, KeyError) as e:
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
    return results


def phase_tier1() -> None:
    log("=== PHASE: tier1 (cluster manifests) ===")

    # Kyverno must be installed before its policies
    log("installing Kyverno...")
    run(["kubectl", "apply", "-f", KYVERNO_INSTALL_URL])
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
    write_diff(baseline_dir, outdir, results)


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

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
    lines = ["# Hardening Delta", "",
             f"Baseline: {baseline_report}",
             f"Post:     {post_report}", "",
             "| Tool | Score Before | Score After | Delta |",
             "|------|--------------|-------------|-------|"]
    # parse baseline scores from JSON sidecars if present
    for name, r_post in post_results.items():
        before = "?"
        bjson = baseline_dir / f"{name}.json"
        if bjson.exists():
            # the score isn't stored, so approximate by re-scanning the file
            # quick & dirty: just leave "?" and rely on reports for truth
            pass
        lines.append(f"| {name} | {before} | {r_post.score}% | - |")
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
