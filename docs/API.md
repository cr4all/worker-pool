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

### Proxies (optional)

- Env `PROXIES_CSV` (default `proxies.csv`): CSV with columns `Region`, `Host`, `Port`, `User`, `Pass`. Used only when `POST /start` sets **`proxy` to `AUTO`** (see below).
- The pool image loads the proxy extension only when Docker receives `PROXY_HOST` (and related `PROXY_*`). Otherwise Chromium runs **direct** (no proxy extension).

Per-request **`proxy`** on `POST /start` (string enum, default `AUTO`):

| `proxy` | Behavior |
|---------|----------|
| **`AUTO`** | If `PROXIES_CSV` has at least one valid row: pick the row index with **minimum** current use among running pool containers (random tie-break), pass `PROXY_HOST`, `PROXY_PORT`, `PROXY_USER`, `PROXY_PASS`, and set labels `chrome-pool.proxy-index` and `chrome-pool.proxy-region` (no secrets in labels). If the file is missing, empty, or has no valid rows: same as direct Chrome (no `PROXY_*`). |
| **`NONE`** | Never pass `PROXY_*`, even if the CSV has rows. |
| **`USER`** | **Required**: body field `user_proxy` with `host`, `port` (1–65535), and optional `user`, `password`, `region`. Passes `PROXY_*` from that object. Labels: `chrome-pool.proxy-source=user` and `chrome-pool.proxy-region` (from `user_proxy.region`, or empty string if omitted). No `chrome-pool.proxy-index` label. |

Validation: `user_proxy` is **required** when `proxy` is `USER`, and **must be omitted** when `proxy` is `AUTO` or `NONE` → otherwise **422**.

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

Start one Chrome container using the image from `CHROME_DOCKER_IMAGE` (default `proxy-chrome:latest`), label it as pool-managed, and expose:

- container `5900/tcp` → host VNC port
- container `9222/tcp` → host CDP port
- container `8080/tcp` (noVNC web UI) → host `novnc_port`

**Auth**: required if `API_KEY` is set

### Request body (JSON)

All fields optional unless `proxy` is `USER`.

- **`vnc_password`** (string, optional): VNC password passed to the container as `VNC_PASS`. If omitted, the server uses env `VNC_PASS` (default `mystakechrome`). Must be non-empty when sent (after trim); max length 128.

```json
{}
```

Name only:

```json
{ "name": "my-chrome-1" }
```

Custom VNC password for this instance:

```json
{ "name": "my-chrome-1", "vnc_password": "my-secret" }
```

Explicit built-in (CSV) selection — same as omitting `proxy`:

```json
{ "name": "my-chrome-1", "proxy": "AUTO" }
```

No proxy:

```json
{ "proxy": "NONE" }
```

Caller-supplied proxy:

```json
{
  "proxy": "USER",
  "user_proxy": {
    "host": "proxy.example.com",
    "port": 8888,
    "user": "alice",
    "password": "secret",
    "region": "KR"
  }
}
```

`user_proxy.user` and `user_proxy.password` default to empty strings if omitted. `user_proxy.region` is optional.

### 200 response

```json
{
  "name": "chrome-pool-abc123def456",
  "vnc_port": 5901,
  "cdp_port": 9223,
  "novnc_port": 6080,
  "vnc_password": "mystakechrome",
  "novnc_url": "https://web-6080.example.com/index.html?password=mystakechrome",
  "proxy_index": 0,
  "proxy_region": "UK"
}
```

- **`novnc_url`**: present when env **`NOVNC_DOMAIN`** is set (e.g. `example.com`). Built as `https://web-<novnc_port>.<NOVNC_DOMAIN>/index.html?password=<url-encoded effective VNC password>` (same password as `vnc_password`: request `vnc_password` or default `VNC_PASS`). **`null`** when `NOVNC_DOMAIN` is unset or blank.
- **`proxy_index`**: set only when `proxy` was `AUTO` and a CSV row was used; **`null`** for `NONE`, for `AUTO` with no CSV rows, or for `USER`.
- **`proxy_region`**: `null` only when no proxy was applied (`NONE`, or `AUTO` with no valid CSV rows). Otherwise the region string from the CSV row or from `user_proxy.region` (may be an empty string `""` if the CSV or request left region blank).

### Status codes

- **400**: invalid container name
- **409**: container already exists
- **500**: docker run failed / CDP not ready within timeout
- **503**: no free sequential port slot
- **429**: global limit reached (no queue; rejected immediately)
- **422**: invalid JSON / body validation failed (e.g. `USER` without `user_proxy`, or `user_proxy` present when `proxy` is not `USER`)

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
    {
      "name": "chrome-pool-a",
      "vnc_port": 5901,
      "cdp_port": 9223,
      "novnc_port": 6080,
      "proxy_index": 0,
      "proxy_region": "UK"
    }
  ]
}
```

`proxy_index` is `null` unless the instance was started with **`AUTO`** and a CSV row was used. `proxy_region` is `null` only when no proxy is in use; otherwise it matches the Docker label (CSV or `USER` request; may be `""`).

Notes:

- Under heavy concurrency, Docker may briefly return a container before its port bindings are visible via `inspect`. In that case this API may temporarily return `null` for `vnc_port`/`cdp_port`/`novnc_port`, but the container **name will still be listed**, so the count stays correct.

---

## Port allocation policy

- Slot \(k = 0, 1, 2, ...\)
- Host VNC port: `5901 + k` → container `5900/tcp`
- Host CDP port: `9223 + k` → container `9222/tcp`
- Host noVNC port: `6080 + k` → container `8080/tcp`
- Slots already used by the pool, or not bindable on the host, are skipped.
