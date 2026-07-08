# Technical Debt Report

## 1. Critical Debt

- **Reed-Solomon Integration**: `reedsolo` is in requirements but the fragmentation engine is not yet wired to the API.
- **Database Migrations**: No Alembic setup yet.
- **Provider Auth**: OneDrive requires a complex OAuth flow for user-based storage (currently app-only).

## 2. Acceptable Debt

- **Metrics**: Basic `/metrics` endpoint; needs more granular Prometheus counters.
- **Error Handling**: Standard FastAPI exceptions used; could use more domain-specific errors.
- **Task Queue**: Background worker is in-process; for high scale, should move to Redis-backed queue.

## 3. Known Limitations

- Single-region deployment (Render Oregon).
- Simple fragment allocation logic in scheduler.
