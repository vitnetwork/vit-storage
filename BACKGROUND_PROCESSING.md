# Background Processing Report

## 1. Worker Status

| Worker | Task | Interval | Error Handling | Shutdown Behavior |
|--------|------|----------|----------------|-------------------|
| TachyonVerificationWorker | Fragment health check | 1 hour | Retry with backoff | Graceful stop via lifespan |

## 2. Identified Gaps

- **Retry Logic**: Simple sleep(10) on failure; needs exponential backoff.
- **Dead-lettering**: No mechanism for tracking permanently failed fragments.
- **Cancellation**: Long-running verification might not respond instantly to SIGTERM.

## 3. Recommended Actions

- Implement a task queue (e.g., TaskIQ or ARQ) for better job management.
- Add Prometheus metrics for worker success/failure rates.
- Implement per-fragment retry counters.
