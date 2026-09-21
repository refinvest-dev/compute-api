# Compute Prometheus metrics

Metrics are opt-in, process-local aggregate observations for development and verification. They do
not replace fixed-input benchmarking, its canonical result hash gate, or a reviewed official
baseline. Use protected structured logs with `run_id` for individual jobs. Counters reset on
restart; Prometheus `rate()` and `increase()` account for resets. API and Worker registries must
be scraped separately. Never put identifiers, payloads, credentials, error text, or DB URLs in
labels or exposition.

| Metric | Type / unit | Owner | Recording point; bounded labels |
|---|---|---|---|
| `refinvest_compute_http_requests_total` | Counter / requests | API | Response or error; `operation=submit,status,assets,series,other`; `status_class=2xx,3xx,4xx,5xx` |
| `refinvest_compute_http_request_duration_seconds` | Histogram / seconds | API | Request start through response creation/error; same `operation` |
| `refinvest_compute_backtest_jobs_accepted_total` | Counter / jobs | API | New durable acceptance; no labels |
| `refinvest_compute_backtest_idempotency_replays_total` | Counter / replays | API | Existing acceptance returned; no labels |
| `refinvest_compute_backtest_capacity_rejections_total` | Counter / rejections | API | Capacity exception returned as 503; no labels |
| `refinvest_compute_worker_claims_total` | Counter / attempts | Worker | Successful DB claim, including lease recovery; no labels |
| `refinvest_compute_worker_queue_wait_seconds` | Histogram / seconds | Worker | Durable creation timestamp to each successful claim, including retry delay; no labels |
| `refinvest_compute_worker_attempt_duration_seconds` | Histogram / seconds | Worker | Executor start through outcome and heartbeat stop, before DB persistence; no labels |
| `refinvest_compute_worker_terminal_transitions_total` | Counter / transitions | Worker | DB `mark_completed`/`mark_failed` returned true; `status=COMPLETED,FAILED` |
| `refinvest_compute_worker_retries_scheduled_total` | Counter / schedules | Worker | DB `schedule_retry` returned true; no labels |
| `refinvest_compute_worker_timeouts_total` | Counter / outcomes | Worker | Process executor returned `EXECUTION_TIMEOUT`, even if terminal persistence later fails; no labels |
| `refinvest_compute_worker_failures_total` | Counter / transitions | Worker | Successful DB failed transition; `error_code=MISSING_REQUIRED_DATA,DATASET_CORRUPTION,CALENDAR_RESOLUTION_FAILED,PRICE_DATA_MISSING,EXECUTION_TIMEOUT,DSL_INVALID,UNSPECIFIED` |

Health and the separate metrics endpoint are excluded from HTTP counts. Unknown API paths map to
`other`; raw URL paths never become labels. API acceptance counts Compute jobs, not Core user
submissions; replay and rejection never increment acceptance. Failed DB updates and lease loss do
not increment terminal or retry counters. Worker duration includes spawn, child execution, IPC,
timeout/kill handling, and heartbeat shutdown, but excludes terminal/retry DB persistence; it is
different from benchmark `total_execution`. The timeout counter measures the process outcome,
not a committed failure. A child can fail or time out without complete stage diagnostics.

Initial buckets are HTTP `.005,.01,.025,.05,.1,.25,.5,1,2.5,5,10` seconds; queue wait
`.5,1,2.5,5,10,30,60,120,300,600,1800,3600` seconds; execution
`.1,.25,.5,1,2,5,10,30,60,120,300,600,900` seconds. Each histogram also has `+Inf`. The finite
maxima cover typical millisecond-to-second HTTP, second-to-hour queue wait, and up-to-10-minute
execution plus termination grace. These are starting ranges, not SLOs.

## Scrape boundary

Set `REFINVEST_API_METRICS_ENABLED=true` and `REFINVEST_WORKER_METRICS_ENABLED=true` in the
respective service environments. Both default to `false` and open no extra port. Optional
`REFINVEST_API_METRICS_BIND/PORT` and `REFINVEST_WORKER_METRICS_BIND/PORT` default to
`127.0.0.1:9101` and `127.0.0.1:9102`. Only IPv4 loopback bind addresses are accepted.
Invalid settings and occupied ports fail startup. Keep distinct ports on a shared host. Use a
local scrape agent or approved host-level forwarding; never proxy metrics to a public network.
The existing private API listener and TLS/network isolation rules remain in force.

```text
curl --fail http://127.0.0.1:9101/metrics
curl --fail http://127.0.0.1:9102/metrics
```

The API systemd unit runs one Uvicorn process; multiple Uvicorn workers are unsupported with this
per-process registry and single listener. The durable Worker owns its registry and listener. Its
spawn child starts no server and emits no Prometheus metric. Existing queue/execution structured
events remain for run-level investigation. The JSON `backtest_runs_failed_total` log-derived event
remains for compatibility and describes the same committed failure as
`refinvest_compute_worker_failures_total`; never sum both sources in a dashboard.

Production stage histograms are deferred. Transferring complete `ExecutionStageObserver` timing
from the spawn child needs a distinct IPC diagnostic result and timeout/kill tests. The benchmark
observer remains available, but no production stage metric is exposed. Snapshot cache hit/miss
metrics await a real cache. Prometheus/Grafana composition belongs to the next integration stage;
operational alerts, SLOs, and tracing are outside this scope.
