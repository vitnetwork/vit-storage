# vit-storage Architecture

> Formerly `vit-tachyon`, renamed to `vit-storage` to align with the VIT Network ecosystem's
> repository naming convention. The existing "Tachyon Fabric" implementation is the canonical
> engine behind this repository — no code was rewritten as part of the rename.

## Overview

`vit-storage` is the VIT Network's decentralized, swarm-based storage fabric. It aggregates
capacity across multiple cloud/object-storage providers, applies erasure coding for redundancy
and speed, and exposes a single storage API/SDK/CLI to the rest of the ecosystem (primarily
`vit-network`).

## Current Implementation (existing code)

- `tachyon/core` — swarm coordinator + EEC (erasure coding) engine
- `tachyon/providers` — provider adapters (Google Drive, S3, IPFS, etc.)
- `tachyon/api` — FastAPI service exposing the storage API
- `main.py` — service entrypoint (`uvicorn tachyon.main:app`)

## Planned Package Layout

The following top-level packages define the target module boundaries. Existing code in
`tachyon/` will be incrementally reorganized into this layout (tracked in `ROADMAP.md` —
no functional code was moved as part of this scaffolding pass):

| Package | Responsibility |
|---|---|
| `core/` | Domain models, coordination logic |
| `network/` | Peer/provider network transport layer |
| `gateway/` | Public-facing storage gateway service |
| `sdk/` | Client SDK for consumers (e.g. `vit-network`) |
| `cli/` | Command-line administration tooling |
| `storage/` | Low-level storage engine / backends |
| `metadata/` | Metadata indexing and lookup |
| `replication/` | Fragment replication across providers |
| `chunking/` | Data chunking / erasure-coded fragmentation |
| `encryption/` | At-rest and in-transit encryption |
| `integrity/` | Integrity verification, checksums, repair |
| `cache/` | Read/write caching layer |
| `monitoring/` | Metrics, health checks, alerting |
| `api/` | Public HTTP/gRPC API surface |

## Integration Points

- Consumed by: `vit-network` (primary consumer of the storage API)
- Deployed via: `render.yaml` (existing Render.com deployment)
- Security: encryption module protects data at rest; integrity module verifies fragment health

## Data Flow

```
Client → gateway/ → chunking/ → encryption/ → replication/ → storage providers (network/)
                                                     ↓
                                              metadata/ (index)
Client ← gateway/ ← integrity/ ← cache/ ← reconstruction ← storage providers
```
