#!/usr/bin/env python3
"""
Ensure kube-apiserver has --encryption-provider-config flag and the
encryption-config volume mount.
"""
import re
import sys
from pathlib import Path

MANIFEST = Path("/etc/kubernetes/manifests/kube-apiserver.yaml")
FLAG = "--encryption-provider-config"
FLAG_VALUE = "/etc/kubernetes/encryption-config.yaml"

VOLUME_BLOCK = """\
    - name: encryption-config
      hostPath:
        path: /etc/kubernetes/encryption-config.yaml
        type: File"""

VOLUME_MOUNT_BLOCK = """\
    - name: encryption-config
      mountPath: /etc/kubernetes/encryption-config.yaml
      readOnly: true"""


def main() -> int:
    if not MANIFEST.exists():
        print(f"{MANIFEST} not found", file=sys.stderr)
        return 1
    text = MANIFEST.read_text()

    changed = False

    # Add the flag if missing
    if FLAG not in text:
        # find the last - --flag line and insert after it
        lines = text.splitlines()
        last_flag_idx = -1
        for i, ln in enumerate(lines):
            if re.match(r"\s*-\s*--[\w-]+", ln):
                last_flag_idx = i
        if last_flag_idx == -1:
            print("could not locate flag list", file=sys.stderr)
            return 2
        lines.insert(last_flag_idx + 1, f"    - {FLAG}={FLAG_VALUE}")
        text = "\n".join(lines) + "\n"
        changed = True
        print(f"added flag: {FLAG}")

    # Add the volume mount if missing
    if "name: encryption-config" not in text:
        # naive insertion before 'volumes:' top-level key
        text = text.replace(
            "  volumes:\n",
            f"  volumes:\n{VOLUME_BLOCK}\n",
            1,
        )
        # and the volumeMount inside the container
        text = text.replace(
            "    volumeMounts:\n",
            f"    volumeMounts:\n{VOLUME_MOUNT_BLOCK}\n",
            1,
        )
        changed = True
        print("added encryption-config volume + mount")

    if changed:
        tmp = MANIFEST.with_suffix(".yaml.tmp")
        tmp.write_text(text)
        tmp.chmod(0o600)
        tmp.rename(MANIFEST)
        print("manifest updated")
    else:
        print("no changes needed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
