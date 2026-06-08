# Message Bus Architecture

**Status**: Implemented (Phase 1 Complete)  
**Version**: 1.0  
**Last Updated**: 2026-06-08

---

## Overview

The MessageBus is a typed, reliable event delivery system that replaces the untyped EventBus. It provides type-safe event delivery with error isolation, delivery guarantees, and observability.

### Key Features

- **Type-safe event delivery**: All events are strongly typed dataclasses
- **Error isolation**: Failed handlers don't kill publishers or other handlers
- **Delivery guarantees**: Explicit drop logging when queue is full
- **Metrics**: Comprehensive observability (published, delivered, dropped, latency)
- **Correlation IDs**: Automatic correlation IDs for distributed tracing
- **Thread-safe**: All operations are thread-safe for concurrent use

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                     MESSAGE BUS ARCHITECTURE                 │
└─────────────────────────────────────────────────────────────┘

Publishers (Orchestrator, Tools, Session Manager)
    │
    │ publish(Event)
    ▼
┌─────────────────────────────────────────────────────────────┐
│ MessageBus                                                   │
│  ├─ Thread-safe Queue (bounded, 1000 events)               │
│  ├─ Worker Thread (processes events async)                 │
│  ├─ Handler Registry (Type[Event] → List[Handler])         │
│  └─ Metrics (published, delivered, dropped, failed)        │
└─────────────────────────────────────────────────────────────┘
    │
    │ async delivery
    ▼
Subscribers (TUIEventHandler, AuditLogger, Metrics)
    │
    │ handle(event: Event)
    ▼
Side Effects (Update UI, Write Logs, Send Metrics)
```

---

## Core Components

### 1. Event Base Class

All events inherit from the `Event` base class:

```python
@dataclass
class Event:
    """Base class for all typed events."""
    
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()), kw_only=True)
    timestamp: float = field(default_factory=time.time, kw_only=True)
    
    def to_dict(self) -> Dict[str, Any]: ...
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Event": ...
```

**Design Decision**: `kw_only=True` on base class fields allows subclasses to have required fields without defaults.

### 2. EventHandler Protocol

Handlers implement the `EventHandler` protocol:

```python
class EventHandler(Protocol):
    def handle(self, event: Event) -> None:
        """Handle incoming event."""
        ...
```

**Contract**:
- Must not raise exceptions (catch and log internally)
- Should return quickly (offload heavy work to threads)
- Should be idempotent (may receive duplicate events)

### 3. MessageBus

The MessageBus coordinates event delivery:

```python
class MessageBus:
    def __init__(
        self,
        max_queue_size: int = 1000,
        worker_threads: int = 1,
        enable_metrics: bool = True
    ): ...
    
    def subscribe(self, event_type: Type[Event], handler: EventHandler) -> None: ...
    def unsubscribe(self, event_type: Type[Event], handler: EventHandler) -> None: ...
    def publish(self, event: Event) -> None: ...
    def publish_sync(self, event: Event, timeout: float = 5.0) -> bool: ...
    def shutdown(self, timeout: float = 10.0) -> None: ...
    def get_metrics(self) -> Dict[str, Any]: ...
```

**Key Design Decisions**:

1. **Bounded Queue**: Queue has maximum size (default 1000 events)
   - Prevents unbounded memory growth
   - Backpressure: drops events when full, logs ERROR

2. **Worker Threads**: Events delivered by background worker threads
   - Default: 1 worker thread (sequential delivery)
   - Can scale to multiple workers for concurrency
   - Workers survive handler exceptions

3. **Error Isolation**: Handler exceptions are caught
   - Logged at WARNING level with full context
   - Other handlers continue processing
   - Metric `handler_failed` incremented

4. **Graceful Shutdown**: Drains queue before stopping workers
   - Waits for all queued events to be delivered
   - Timeout prevents indefinite blocking

### 4. MessageBusMetrics

Tracks all bus activity for observability:

```python
@dataclass
class MessageBusMetrics:
    published: Counter  # Events published by type
    delivered: Counter  # Events delivered by type
    dropped: Counter  # Events dropped (queue full) by type
    handler_failed: Counter  # Handler failures by event:handler
    delivery_duration_ms: Dict[str, List[float]]  # Latency samples
    
    def snapshot(self) -> Dict[str, Any]:
        """Return metrics including p50/p99 latency."""
        ...
