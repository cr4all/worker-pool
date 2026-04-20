# Chrome pool manager — HTTP API spec

Code reference: `app/main.py` (service `version="1.0.0"`)

## Common

- **Base URL**: depends on deployment (example: `http://127.0.0.1:8080`)
- **Encoding**: UTF-8 JSON
- **OpenAPI**: `GET /openapi.json`
- **Swagger UI**: `GET /docs`

### Capacity limit (global)

`POST /start` is globally rate-limited by the number of *currently running* pool containers.

- If `MAX_RUNNING` is set, it is used as the limit.
- If `MAX_RUNNING` is not set, the limit is auto-computed from total RAM:
  - `max(1, floor(total_ram_bytes * 0.85 / 1GiB))`

### Auth (optional)

If `API_KEY` env var is set, all endpoints **except** `GET /health` require:

```http
Authorization: Bearer <API_KEY>
```

- Missing/invalid header → **401**
- Wrong key → **403**

### Error responses

- For `HTTPException`, FastAPI returns `{"detail": "<message>"}`.
- Validation errors return **422** with a structured `detail` array.

---

## `GET /health`

Docker client availability check.

**Auth**: not required

### 200 response

```json
{
  "ok": true,
  "docker": true,
  "docker_error": null
}
```

---

## `POST /start`

Start one Chrome container using Docker image `suyash5053/chromium-vnc-cdp`, label it as pool-managed, and expose:

- container `5900/tcp` → host VNC port
- container `9222/tcp` → host CDP port

**Auth**: required if `API_KEY` is set

### Request body (JSON)

```json
{}
```

Optional:

```json
{ "name": "my-chrome-1" }
```

### 200 response

```json
{
  "name": "chrome-pool-abc123def456",
  "vnc_port": 5901,
  "cdp_port": 9223,
  "vnc_password": "mystakechrome"
}
```

### Status codes

- **400**: invalid container name
- **409**: container already exists
- **500**: docker run failed / CDP not ready within timeout
- **503**: no free sequential port slot
- **429**: global limit reached (no queue; rejected immediately)
- **422**: invalid JSON / body validation failed

#### 429 response body example

```json
{
  "detail": {
    "error": "limit reached",
    "current": 3,
    "max": 3
  }
}
```

---

## `POST /stop`

Remove a **pool-managed** container by name (`docker rm -f`).

**Auth**: required if `API_KEY` is set

### Request body (JSON)

```json
{ "name": "chrome-pool-abc123def456" }
```

### 200 response

```json
{ "ok": true, "name": "chrome-pool-abc123def456" }
```

### Status codes

- **400**: missing name OR container exists but is not pool-managed (missing label)
- **404**: container not found
- **500**: docker rm failed

---

## `POST /stopall`

Remove all running pool-managed containers (label `chrome-pool.managed=1`).

**Auth**: required if `API_KEY` is set

### Request body

None.

### 200 response

```json
{
  "stopped": ["chrome-pool-a", "chrome-pool-b"],
  "errors": [
    { "name": "chrome-pool-x", "error": "..." }
  ]
}
```

---

## `GET /list`

List running pool-managed containers and their host ports.

**Auth**: required if `API_KEY` is set

### 200 response

```json
{
  "instances": [
    { "name": "chrome-pool-a", "vnc_port": 5901, "cdp_port": 9223 }
  ]
}
```

Notes:

- Under heavy concurrency, Docker may briefly return a container before its port bindings are visible via `inspect`. In that case this API may temporarily return `null` for `vnc_port`/`cdp_port`, but the container **name will still be listed**, so the count stays correct.

---

## Port allocation policy

- Slot \(k = 0, 1, 2, ...\)
- Host VNC port: `5901 + k`
- Host CDP port: `9223 + k`
- Slots already used by the pool, or not bindable on the host, are skipped.
