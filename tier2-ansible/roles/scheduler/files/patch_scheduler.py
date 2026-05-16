#!/usr/bin/env python3
"""Patch kube-scheduler manifest with CIS 1.4 flags."""
import re
import sys
from pathlib import Path

MANIFEST = Path("/etc/kubernetes/manifests/kube-scheduler.yaml")

CIS_FLAGS = {
    "--profiling":   "false",       # 1.4.1
    "--bind-address": "127.0.0.1",  # 1.4.2
}


def main() -> int:
    if not MANIFEST.exists():
        print(f"{MANIFEST} not found", file=sys.stderr)
        return 1
    lines = MANIFEST.read_text().splitlines()
    out, flag_idx, in_cmd = [], [], False
    for line in lines:
        s = line.lstrip()
        if s.startswith("- kube-scheduler"):
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
    indent, changed = "    ", False
    for flag, value in CIS_FLAGS.items():
        desired = f"{indent}- {flag}={value}"
        if flag in existing:
            if out[existing[flag]].rstrip() != desired:
                out[existing[flag]] = desired
                changed = True
        else:
            out.insert(insert_at, desired)
            insert_at += 1
            changed = True

    if changed:
        tmp = MANIFEST.with_suffix(".yaml.tmp")
        tmp.write_text("\n".join(out) + "\n")
        tmp.chmod(0o600)
        tmp.rename(MANIFEST)
    return 0


if __name__ == "__main__":
    sys.exit(main())
