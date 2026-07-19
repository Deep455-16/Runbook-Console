# High CPU Utilization

**Severity:** SEV-2 if user-facing latency is affected, SEV-3 otherwise
**Owning Team:** Service-owning team, with Infrastructure / SRE support
**Primary Escalation:** #infra-oncall (PagerDuty schedule "sre-primary")

## Symptoms

CPU utilization alert fires above 85% sustained for 5+ minutes on a host,
container, or pod. User-facing symptoms typically include elevated p95/p99
latency, request timeouts, and in Kubernetes, pods being throttled by CFS
(visible as `container_cpu_cfs_throttled_seconds_total` climbing) even
though the pod hasn't crashed.

## Likely Causes

1. A genuine, expected traffic increase that has simply outpaced current
   replica count or instance sizing.
2. A regression in a recent deploy -- an accidentally-quadratic code path,
   a missing cache, an N+1 query pattern moved into a hot loop, or a change
   that disabled an optimization.
3. A single noisy-neighbor process or pod consuming disproportionate CPU on
   a shared host.
4. Garbage collection thrashing on a JVM/CLR-based service that is under
   memory pressure, which manifests as CPU spikes rather than OOM.
5. An infinite retry loop -- a downstream dependency degraded, and a client
   without backoff is hammering it (and burning CPU on both sides).

## Diagnosis Steps

1. Check the CPU dashboard (Grafana: `service / cpu`) to see whether the
   spike correlates with a traffic increase (check request-rate panel
   side by side) or is disproportionate to traffic.
2. Check the deploy timeline for the affected service over the last few
   hours -- CPU regressions are very commonly introduced by a specific
   commit, and correlating the spike's start time with a deploy timestamp
   is often the fastest diagnosis.
3. On a Linux host, run `top` or `htop` to identify which process is
   consuming CPU; for a more precise breakdown use `perf top` if available.
4. On Kubernetes, run `kubectl top pods -n <namespace>` to find the
   specific pod(s) responsible, then check
   `container_cpu_cfs_throttled_seconds_total` in Grafana to confirm
   whether throttling (not just usage) is impacting latency.
5. For JVM/CLR services, check GC logs or the runtime's memory dashboard --
   if CPU rises alongside frequent full GCs, the root cause is memory
   pressure, not a CPU-bound code path.
6. Check error rates and retry counts on calls to downstream dependencies;
   a retry storm from a degraded dependency is a common CPU root cause that
   is easy to miss if you only look at the service itself.

## Remediation Steps

1. **Immediate relief:** scale out horizontally (increase replica count)
   if the service supports it and the cause is traffic-proportional --
   this buys time without needing to identify root cause first.
2. **If caused by a recent deploy:** roll back the deploy. Do not attempt
   to patch a CPU regression live under incident pressure.
3. **If caused by a noisy neighbor on shared infrastructure:** cordon and
   drain the affected node, or move the offending workload to isolated
   capacity, then investigate the noisy process separately once the
   incident is mitigated.
4. **If caused by GC thrashing under memory pressure:** increase the
   memory limit/request for the affected pods as an immediate mitigation,
   and file a follow-up to profile actual memory usage growth.
5. **If caused by a retry storm:** fix or disable the runaway retry loop
   at the client, and confirm the downstream dependency it was hammering
   has also recovered.
6. After mitigation, watch CPU and latency dashboards for 15 minutes to
   confirm both have returned to baseline before resolving the incident.

## Escalation Path

If CPU remains elevated after horizontal scale-out and a deploy rollback,
and no noisy neighbor or retry storm is found, escalate to the service
owner's team lead -- this may require a profiling session that is beyond
what can be done live during an incident.

## Related Runbooks

- `database-connection-pool-exhausted.md` -- if elevated CPU on a database
  host correlates with active (not idle) query counts.
- `kubernetes-pod-crashloop.md` -- if CPU throttling is severe enough that
  liveness probes start failing and pods begin restarting.