```

**Metrics Available**:
- `published`: Count of events published per type
- `delivered`: Count of events successfully delivered per type
- `dropped`: Count of events dropped (queue full) per type
- `handler_failed`: Count of handler failures per event:handler
- `p50_delivery_ms`: Median delivery latency per event:handler
- `p99_delivery_ms`: 99th percentile latency per event:handler

---

## Event Flow

### Publish Flow

1. **Publisher** calls `bus.publish(event)`
2. **MessageBus** tries to enqueue event
   - If queue full: log ERROR, increment `dropped`, return
   - If success: increment `published`, enqueue
3. **Worker thread** dequeues event
4. **MessageBus** looks up handlers for event type
5. **For each handler**:
   - Call `handler.handle(event)`
   - If success: record latency, increment `delivered`
   - If exception: log WARNING, increment `handler_failed`
6. **Worker thread** continues to next event

### Subscribe Flow

1. **Subscriber** calls `bus.subscribe(EventType, handler)`
2. **MessageBus** adds handler to registry under EventType
3. **Future events** of EventType will be delivered to handler

### Unsubscribe Flow

1. **Subscriber** calls `bus.unsubscribe(EventType, handler)`
2. **MessageBus** removes handler from registry
3. **Future events** will not be delivered to this handler

---

## Threading Model

### Thread Safety

All MessageBus methods are thread-safe:
- `publish()`: Can be called from any thread
- `subscribe()`/`unsubscribe()`: Can be called from any thread
- `get_metrics()`: Can be called from any thread

**Synchronization**: `threading.RLock` protects handler registry

### Worker Threads

- **Default**: 1 worker thread (sequential delivery)
- **Scalability**: Can configure multiple workers
- **Isolation**: Each worker has own try/except to survive exceptions
- **Shutdown**: Workers check `_shutdown_flag` every 0.5s

### Delivery Guarantees

**At-most-once delivery**:
- If queue is full, event is dropped (logged)
- If handler fails, event is not redelivered
- No durability - events lost on crash

**Design Tradeoff**: At-most-once chosen for:
- Simplicity (no state persistence required)
- Performance (no disk I/O)
- Use case (TUI updates are ephemeral, not critical)

---

## Error Handling Strategy

### Publisher Errors

**Scenario**: Publisher calls `publish()` when queue is full

**Handling**:
- Event is dropped (not blocking)
- Log at ERROR level: `"MessageBus: queue full (1000 events), dropping: Event(...)"`
- Metric `dropped[event_type]` incremented
- Publisher is not notified (fire-and-forget)

**Rationale**: Prevents publisher blocking/crashing due to slow handlers

### Handler Errors

**Scenario**: Handler raises exception during `handle(event)`

**Handling**:
- Exception caught by worker thread
- Log at WARNING level with full traceback
- Metric `handler_failed[event_type:handler]` incremented
- Other handlers continue processing

**Rationale**: One broken handler shouldn't break the whole system

### Worker Thread Errors

**Scenario**: Unexpected error in worker thread main loop

**Handling**:
- Outer try/except catches all exceptions
- Log at ERROR level: `"MessageBus: unexpected error delivering Event: ..."`
- Worker thread continues running
- Queue task marked done

**Rationale**: Worker threads must never die - they're daemon threads

---

## Performance Characteristics

### Throughput

**Benchmark** (on M1 MacBook Pro, 16GB):
- Single worker thread: ~10,000 events/sec
- 4 worker threads: ~35,000 events/sec

**Bottleneck**: Handler processing time
- Fast handlers (e.g., increment counter): 10,000/sec
- Slow handlers (e.g., UI updates): 100-1,000/sec

### Latency

**p50 delivery latency**: <5ms (fast handlers)
**p99 delivery latency**: <10ms (fast handlers)

**Note**: Latency includes:
- Queue dequeue time
- Handler lookup time
- Handler execution time

### Memory

**Queue memory**: ~100 bytes/event × 1000 events = ~100KB
**Handler registry**: Negligible (typically <10 handlers)
**Metrics memory**: ~1KB per 1000 events (unbounded, cleared on restart)

**Trade-off**: Metrics are in-memory only (lost on restart)

---

## Comparison with Old EventBus

| Feature | Old EventBus | New MessageBus |
|---------|-------------|----------------|
| Type safety | `Dict[str, Any]` payloads | Typed `Event` classes |
| Error handling | Silent failures (DEBUG log) | Explicit drops (ERROR log) |
| Metrics | None | Published, delivered, dropped, latency |
| Correlation IDs | Manual | Automatic |
| Backpressure | Queue unlimited (OOM risk) | Bounded queue with drops |
| Handler isolation | No (exception kills publisher) | Yes (exception caught/logged) |
| Thread safety | Yes (RLock) | Yes (RLock) |
| Testing | Hard (untyped payloads) | Easy (typed events) |

**Key Improvements**:
1. Type safety catches bugs at dev time (mypy), not runtime
2. Metrics provide observability (dropped events are now visible)
3. Bounded queue prevents OOM from slow handlers
4. Handler exceptions don't affect publishers or other handlers

---

## Usage Examples

### Example 1: Define an Event

```python
from dataclasses import dataclass
from src.core.messaging.events import Event

