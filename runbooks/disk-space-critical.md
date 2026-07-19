# Disk Space Critical on Production Node

**Severity:** SEV-1 if root/data volume, SEV-3 if a non-critical mount
**Owning Team:** Infrastructure / SRE
**Primary Escalation:** #infra-oncall (PagerDuty schedule "sre-primary")

## Symptoms

Alert fires from disk-usage monitoring at 90% (warning) or 95% (critical)
on a host or mounted volume. Downstream symptoms can include: write
failures (`No space left on device`), a database refusing new writes or
crashing outright, log shippers falling behind or crash-looping, and on
Kubernetes nodes, the kubelet evicting pods with `DiskPressure` taints once
the node crosses its configured eviction threshold.

## Likely Causes

1. Uncapped or unrotated application logs filling the log partition.
2. A runaway process writing temp files that are never cleaned up (core
   dumps, cache directories, unbounded upload buffers).
3. Database WAL / binlog accumulation because replication or a backup job
   is stalled and the DB cannot recycle old log segments.
4. Container image sprawl -- old, unused image layers accumulating on a
   node's container runtime storage.
5. A genuine capacity shortfall: sustained data growth has simply outpaced
   the provisioned volume size.

## Diagnosis Steps

1. SSH to the affected host and run `df -h` to confirm which mount is
   actually full -- do not assume it is the root volume.
2. Run `du -sh /* 2>/dev/null | sort -rh | head -15` (adjust path to the
   full mount) to find the largest top-level consumers.
3. If logs are suspected: check `/var/log` and any application-specific
   log directories with `du -sh`, and check whether logrotate is actually
   running (`cat /etc/logrotate.d/<service>` and check its last run).
4. If it's a database host: check WAL/binlog directory size and compare
   against replication lag -- a stalled replica or backup job will prevent
   old segments from being recycled even though the primary is healthy.
5. On Kubernetes nodes: run `docker system df` or `crictl imagefs info`
   (depending on runtime) to see how much space is consumed by unused
   image layers versus running containers.
6. Check historical disk usage trend on the Grafana `node / disk` dashboard
   over the last 30 days to distinguish a sudden spike from steady organic
   growth.

## Remediation Steps

1. **Immediate relief (buys time, not a fix):**
   - Clear rotated/compressed logs older than the retention policy:
     `find /var/log -name "*.gz" -mtime +7 -delete` (confirm retention
     policy before running against production).
   - Prune unused container images: `docker image prune -af` or the
     `crictl`/`ctr` equivalent for the node's runtime.
   - Clear known-safe temp/cache directories for the affected application
     per its own runbook -- never delete files you cannot positively
     identify as safe to remove.
2. **If caused by unrotated logs:** fix or reinstall the logrotate config
   for the affected service and confirm it runs successfully with
   `logrotate -f <config>` before closing the incident.
3. **If caused by stalled WAL/binlog recycling:** resolve the underlying
   replication or backup stall first (see `aws-rds-failover.md` if the
   replica itself is down) -- deleting WAL/binlog files manually can break
   replication or point-in-time recovery and should only be done under DBA
   guidance.
4. **If it is a genuine capacity shortfall:** expand the volume (cloud
   block storage resize, or provision a larger disk) rather than
   repeatedly firefighting cleanup. File a capacity-planning follow-up.
5. Re-check `df -h` after remediation and confirm usage has dropped below
   the warning threshold (typically 80%) before resolving the alert.

## Escalation Path

If the volume is the root filesystem of a stateful/database node and usage
does not drop after clearing safe-to-delete files, escalate to the DBA
on-call immediately -- a full root volume on a database primary can cause
an unclean crash.

## Related Runbooks

- `aws-rds-failover.md` -- when a database replica is down and unable to
  recycle WAL/binlogs, contributing to disk growth.
- `kubernetes-pod-crashloop.md` -- when `DiskPressure` evictions are the
  symptom bringing you here.
