#!/usr/bin/env python3
"""
Patch /etc/kubernetes/manifests/kube-apiserver.yaml to apply CIS 1.2.x flags.

Idempotent: only writes if changes are required.
"""
import re
import sys
from pathlib import Path

MANIFEST = Path("/etc/kubernetes/manifests/kube-apiserver.yaml")

# (flag, value) - value=None means "ensure flag is present without value" (rare)
CIS_FLAGS = {
    "--anonymous-auth":                  "false",
    "--profiling":                       "false",
    "--audit-log-path":                  "/var/log/kubernetes/audit.log",
    "--audit-log-maxage":                "30",
    "--audit-log-maxbackup":             "10",
    "--audit-log-maxsize":               "100",
    "--audit-policy-file":               "/etc/kubernetes/audit-policy.yaml",
    "--request-timeout":                 "60s",
    "--service-account-lookup":          "true",
    "--enable-admission-plugins":        "NodeRestriction,EventRateLimit,AlwaysPullImages",
    "--tls-cipher-suites":               (
        "TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256,"
        "TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384,"
        "TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305,"
        "TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256,"
        "TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384,"
        "TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305"
    ),
}

# Flags that must NOT appear (insecure defaults)
FORBIDDEN = [
    "--insecure-bind-address",
    "--insecure-port",
    "--token-auth-file",
]


def main() -> int:
    if not MANIFEST.exists():
        print(f"{MANIFEST} not found - is this a control plane node?",
              file=sys.stderr)
        return 1

    text = MANIFEST.read_text()
    original = text

    # The manifest has a 'command:' list with one '- kube-apiserver' entry
    # followed by '- --flag=value' lines. We operate on those.
    lines = text.splitlines()
    out = []
    flag_lines_idx = []
    in_command = False
    command_indent = ""

    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("- kube-apiserver"):
            in_command = True
            command_indent = line[:len(line) - len(stripped)]
            out.append(line)
            continue
        if in_command and stripped.startswith("- --"):
            flag_lines_idx.append(len(out))
            out.append(line)
            continue
        if in_command and stripped and not stripped.startswith("-"):
            in_command = False
        out.append(line)

    # Index existing flags by name
    existing = {}
    for idx in flag_lines_idx:
        m = re.match(r"\s*-\s*(--[\w-]+)(?:=(.*))?", out[idx])
        if m:
            existing[m.group(1)] = idx

    changed = False

    # Remove forbidden flags
    indices_to_delete = []
    for f in FORBIDDEN:
        if f in existing:
            indices_to_delete.append(existing[f])
            print(f"removing forbidden flag: {f}")
            changed = True
    # delete from end to keep indices valid
    for idx in sorted(indices_to_delete, reverse=True):
        out.pop(idx)
        # rebuild index map for subsequent operations
        flag_lines_idx = []
        for i, ln in enumerate(out):
            if re.match(r"\s*-\s*--", ln):
                flag_lines_idx.append(i)
        existing = {}
        for i in flag_lines_idx:
            m = re.match(r"\s*-\s*(--[\w-]+)(?:=(.*))?", out[i])
            if m:
                existing[m.group(1)] = i

    # Apply CIS flags
    insert_at = max(flag_lines_idx) + 1 if flag_lines_idx else None
    indent = "    "  # standard kubeadm indent for command args

    for flag, value in CIS_FLAGS.items():
        desired = f"{indent}- {flag}={value}"
        if flag in existing:
            current = out[existing[flag]].rstrip()
            if current != desired:
                print(f"updating: {flag}")
                out[existing[flag]] = desired
                changed = True
        else:
            print(f"adding:   {flag}={value}")
            if insert_at is None:
                print("could not locate command list in manifest", file=sys.stderr)
                return 2
            out.insert(insert_at, desired)
            insert_at += 1
            changed = True

    if changed:
        new_text = "\n".join(out) + ("\n" if original.endswith("\n") else "")
        # write atomically
        tmp = MANIFEST.with_suffix(".yaml.tmp")
        tmp.write_text(new_text)
        tmp.chmod(0o600)
        tmp.rename(MANIFEST)
        print("manifest updated")
    else:
        print("no changes needed")

    return 0


if __name__ == "__main__":
    sys.exit(main())
