#!/usr/bin/env python3
"""Patch kube-controller-manager manifest with CIS 1.3 flags."""
import re
import sys
from pathlib import Path

MANIFEST = Path("/etc/kubernetes/manifests/kube-controller-manager.yaml")

CIS_FLAGS = {
    "--profiling":                       "false",   # 1.3.2
    "--use-service-account-credentials": "true",    # 1.3.3
    "--service-account-private-key-file": "/etc/kubernetes/pki/sa.key",  # 1.3.4
    "--root-ca-file":                    "/etc/kubernetes/pki/ca.crt",   # 1.3.5
    "--bind-address":                    "127.0.0.1",  # 1.3.7
}


def main() -> int:
    if not MANIFEST.exists():
        print(f"{MANIFEST} not found", file=sys.stderr)
        return 1

    lines = MANIFEST.read_text().splitlines()
    out, flag_idx, in_cmd = [], [], False
    for line in lines:
        s = line.lstrip()
        if s.startswith("- kube-controller-manager"):
            in_cmd = True
            out.append(line)
            continue
        if in_cmd and s.startswith("- --"):
            flag_idx.append(len(out))
            out.append(line)
            continue
        if in_cmd and s and not s.startswith("-"):
            in_cmd = False
        out.append(line)

    existing = {}
    for i in flag_idx:
        m = re.match(r"\s*-\s*(--[\w-]+)(?:=(.*))?", out[i])
        if m:
            existing[m.group(1)] = i

    insert_at = max(flag_idx) + 1 if flag_idx else None
    indent = "    "
    changed = False
    for flag, value in CIS_FLAGS.items():
        desired = f"{indent}- {flag}={value}"
        if flag in existing:
            if out[existing[flag]].rstrip() != desired:
                out[existing[flag]] = desired
                changed = True
                print(f"updated: {flag}")
        else:
            if insert_at is None:
                print("no command list found", file=sys.stderr)
                return 2
            out.insert(insert_at, desired)
            insert_at += 1
            changed = True
            print(f"added: {flag}={value}")

    if changed:
        tmp = MANIFEST.with_suffix(".yaml.tmp")
        tmp.write_text("\n".join(out) + "\n")
        tmp.chmod(0o600)
        tmp.rename(MANIFEST)
        print("manifest updated")
    else:
        print("no changes needed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
