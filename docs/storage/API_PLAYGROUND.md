# VIT Storage API Playground Guide

## 1. Introduction
The **API Playground** is an integrated interactive developer tool built directly into the VIT Storage User Interface. It provides an intuitive, robust alternative to raw Swagger documentation (`/docs`) for testing, debugging, and integrating with the Decentralized Swarm Coordination APIs.

---

## 2. Key Capabilities
- **Method & Route Selection**: Quickly test all standard REST HTTP verbs (`GET`, `POST`, `DELETE`) with dynamic target URL paths.
- **Authorization Support**: Easily pass JWT (JSON Web Token) bearer tokens in the authorization headers.
- **Payload Editing**: Standard raw JSON editing with a pretty-formatted responsive output stream container.
- **One-Click Requests**: Fully client-side execution using `fetch` with no backend proxy requirements, ensuring fast and safe transfers.

---

## 3. Sample REST Requests & Responses

### 3.1 Get Coordination Status
- **Method**: `GET`
- **Route**: `/api/v1/status`
- **Description**: Returns general metrics, active cloud node counts, and detailed provider connection statuses.
- **Sample Response**:
  ```json
  {
    "status": "operational",
    "module": "tachyon.api",
    "version": "1.1.0",
    "active_nodes": 4,
    "manifest_count": 12,
    "total_bytes": 1048576,
    "providers": {
      "s3_aws": {
        "status": "online",
        "latency_ms": 23
      },
      "dropbox_storage": {
        "status": "online",
        "latency_ms": 45
      }
    }
  }
  ```

### 3.2 List File Manifests
- **Method**: `GET`
- **Route**: `/api/v1/files?limit=10&offset=0`
- **Description**: Lists all active metadata manifests tracked by the coordination layer.
- **Sample Response**:
  ```json
  [
    {
      "filename": "ledger.json",
      "total_size": 2048,
      "fragments": [
        {
          "name": "a1b2c3d4_0",
          "provider": "s3_aws",
          "size": 512,
          "checksum": "sha256_abcdef..."
        }
      ],
      "redundancy_ratio": 1.5
    }
  ]
  ```

---

## 4. Error Code Explanations
When performing queries, the coordination layer may return specific status codes.

| Code | Explanation | Recommended Action |
|---|---|---|
| `401 Unauthorized` | Invalid or expired JWT Bearer token | Refresh authorization token and paste in Auth header field |
| `404 Not Found` | Manifest metadata or physical shard cannot be resolved | Verify file ID matching and rerun background audit worker |
| `413 Payload Too Large` | Upload file exceeds settings maximum size (default 100 MB) | Chunk files or edit `TACHYON_MAX_FILE_SIZE_MB` in settings |
| `500 Server Error` | Backend system/database failed to process shard operations | Inspect database connection and verify configuration keys |