@dataclass
class AgentStarted(Event):
    """Published when agent begins execution."""
    session_id: str
    role: str
    task: str
```

### Example 2: Publish an Event

```python
from src.core.messaging.bus import MessageBus

bus = MessageBus()

bus.publish(AgentStarted(
    session_id="sess_123",
    role="operational",
    task="Fix the login bug"
))
```

### Example 3: Subscribe to Events

```python
class TUIEventHandler:
    def handle(self, event: Event) -> None:
        if isinstance(event, AgentStarted):
            print(f"Agent started: {event.task}")

bus = MessageBus()
handler = TUIEventHandler()
bus.subscribe(AgentStarted, handler)
```

### Example 4: Check Metrics

```python
metrics = bus.get_metrics()
print(f"Published: {metrics['published']}")
print(f"Dropped: {metrics['dropped']}")
print(f"P99 latency: {metrics['p99_delivery_ms']}")
```

### Example 5: Graceful Shutdown

```python
# Publish events
for i in range(100):
    bus.publish(AgentStarted(...))

# Shutdown waits for all events to be delivered
bus.shutdown(timeout=5.0)
```

---

## Testing Strategy

### Unit Tests

**Coverage**: 90%+ (Phase 1 target)

**Key Test Cases**:
- Event serialization/deserialization
- Handler registration/unregistration
- Event delivery to subscribed handlers
- Handler exception isolation
- Queue overflow handling
- Metrics accuracy
- Graceful shutdown

**Location**: `tests/unit/messaging/`

### Integration Tests

**Scope**: Multi-component event flows

**Key Test Cases** (Phase 3+):
- Agent execution emits expected events
- TUI stays synchronized under load
- Event correlation IDs flow through chains

**Location**: `tests/integration_real/`

---

## Migration Path

**Current Status**: Phase 1 Complete (Foundation)

**Next Steps**:
- **Phase 2**: Define 20-30 typed event classes
- **Phase 3**: Build TUIEventHandler to replace 6 bridge mixins
- **Phase 4**: Migrate all 63 publish sites to typed events
- **Phase 5**: Remove old EventBus, final rollout

**See**: `docs/plans/MESSAGE_BUS_UNIFICATION_PLAN.md` for full migration plan

---

## Operational Considerations

### Monitoring

**Key Metrics to Alert On**:
1. `dropped > 0`: Queue is full, handlers are too slow
2. `handler_failed > 10/min`: Handlers are crashing
3. `p99_delivery_ms > 100ms`: Handlers are slow

### Troubleshooting

**Problem**: Queue filling up (dropped events)

**Diagnosis**:
1. Check `p99_delivery_ms` - are handlers slow?
2. Check event publish rate - too many events?

**Solutions**:
- Increase `max_queue_size` (temporary)
- Optimize slow handlers (offload work to threads)
- Add backpressure at source (rate limit events)

**Problem**: Handler failures increasing

**Diagnosis**:
1. Check logs for handler exceptions
2. Identify which handler is failing

**Solutions**:
- Fix handler bug
- Add defensive error handling in handler
- Temporarily unsubscribe broken handler

---

## Future Enhancements

### Potential Improvements

1. **Event Persistence**: Durable queue for critical events
2. **Event Replay**: Replay events for debugging
3. **Event Filtering**: Subscribe with predicate (e.g., only errors)
4. **Priority Queue**: High-priority events jump queue
5. **Dead Letter Queue**: Failed events go to DLQ for retry
6. **Metrics Export**: Export metrics to Prometheus/Grafana

**Decision**: Keep it simple for now, add complexity only when needed

---

## References

- **Implementation**: `src/core/messaging/`
- **Tests**: `tests/unit/messaging/`
- **Migration Plan**: `docs/plans/MESSAGE_BUS_UNIFICATION_PLAN.md`
- **Event Catalog**: `docs/EVENT_CATALOG.md` (Phase 2)

---

**Document Version**: 1.0  
**Last Updated**: 2026-06-08  
**Author**: MessageBus Implementation Team
