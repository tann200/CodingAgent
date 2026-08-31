# Production Deployment Guide (STAB-06)

This document defines the production endpoint **exposure and authentication
policy** for the CodingAgent HTTP/SSE/WebSocket server, the **TLS /
reverse-proxy expectations** operators must meet, and a **deployment
checklist**.

The endpoint policy below is the operational rendering of the single source of
truth in [`src/server/exposure_policy.py`](../src/server/exposure_policy.py).
The contract test `tests/unit/test_exposure_policy.py` asserts that every route
registered on the FastAPI app has a documented policy here — so a new endpoint
cannot be added without also documenting its exposure and auth.

---

## 1. Bind / exposure security model

- The server binds to **loopback only by default** (`127.0.0.1:8000`). There is
  currently **no environment variable for the bind host/port** — it is
  hardcoded to loopback in `run_server()` and the orchestrator's embedded
  server thread.
- `validate_server_exposure(host, admin_token)` (`src/server/server_config.py`)
  **refuses to start** when binding to any non-loopback address
  (`0.0.0.0`, `::`, a LAN IP, or a hostname) **without** `CODINGAGENT_ADMIN_TOKEN`
  configured. This fail-closed guard is the primary protection against exposing
  agent tooling off-machine unauthenticated.
- Recommended production posture: put the server behind a reverse proxy (see
  §3) and keep the application bind on loopback. Only the proxy listens on a
  non-loopback address.

---

## 2. Endpoint exposure & authentication policy

Every endpoint the server exposes is listed below. The auth column references
the governing environment variable:

| Env var | Purpose | Credential form |
|---|---|---|
| `CODINGAGENT_ADMIN_TOKEN` (legacy alias `CODING_AGENT_ADMIN_TOKEN`) | Admin auth for control endpoints | `Authorization: Bearer <token>` **or** `X-CodingAgent-Token: <token>` |
| `CODINGAGENT_METRICS_AUTH` (legacy alias `CODING_AGENT_METRICS_AUTH`) | `/metrics` HTTP Basic auth | `Authorization: Basic <base64(user:pass)>` in the single `user:pass` format |

### Policy table

| Method | Path | Exposure | Auth | Notes |
|---|---|---|---|---|
| `GET` | `/health` | **Public** (always) | None | Liveness/readiness + optional-feature capability flags. Intended for load-balancer/orchestrator probes. |
| `GET` | `/metrics` | Public by default | `CODINGAGENT_METRICS_AUTH` (HTTP Basic) | Prometheus text metrics. Fully public if the env var is unset; Basic-auth otherwise. |
| `POST` | `/session` | Auth-gated* | Admin token | Create a session. |
| `GET` | `/session/{session_id}/events` | Auth-gated* | Admin token (header) | SSE event stream. Header token only; query-string tokens are never accepted. |
| `WS` | `/ws/session/{session_id}` | Auth-gated* | Admin token (header) | WebSocket event stream. Verified at handshake with a header token; closes `1008` on failure; query-string tokens never accepted. |
| `GET` | `/scheduler/jobs` | Auth-gated* | Admin token | List scheduler jobs. |
| `POST` | `/scheduler/jobs/{name}/unregister` | Auth-gated* | Admin token | Unregister a job. |
| `POST` | `/scheduler/jobs/clear` | Auth-gated* | Admin token | Clear all jobs. |
| `POST` | `/scheduler/jobs/{name}/enable` | Auth-gated* | Admin token | Enable a factory job. |
| `POST` | `/scheduler/jobs/{name}/interval` | Auth-gated* | Admin token | Update a job interval. |
| `POST` | `/task` | Auth-gated* | Admin token | Submit an async task. |
| `GET` | `/task/{task_id}` | Auth-gated* | Admin token | Query a task. |
| `POST` | `/task/{task_id}/cancel` | Auth-gated* | Admin token | Cancel a task. |
| `GET` | `/tasks` | Auth-gated* | Admin token | List recent tasks. |

