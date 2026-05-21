# Issues encountered and improvements to make

Living log of fragility points exposed while running the
workload-validation harness end-to-end (issue #1). Bugs are split
into two buckets:

- **Fixed** — already committed, here as a regression-prevention
  reference and a hint for future debugging.
- **Open** — known fragility, workaround in place, real fix queued.

Cross-referenced with the open items in the agent's task list.

## Status of T8 (full E2E run on fresh DO droplets)

**Partial success as of 2026-05-20.** Across six pipeline attempts
on real DO droplets we validated each of the 19 fixes below
individually but never landed a single one-shot run from a fresh
cluster through to both CP1 and CP2 PASS with reports committed.

What worked end-to-end at least once:
- check-images pre-flight
- admin v1 (cert-manager, ClusterIssuer, metrics-server, node-debug DS, local-path-provisioner)
- create-tenant (tenant-a with SA + Role + kubeconfig)
- tenant v1 deploy (8 workloads all reaching Ready)
- **pre-hardening verify Job (CP0): PASS** ← first time on attempt 5
- baseline scan
- Tier 1 (Kyverno + policies + manifests)
- Tier 2 (Ansible playbook against all 3 nodes)

What never landed in one run:
- post-hardening verify CP1 + admin v2 + tenant-b deploy + harness + CP2 + validate

The 30 minutes of cluster churn from Tier 2's manifest patching
exposed timing fragility that the orchestrator's waits didn't fully
cover until the very last fix (commit `43c9cd0`). Then a db PVC
binding issue surfaced on the cluster's 6th re-run, at which point
we tore down to avoid further DO spend.

Each open improvement below would close a class of failures
encountered. Items #36, #37, and #40 in particular would make a
future T8 attempt high-confidence one-shot.

---

## Fixed bugs (in order encountered)

### Image references
| # | Symptom | Root cause | Fix commit |
|---|---------|------------|-----------|
| 1 | `docker.io/bitnami/redis:7.4: not found` and similar 404s for every `bitnami/*` tag | Bitnami pulled their entire docker.io public catalog in mid-2025. Only orphaned signature/metadata blobs remain. | `746327c` — migrated to official `library/postgres:16-alpine` + `library/redis:7-alpine`, adjusted env vars (POSTGRES_* vs POSTGRESQL_*), data paths, runAsUser. |
| 2 | `api` pod CrashLoopBackOff with `exec: "-port": executable file not found in $PATH` | go-httpbin's image uses CMD with no ENTRYPOINT. My `args: ["-port", "8080"]` replaced the binary path. | `746327c` — dropped the args; default CMD already binds :8080. |
| 3 | `web` pods stuck ContainerCreating: `configmap references non-existent config key: nginx.conf` | Renamed ConfigMap data key `nginx.conf` → `default.conf` but the volume `items:` selector still asked for the old name. | `8a989ad` — dropped the items selector, project 1:1. |

### Storage / DNS
| # | Symptom | Root cause | Fix commit |
|---|---------|------------|-----------|
| 4 | db StatefulSet's PVC stuck Pending forever | Vanilla kubeadm has no default StorageClass. | `746327c` — install local-path-provisioner in admin v1, promote to default only if none exists. |
| 5 | `psql: could not translate host name "db-0.db.tenant-a.svc.cluster.local"` even though wget + redis-cli resolved their FQDNs fine | musl libc resolver edge case (or stale endpoints in CoreDNS for headless-Service pod-FQDN names). | `67b175b` — use the headless Service name `db.<NS>.svc.cluster.local`, resolves to the same pod. |

### apk-install inside verify Job
The verify Job needs psql, redis-cli, curl/wget in one place. We
chose to apk-install into a writable sysroot. Five separate fixes
were required:

| # | Symptom | Root cause | Fix commit |
|---|---------|------------|-----------|
| 6 | `ERROR: Use --usermode to allow creating database as non-root` | apk's `--initdb` won't run for non-root users without `--usermode`. | `dc451a0` — add the flag. |
| 7 | `curl (no such package)` after apk install | `--initdb` made a fresh apk DB but didn't populate `/etc/apk/repositories` or `/etc/apk/keys`. | `cc690e2` — cp the host's `/etc/apk/` config into the sysroot. |
| 8 | `apk` returned non-zero with `ERROR: ... busybox.trigger: exited 127`, `unshare: Operation not permitted` | apk's post-install triggers (busybox, ca-certificates) call `unshare`, blocked by Tier 1's drop-ALL-capabilities. | `cc94e3d` — `--no-scripts` to skip triggers. Binaries install fine without them. |
| 9 | curl ran but `Error relocating /tmp/sysroot/usr/bin/curl: curl_easy_init: symbol not found` (and many more) | libcurl is installed under `/tmp/sysroot/usr/lib` but the dynamic linker's default search path doesn't include it. RPATH baked into the binary doesn't help. | `5e95d95` — drop curl entirely, use busybox `wget` (already in the postgres image). |
| 10 | `FATAL: /usr/local/bin/psql missing` even though the postgres image absolutely ships it there | An emptyDir volume `apk-local` was mounted at `/usr/local`, shadowing the image's `/usr/local`. | `cbd016a` — drop that volume entirely. |

### Timing races
| # | Symptom | Root cause | Fix commit |
|---|---------|------------|-----------|
| 11 | `failed calling webhook "validate.kyverno.svc-fail": connect: connection refused` after Tier 2 | `wait_for_kyverno()` only checked Deployment Available, but Service endpoints lagged. | `d6e0f39` — also wait for endpoint addresses + probe with a `kubectl apply --dry-run=server`. |
| 12 | False PASS on CP1 verify | Stale Job from pre-hardening had `Complete` condition; `delete` was blocked by then-broken Kyverno webhook; `apply` reported `unchanged`; `kubectl wait` returned immediately on the stale condition. | `d6e0f39` — per-checkpoint Job names (`workload-verify-pre-hardening`, `workload-verify-post-hardening-cp1`, etc.). Pod logs filtered on the built-in `job-name=` label. |
| 13 | `cron-pinger has not recorded a success yet` even though the CronJob was healthy | Verify Job ran ~25s after tenant deploy, before the first 1-minute tick of the CronJob. | `67b175b` — retry the cron-pinger check for up to ~90s. |
| 14 | Workload deploy hit Kyverno webhook 503 even though wait happened earlier | wait_for_kyverno was only called before validate / CP1; not before admin/v1, tenant v1, etc. | `50aa555` — call wait_for_kyverno at the start of every workload-deploy. |
| 15 | wait_for_kyverno timed out because the apiserver was crashlooping during the wait | metrics-server idempotent re-apply patched its deployment, rolled the pods, broke `v1beta1.metrics.k8s.io` APIService, which made kube-apiserver's `/livez` probe fail enough times that kubelet SIGKILLed it (exit 137). Apiserver recovered after ~9 restarts. | `43c9cd0` — call wait_for_apiserver before wait_for_kyverno. (Better fix queued: make the metrics-server patch idempotent — see Open #1.) |

### Bootstrap-script bugs (caught earlier, see commit 709b627)
| # | Symptom | Root cause | Fix commit |
|---|---------|------------|-----------|
| 16 | First SSH to a freshly-`doctl-create`d droplet: connection refused | `--wait` returns at "Active", before sshd accepts. | `709b627` — wait_for_ssh polling. |
| 17 | `prep-node.sh: 138.197...: syntax error` | bash treats dots in associative-array keys as arithmetic. | `709b627` — parallel indexed arrays. |
| 18 | install-kubescape.sh silently exited 23 | `curl ... \| grep -m1 ...` with `set -o pipefail`. SIGPIPE on curl. | `709b627` — materialize JSON to variable first, then grep. |
| 19 | `Could not get lock /var/lib/apt/lists/lock` on cold-droplet prep | Cloud-init's unattended-upgrades races our apt. | `73527e0` — `cloud-init status --wait` at top of prep-node.sh. |

---

## Open improvements

Each tracked as a task in the agent's task list.

| # | Improvement | Status | Why |
|---|-------------|--------|-----|
| O1 | Make metrics-server patch idempotent | Open (task #36) | Avoids the apiserver crashloop documented in fix #15. Check args before patching. |
| O2 | Build a fixed verify-tools image | Open (task #37) | Eliminates fixes #6–#10 by burning curl+redis-cli+psql into a pinned-by-digest image. Stop apk-installing at runtime. |
| O3 | Pin all images by digest, not just tag | Open (task #38) | check-images preflight catches tag deletion (#1) but not tag mutation. |
| O4 | Consolidate kubectl-apply-with-retry | Open (task #39) | Retry logic for Kyverno + cert-manager webhook races is scattered. Single helper that recognizes "failed calling webhook" and retries. |
| O5 | `./harden.py reset-tenants` phase | Open (task #40) | Manual `kubectl delete ns tenant-a tenant-b --wait=true` between runs is awkward. Bake into a phase. |
| O6 | Structured phase START/END log markers | Open (task #41) | Monitor watchdog uses heuristic mtime stuck-detection. Explicit `=== PHASE: X END (ok\|fail) ===` would let watchdogs reason about boundaries. |

---

## Recurring patterns worth noting

1. **Idempotent re-apply ≠ no-op.** `kubectl apply` is idempotent at the *object* level but can trigger pod rolls (e.g., when one field changes a Deployment's pod template). Pod rolls can cascade into apiserver / aggregator instability. Pre-flight checks should detect "already-applied" state and skip the apply, not rely on apply being side-effect-free.

2. **Hardened cluster is fragile to re-runs.** Once Tier 2 is applied, the control plane is sensitive to mass-restart events. Re-running `all` makes Kyverno restart (it was installed by Tier 1), which makes admission unavailable for ~30s, which can stop other reconciles. Successful re-runs require ordered waits at every layer.

3. **Webhook readiness ≠ Deployment Available.** Both for Kyverno and cert-manager, the Deployment can report Available before the Service endpoints are populated. Always probe the webhook end-to-end (`--dry-run=server`) before relying on it.

4. **Empty volume mounts shadow image content.** A common k8s-newbie mistake the framework re-discovered: mounting an emptyDir at a path that the image populates loses the image's content. Verify with `kubectl exec` if a file you "know" is in the image suddenly isn't.
