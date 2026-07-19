# AWS RDS Primary Unresponsive / Failover

**Severity:** SEV-1
**Owning Team:** DBA / Data Services
**Primary Escalation:** #data-services-oncall (PagerDuty schedule
"dba-primary") -- page immediately, do not wait to investigate alone

## Symptoms

Applications report a total inability to connect to the database, or every
query times out. The RDS console or CloudWatch shows the primary instance
as `Unavailable` or with a spike in `DatabaseConnections` errors, and
`FreeableMemory`/`CPUUtilization` may show anomalous flat-lining rather
than a gradual trend, which is characteristic of an instance-level failure
rather than a query-level problem.

## Likely Causes

1. Underlying EC2/host hardware failure on the primary instance.
2. A storage-layer issue (EBS volume degradation) affecting I/O.
3. An accidental or scheduled maintenance event caused an unplanned reboot.
4. Multi-AZ automatic failover has already been triggered by AWS and the
   application is still pointed at a stale connection/DNS cache.
5. Replication or Multi-AZ standby itself failed, leaving no healthy
   failover target.

## Diagnosis Steps

1. Check the RDS console for the instance's `Status` field and the
   `Recent events` tab -- AWS often logs the failure reason (host
   replacement, storage issue, failover initiated) before you have to
   dig further.
2. Check CloudWatch `DatabaseConnections`, `CPUUtilization`, and
   `FreeableMemory` for the instance over the last 30 minutes to
   distinguish a sudden hard failure from a gradual degradation.
3. If Multi-AZ is enabled, check whether AWS has already initiated an
   automatic failover -- the RDS endpoint DNS record may already point at
   the new primary, but application connection pools with long-lived
   cached DNS resolutions may still be trying the old IP.
4. Check `pg_stat_replication` (or the MySQL equivalent) from the
   application's last known-good connection, if available, to see replica
   lag immediately before the incident -- this helps confirm whether the
   standby was healthy and current at failover time.
5. Confirm whether this coincides with a scheduled maintenance window in
   the RDS console's `Maintenance` tab.

## Remediation Steps

1. **If Multi-AZ auto-failover has already occurred:** the fastest fix is
   almost always forcing application pods/instances to re-resolve DNS and
   re-establish connections -- restart application pods
   (`kubectl rollout restart deployment/<name>`) rather than waiting for
   connection pools to notice the old endpoint is dead on their own.
2. **If Multi-AZ is enabled but has not yet failed over:** manually
   trigger a failover from the RDS console ("Failover" action) or via
   `aws rds reboot-db-instance --db-instance-identifier <id>
   --force-failover`. Confirm the standby is healthy and in-sync first if
   time allows -- failing over to a lagging standby can lose recent writes.
3. **If Multi-AZ is not enabled (single-AZ instance):** this requires a
   restore from the most recent automated snapshot or point-in-time
   recovery, which has a real RTO measured in tens of minutes. Begin the
   restore immediately in parallel with continuing investigation, since
   there is no faster path for a genuinely failed single-AZ instance.
4. Once a healthy primary is confirmed (new endpoint responding, replica
   lag at zero on any remaining standby), restart or roll application pods
   to force fresh connections, and watch error rates return to baseline.
5. Do not delete or modify the old (failed) instance until the DBA team
   has had a chance to investigate root cause from its logs/metrics.

## Escalation Path

This runbook itself is the SEV-1 escalation -- do not attempt independent
manual failover on a production primary without the DBA on-call engaged,
except in the auto-failover-already-happened case in remediation step 1,
which is safe to act on immediately.

## Related Runbooks

- `database-connection-pool-exhausted.md` -- if the primary is reachable
  but connection-starved rather than fully unresponsive.
- `disk-space-critical.md` -- if a replica's disk filled up due to stalled
  WAL/binlog recycling, contributing to a failed standby.