\* **Auth-gated** means: the endpoint enforces the admin token **whenever
`CODINGAGENT_ADMIN_TOKEN` is set**, and is open only when it is unset. With the
token configured, the whole control surface requires admin auth — the single
exceptions are `/health` (always public) and an unauthenticated `/metrics`.

### Framework (dev) routes — must be disabled in production

FastAPI generates `/docs`, `/redoc`, `/openapi.json` (and the Swagger
`/docs/oauth2-redirect` helper). These are **dev helpers and must not be
reachable** on a production deployment. Disable them by disabling the app's
docs/redoc/OpenAPI URLs and blocking them in the reverse proxy ACL.

---

## 3. TLS and reverse-proxy expectations

The application itself does **not** terminate TLS. Operational expectations:

1. **TLS termination happens at the reverse proxy** (e.g. an ingress
   controller, `nginx`, HAProxy, or the PaaS load balancer). TLS (>= TLS 1.2;
   prefer 1.3) is mandatory for any non-local traffic.
2. **The proxy terminates the encrypted connection and forwards plaintext
   HTTP/WS to the loopback-bound application** (e.g. `127.0.0.1:8000`).
3. The proxy must support and forward **WebSocket upgrade** (`Upgrade:
   websocket`) and **Server-Sent Events** (long-lived `text/event-stream`)
   connections without buffering or aggressive keep-alive timeouts that cut off
   streams.
4. The proxy must forward the **`Authorization` and `X-CodingAgent-Token`
   headers** to the application so admin auth is enforced at the app (do not
   strip them).
5. If you must expose `/metrics` at the edge, either keep the proxy ACL
   restricted, set `CODINGAGENT_METRICS_AUTH`, or scrape from the application
   port directly (recommended for Prometheus) rather than through a public
   route.
6. Block `/docs*`, `/redoc*`, `/openapi.json` at the proxy (or disable them in
   the app) — they are dev helpers.
7. **Pathing:** if the proxy strips a path prefix, it must rewrite
   `{session_id}` route segments consistently; path parameters are validated by
   the app.

---

## 4. Production deployment checklist

**Authentication & exposure**
- [ ] `CODINGAGENT_ADMIN_TOKEN` set to a long, high-entropy value (>= 32 chars).
- [ ] Admin token distributed via `/etc/environment`, a secrets manager, or
      container secret (never committed to the repo, never in the image).
- [ ] Non-loopback bind without a token verified to **fail closed**
      (`validate_server_exposure`). Do not bypass it.
- [ ] Decided the `/health` posture (public / behind proxy ACL) and `/metrics`
      posture (set `CODINGAGENT_METRICS_AUTH` or scrape privately).
- [ ] Confirmed SSE/WebSocket clients send the token via a **header**, not the
      query string.

**TLS & network**
- [ ] TLS terminated at the proxy on >= TLS 1.2 (prefer 1.3) with a valid cert.
- [ ] App left bound to loopback; only the proxy binds a public address.
- [ ] Proxy forwards WebSocket upgrade + SSE streams without premature timeouts.
- [ ] Proxy forwards `Authorization` / `X-CodingAgent-Token` headers.
- [ ] `docs`/`redoc`/`openapi` disabled or ACL-blocked.

**Verification**
- [ ] `GET /health` returns `200` through the proxy.
- [ ] Control endpoints return `401` from a public path without a token, `200`
      with a valid token.
- [ ] `/metrics` obeys the configured `/metrics` auth policy.
- [ ] WebSocket handshake rejects a missing/wrong token with `1008`.
- [ ] Run the unit suite: `python -m pytest tests/unit/test_exposure_policy.py`
      green (locks the endpoint→policy mapping).

---

## 5. Reference

- Source of truth registry: `src/server/exposure_policy.py`
- Bind/exposure guard: `src/server/server_config.py`
- Auth enforcement: `src/server/app.py` (`_require_admin_auth`), `src/server/websocket_handler.py`
- Contract tests: `tests/unit/test_exposure_policy.py`
