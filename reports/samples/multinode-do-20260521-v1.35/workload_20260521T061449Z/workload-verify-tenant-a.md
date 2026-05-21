# Workload Verify — tenant-a

Status: **PASS**

## Job output

```
(8/9) Installing libstdc++ (15.2.0-r2)
(9/9) Installing redis (8.4.2-r0)
OK: 14.0 MiB in 9 packages
===== L2: Service-level reachability =====
OK:   web Service serves HTTP 200
OK:   api Service serves HTTP 200
OK:   cache Service responds to PING
===== L3: End-to-end functional =====
OK:   web -> api round-trip succeeds (nginx proxy_pass)
OK:   db write+read succeeded
OK:   queue-worker has pushed 42 entries to redis
OK:   cron-pinger last success: 2026-05-21T06:14:27Z
===== Summary =====
ALL CHECKS PASSED for namespace tenant-a
```
