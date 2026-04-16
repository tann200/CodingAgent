**Scheduler & Admin Endpoints**

Overview

This repository includes a lightweight, scheduler-driven maintenance workflow (distillation/compaction) and HTTP admin endpoints to manage scheduler jobs at runtime. This doc summarizes the runtime knobs and examples for using the endpoints.

Config

The scheduler is configured via the existing config object under the `scheduler_jobs` map. Example (YAML):

```yaml
scheduler_jobs:
  periodic_distill_request:
    enabled: true
    interval: 60  # seconds between periodic distill requests
```

The orchestrator will register jobs declared in `scheduler_jobs` (name, enabled, interval). The `periodic_distill_request` job triggers distillation (compaction) at the configured interval by publishing `scheduler.distill_request` on the orchestrator EventBus.

Environment knobs

- `CODING_AGENT_SCHEDULER_HEARTBEAT`: Scheduler heartbeat loop interval in seconds (default used if unset).
- `CODING_AGENT_DISTILL_INTERVAL`: Default distill interval in seconds used by the periodic distill job when not configured explicitly.
- `CODING_AGENT_ADMIN_TOKEN`: Optional admin token. When set, admin HTTP endpoints require this token. Accepts Bearer `Authorization` header or `X-CodingAgent-Token` header.

HTTP Admin endpoints

The following endpoints are available on the HTTP server (`src/server/app.py`). They are intended for local admin/ops usage and are protected by `CODING_AGENT_ADMIN_TOKEN` if that environment variable is set.

List jobs
GET /scheduler/jobs

Enable a job from a registered factory
POST /scheduler/jobs/{name}/enable

Unregister (disable) a job (keeps factory)
POST /scheduler/jobs/{name}/unregister

Update job interval
POST /scheduler/jobs/{name}/interval
JSON body: {"interval": <seconds>}

Clear all registered jobs
POST /scheduler/jobs/clear

Auth behavior

If `CODING_AGENT_ADMIN_TOKEN` is set, requests must provide one of:
- `Authorization: Bearer <token>`
- `X-CodingAgent-Token: <token>`
If the token is not set in the environment the endpoints are open (useful for local development).

Examples

Assume the admin token is "s3cr3t", server running at http://localhost:8000

List jobs:
```bash
curl http://localhost:8000/scheduler/jobs
```

Unregister a job:
```bash
curl -X POST http://localhost:8000/scheduler/jobs/periodic_distill_request/unregister -H "Authorization: Bearer s3cr3t"
```

Enable a job:
```bash
curl -X POST http://localhost:8000/scheduler/jobs/periodic_distill_request/enable -H "X-CodingAgent-Token: s3cr3t"
```

Update interval:
```bash
curl -X POST http://localhost:8000/scheduler/jobs/periodic_distill_request/interval -H "Authorization: Bearer s3cr3t" -H "Content-Type: application/json" -d '{"interval": 30}'
```

Clear jobs:
```bash
curl -X POST http://localhost:8000/scheduler/jobs/clear -H "Authorization: Bearer s3cr3t"
```

Metrics

The `/metrics` endpoint exposes in-process metrics and includes admin-auth counters (attempts/successes/failures) for the admin endpoints when `CODING_AGENT_ADMIN_TOKEN` is set. See `src/server/app.py` for the exact metric names.

Testing notes

- Tests that interact with scheduler state must call `src.core.scheduler.worker.stop_scheduler()` and `src.core.scheduler.worker.clear_jobs()` in setup/teardown to avoid cross-test interference.
- When stubbing distillation, have the stub return `{"_compacted_history": [...]}` to trigger message replacement and the `message.compaction_applied` event.

Next steps

- Add negative-path tests for the admin endpoints (invalid JSON, unknown job name).
- Add tests asserting admin auth counters change on attempts/success/failure.
- Implement a WebSocket session endpoint for interactive streaming (larger task).

This is a short reference. For details see the code in `src/core/scheduler/worker.py` and `src/server/app.py`.

WebSocket Session Endpoint

The server exposes a WebSocket endpoint to receive EventBus events in real-time:

- URL: `/ws/session/{session_id}`
- Authentication: mirrors the admin endpoints. If `CODING_AGENT_ADMIN_TOKEN` is set,
  the client must provide either a Bearer token in the `Authorization` header, the
  `X-CodingAgent-Token` header, or include `?token=<token>` as a query param (convenience only).

Query parameters

- `events`: comma-separated list of event names to subscribe to immediately (e.g. `events=session.created,agent.start`).
  - Use `events=none` to start with no subscriptions.
  - `events=all` (or `events=` omitted) subscribes to the default set of events (see below).
- `queue_max_size`: per-connection queue size (default mirrors `CODING_AGENT_SSE_QUEUE_MAX`).
- `drop_policy`: `drop_oldest` (default) or `drop_new` — controls behaviour when the per-connection
  queue is full.
- `keepalive`: keepalive interval in seconds (default mirrors `CODING_AGENT_SSE_KEEPALIVE`).

Default event list

The default events subscribed to when `events` is omitted are:

- `agent.start`, `agent.end`, `tool.start`, `tool.end`, `mcp.server.status`, `workflow.step`,
  `llm.response`, `session.created`, `session.updated`, `perception.corrective_prompt`, `error`, `log`

Control messages (client -> server)

Send JSON control messages over the WebSocket to manage subscriptions at runtime:

- Subscribe to an event:

  {"type": "subscribe", "event": "session.created"}

- Unsubscribe from an event:

  {"type": "unsubscribe", "event": "session.created"}

- List current subscriptions:

  {"type": "list"}

- Ping (server replies with pong):

  {"type": "ping"}

Server responses

Server sends event envelopes and control envelopes as JSON objects with `event` and `data` fields:

- Event envelope example:

  {"event": "session.created", "data": {"session_id": "s1", "metadata": {...}}}

- Control envelope example (acknowledgement for subscribe/unsubscribe/list):

  {"event": "_control", "data": {"type": "subscribed", "event": "session.created"}}

Backpressure and dropped events

The per-connection queue bounds prevent unbounded memory growth for slow clients. When the queue
is full the server enforces the configured `drop_policy`:

- `drop_oldest`: evict the oldest queued event and enqueue the new event (default).
- `drop_new`: drop the incoming event.

Dropped events are recorded in in-process metrics exposed on `/metrics`.

WebSocket client example (browser JavaScript)

```javascript
const token = "YOUR_ADMIN_TOKEN"; // or omit if server is not protected
const ws = new WebSocket(`ws://localhost:8000/ws/session/s1?events=session.created&token=${token}`);

ws.addEventListener('open', () => {
  console.log('ws open');
});

ws.addEventListener('message', (evt) => {
  try {
    const msg = JSON.parse(evt.data);
    console.log('received', msg.event, msg.data);
  } catch (e) {
    console.warn('non-json message', evt.data);
  }
});

// Subscribe at runtime
ws.send(JSON.stringify({type: 'subscribe', event: 'agent.start'}));

// Unsubscribe at runtime
ws.send(JSON.stringify({type: 'unsubscribe', event: 'agent.start'}));

// List subscriptions
ws.send(JSON.stringify({type: 'list'}));
```

Security note: passing tokens in query parameters is convenient for browsers or tools that cannot set headers, but is less secure; prefer `Authorization: Bearer <token>` in production.
