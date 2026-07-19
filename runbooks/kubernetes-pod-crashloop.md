# Kubernetes Pod CrashLoopBackOff

**Severity:** SEV-2 if it affects all replicas of a user-facing service,
SEV-3 if partial/single-replica
**Owning Team:** Service-owning team, with Platform support
**Primary Escalation:** #platform-oncall (PagerDuty schedule "platform-primary")

## Symptoms

`kubectl get pods` shows one or more pods in `CrashLoopBackOff` status,
with a rapidly increasing restart count. If it affects all replicas of a
deployment, the service is fully down; if partial, you'll typically see
uneven load and elevated error rates as remaining healthy pods absorb the
traffic.

## Likely Causes

1. Application-level startup failure -- a bad config value, a missing
   environment variable/secret, or an unhandled exception during init.
2. Failing readiness/liveness probe -- the app may actually be healthy but
   the probe is misconfigured (wrong path/port, too short a timeout) and
   Kubernetes is killing healthy pods.
3. Out-of-memory kill (`OOMKilled`) -- the container exceeds its memory
   limit and is terminated by the kernel's OOM killer.
4. A bad image tag or corrupted image was deployed.
5. Node-level `DiskPressure` or `MemoryPressure` causing the kubelet to
   evict and reschedule pods repeatedly.
6. A dependency the app requires at startup (database, config service) is
   unreachable, so the app crashes during its own health-check-on-boot.

## Diagnosis Steps

1. `kubectl describe pod <pod>` -- check the `Last State` and `Reason`
   field first. `OOMKilled`, `Error`, and probe failure messages all show
   up here and immediately narrow the cause.
2. `kubectl logs <pod> --previous` -- the `--previous` flag is essential;
   it shows logs from the last crashed instance, not the fresh restart
   that has barely logged anything yet.
3. If `Reason` is `OOMKilled`: check the pod's memory limit versus its
   actual usage trend on the Grafana `pod / memory` dashboard leading up
   to the crash.
4. If probes are suspected: check the deployment manifest's
   `readinessProbe`/`livenessProbe` config against the app's actual health
   endpoint and port, and compare `initialDelaySeconds` against the app's
   real startup time.
5. Check recent deploys and image tag changes for this service -- a bad
   rollout is one of the most common causes and is visible in
   `kubectl rollout history`.
6. Check node conditions with `kubectl describe node <node>` for
   `DiskPressure` or `MemoryPressure` if crashes are correlated across
   multiple unrelated pods on the same node.

## Remediation Steps

1. **If caused by a bad deploy:** roll back immediately with
   `kubectl rollout undo deployment/<name>` rather than debugging forward
   under pressure.
2. **If `OOMKilled`:** raise the pod's memory limit as an immediate
   mitigation (`kubectl set resources` or update the manifest), then file
   a follow-up to profile actual memory usage and right-size properly.
3. **If a misconfigured probe:** fix the probe path/port/timeout in the
   manifest and redeploy; do not just delete the probe as a workaround,
   since it exists to protect real traffic from an unhealthy pod.
4. **If a missing config/secret:** correct the ConfigMap/Secret and
   restart the deployment (`kubectl rollout restart deployment/<name>`).
5. **If a node-level pressure condition:** cordon the affected node
   (`kubectl cordon <node>`) so the scheduler stops placing pods there,
   and drain it once replacement capacity is confirmed healthy.
6. After remediation, confirm restart count stops climbing and pods reach
   `Running`/`Ready` status (`kubectl get pods -w`) before closing.

## Escalation Path

If pods continue crash-looping after a rollback and no clear `Reason` is
found in `kubectl describe pod`, escalate to the platform on-call for a
deeper cluster-level investigation (node health, CNI issues, or control
plane problems).

## Related Runbooks

- `high-cpu-utilization.md` -- if CPU throttling is severe enough to fail
  liveness probes.
- `disk-space-critical.md` -- if `DiskPressure` node conditions are
  triggering the evictions.
