# Database Connection Pool Exhausted

**Severity:** SEV-2 (degraded service, rising error rate)
**Owning Team:** Platform / Data Services
**Primary Escalation:** #platform-oncall (PagerDuty schedule "data-services-primary")
**Secondary Escalation:** DBA on-call, escalate after 20 minutes without mitigation

## Symptoms

Application pods start returning HTTP 503s or timeouts on any endpoint that
touches the database. Logs show errors such as `connection pool exhausted`,
`could not obtain connection from pool within 30000ms`, or, on Postgres,
`FATAL: sorry, too many clients already`. Latency dashboards show p99 request
time climbing sharply while CPU on the app tier stays flat or low -- the
bottleneck is connection availability, not compute.

## Likely Causes

1. A slow or long-running query (missing index, lock contention, bad plan
   after a schema migration) is holding connections open far longer than
   normal, starving the rest of the pool.
2. A recent deploy shipped a code path that opens a connection but does not
   release it back to the pool on an exception (a connection leak).
3. Traffic spike exceeded the configured pool size for the current fleet
   size -- the pool was sized for N replicas and the fleet autoscaled past
   that assumption.
4. The database itself is near its own `max_connections` limit, so every
   application pool is fighting over a shrinking shared resource.

## Diagnosis Steps

1. Check the connection pool dashboard (Grafana: `db / connection-pool`)
   for the affected service. Compare "active connections" vs "pool max" --
   if active is pinned at max, the pool is the bottleneck.
2. On Postgres, run `SELECT state, count(*) FROM pg_stat_activity GROUP BY
   state;` to see how many connections are `idle in transaction` (a strong
   signal of leaked or stuck connections holding transactions open).
3. Identify long-running queries with `SELECT pid, now() - query_start AS
   duration, query FROM pg_stat_activity ORDER BY duration DESC LIMIT 10;`.
   Anything running more than a few minutes on an OLTP path is suspicious.
4. Check recent deploys in the last 2 hours for the affected service --
   connection leaks are very frequently introduced by a code change, not
   infrastructure drift.
5. Confirm whether the fleet has autoscaled recently; compare current
   replica count against the value the connection pool size was tuned for
   (`pool_max_size * replica_count` should stay comfortably under the
   database's `max_connections`).

## Remediation Steps

1. **Immediate relief:** if a small number of queries are clearly stuck or
   `idle in transaction` for an abnormal duration, terminate them with
   `SELECT pg_terminate_backend(pid);` for the specific PIDs identified in
   diagnosis step 3. Do not mass-kill connections without confirming which
   ones are the actual problem.
2. **If caused by a bad deploy:** roll back the offending deploy
   immediately rather than attempting a hotfix under pressure. Connection
   leaks are rarely safe to patch live.
3. **If caused by autoscaling outgrowing pool sizing:** temporarily lower
   `pool_max_size` per replica so that `pool_max_size * replica_count` is
   back under 80% of the database's `max_connections`, then redeploy the
   config. File a follow-up ticket to make this ratio dynamic.
4. **If the database itself is saturated:** consider enabling or scaling a
   connection pooler (e.g. PgBouncer in transaction pooling mode) in front
   of the database so application pools multiplex onto a much smaller
   number of real backend connections.
5. Once mitigated, watch the connection pool dashboard for 15 minutes to
   confirm active connections stabilize well below the pool max before
   closing the incident.

## Escalation Path

If active connections remain pinned at max for more than 20 minutes after
step 1-3 above, or if `pg_terminate_backend` calls are not freeing capacity,
page the DBA on-call directly -- this may indicate replication lag, a lock
held by a long-running maintenance job, or a hardware-level issue on the
primary.

## Related Runbooks

- `aws-rds-failover.md` -- if the primary database instance itself becomes
  unresponsive rather than just connection-starved.
- `high-cpu-utilization.md` -- if `pg_stat_activity` shows many active
  (not idle) queries and CPU on the DB host is also elevated.
