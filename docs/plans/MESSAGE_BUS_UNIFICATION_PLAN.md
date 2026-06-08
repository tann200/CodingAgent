# Message Bus Unification - Implementation Plan

**Project**: Unify Event Bus & TUI Bridge with Typed Message System  
**Duration**: 10 working days (2 weeks calendar)  
**Status**: Planning → Ready for Implementation  
**Owner**: TBD  
**Started**: TBD  
**Completed**: TBD

---

## Executive Summary

Replace the current untyped, fragile event bus system with a typed message bus that provides:
- Type-safe event delivery
- Error isolation (failed handlers don't kill publishers)
- Delivery guarantees with explicit drop logging
- Consolidated TUI bridge (6 files → 1)
- Elimination of 111 bare `except Exception` handlers in TUI

**Impact**: Fixes FRAG-3, FRAG-8, FRAG-9 fragility patterns and user-visible UI desync issues.

---

## Table of Contents

1. [Background & Motivation](#background--motivation)
2. [Architecture Design](#architecture-design)
3. [Implementation Phases](#implementation-phases)
4. [Quality Gates](#quality-gates)
5. [Testing Strategy](#testing-strategy)
6. [Documentation Requirements](#documentation-requirements)
7. [Rollout Plan](#rollout-plan)
8. [Risk Mitigation](#risk-mitigation)
9. [Success Metrics](#success-metrics)
10. [Rollback Plan](#rollback-plan)

---

## Background & Motivation

### Current Problems

1. **Silent Failures Everywhere**
   - 111 bare `except Exception: pass` handlers in TUI
   - Failed event callbacks logged at DEBUG level only
   - No delivery guarantees - events can vanish silently
   - Example: Tool execution completes but UI shows "running..." forever

2. **No Type Safety**
   - Events are `Dict[str, Any]` - no validation
   - Subscribers guess payload structure
   - Refactoring breaks at runtime, not compile time
   - Example: `payload.get("tool_name")` - what if it's `"toolName"`?

3. **Lambda Capture Bugs**
   - `lambda p: self.app.handle(p)` where `self.app` can be `None` during shutdown
   - Fixed in FRAG-8, but pattern persists in other callbacks
   - Causes `AttributeError: 'NoneType' has no attribute 'post_message'`

4. **TUI Bridge Complexity**
   - 6 separate mixin files (5,488 LOC total):
     - `_bridge_agent.py` (981 LOC)
     - `_bridge_tools.py` (847 LOC)
     - `_bridge_context.py` (623 LOC)
     - `_bridge_session.py` (891 LOC)
     - `_bridge_provider.py` (567 LOC)
     - `_bridge_subscriptions.py` (1,579 LOC)
   - Responsibilities scattered across mixins
   - Hard to follow event flow

5. **Inconsistent Event Payloads**
   - 63 publish sites with different payload shapes
   - Sometimes `dict`, sometimes `dataclass`, sometimes primitives
   - No schema validation

### User Impact

- **UI gets out of sync**: Tool running but UI shows idle
- **Lost updates**: File changes don't refresh file tree
- **Silent errors**: Permission denied, but no error shown
- **Need to restart TUI**: Only way to recover from desync

### Fragility Patterns Fixed

- **FRAG-3**: Event bus silent failure (DEBUG logging only)
- **FRAG-8**: Lambda capture `self.app` during shutdown
- **FRAG-9**: Session manager event publish silent drop
- **FRAG-15**: StreamView lambda capture race condition

---

## Architecture Design

### Overview

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

### Core Components

#### 1. Event Base Class

```python
@dataclass
class Event:
    """Base class for all typed events."""
    
    # Automatically set by MessageBus
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize for logging/debugging."""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Event":
        """Deserialize from dict."""
        return cls(**data)
```

#### 2. Event Types (20-30 total)

**Agent Lifecycle Events:**
```python
@dataclass
class AgentStarted(Event):
    session_id: str
    role: str
    task: str

@dataclass
class AgentResponse(Event):
    session_id: str
    content: str
    usage: Dict[str, int]
    
@dataclass
class AgentCompleted(Event):
    session_id: str
    result: Dict[str, Any]

@dataclass
class AgentError(Event):
    session_id: str
    error: str
    traceback: str
```

**Tool Events:**
```python
@dataclass
class ToolCallStarted(Event):
    tool_name: str
    args: Dict[str, Any]
    tool_call_id: str

@dataclass
class ToolCallFinished(Event):
    tool_name: str
    tool_call_id: str
    result: Dict[str, Any]
    ok: bool
    duration_ms: float
```

**Session Events:**
```python
@dataclass
class SessionCreated(Event):
    session_id: str
    working_dir: str

@dataclass
class SessionStateUpdated(Event):
    session_id: str
    state_changes: Dict[str, Any]

@dataclass
class FilesChanged(Event):
    session_id: str
    files: List[str]
    change_type: Literal["created", "modified", "deleted"]
```

**Provider Events:**
```python
@dataclass
class ProviderModelListUpdated(Event):
    provider: str
    models: List[str]

@dataclass
class ProviderConnected(Event):
    provider: str
    
@dataclass
class ProviderDisconnected(Event):
    provider: str
    reason: str
```

#### 3. MessageBus Implementation

```python
class MessageBus:
    """
    Asynchronous message bus with typed events.
    
    Features:
    - Type-safe event delivery
    - Bounded queue with backpressure
    - Error isolation (failed handlers don't kill bus)
    - Delivery guarantees with explicit drop logging
    - Metrics for observability
    """
    
    def __init__(
        self,
        max_queue_size: int = 1000,
        worker_threads: int = 1,
        enable_metrics: bool = True
    ):
        self._queue: Queue[Event] = Queue(maxsize=max_queue_size)
        self._handlers: Dict[Type[Event], List[EventHandler]] = {}
        self._lock = threading.RLock()
        self._shutdown = threading.Event()
        self._workers: List[Thread] = []
        self._enable_metrics = enable_metrics
        
        # Metrics
        self._metrics = MessageBusMetrics()
        
        # Start worker threads
        for i in range(worker_threads):
            worker = Thread(
                target=self._process_events,
                name=f"MessageBus-Worker-{i}",
                daemon=True
            )
            worker.start()
            self._workers.append(worker)
    
    def subscribe(self, event_type: Type[Event], handler: EventHandler) -> None:
        """
        Register a handler for specific event type.
        
        Args:
            event_type: The Event class to subscribe to
            handler: Callable that accepts event instance
        """
        with self._lock:
            self._handlers.setdefault(event_type, []).append(handler)
            logger.debug(
                "MessageBus: subscribed %s to %s",
                handler.__class__.__name__,
                event_type.__name__
            )
    
    def unsubscribe(self, event_type: Type[Event], handler: EventHandler) -> None:
        """Remove handler subscription."""
        with self._lock:
            if event_type in self._handlers:
                try:
                    self._handlers[event_type].remove(handler)
                except ValueError:
                    pass
    
    def publish(self, event: Event) -> None:
        """
        Publish event for async delivery.
        
        Events are queued and delivered by worker thread.
        If queue is full, event is dropped and logged.
        """
        if self._shutdown.is_set():
            logger.warning("MessageBus: publish after shutdown, dropping: %s", event)
            return
        
        try:
            self._queue.put_nowait(event)
            if self._enable_metrics:
                self._metrics.increment_published(type(event).__name__)
        except Full:
            logger.error(
                "MessageBus: queue full (%d events), dropping: %s",
                self._queue.qsize(),
                event
            )
            if self._enable_metrics:
                self._metrics.increment_dropped(type(event).__name__)
    
    def publish_sync(self, event: Event, timeout: float = 5.0) -> bool:
        """
        Publish event and wait for delivery (blocking).
        
        Returns:
            True if delivered, False if timeout or dropped
        """
        delivered = threading.Event()
        
        def wrapped_handlers():
            try:
                self._deliver_to_handlers(event)
            finally:
                delivered.set()
        
        try:
            self._queue.put(wrapped_handlers, timeout=timeout)
            return delivered.wait(timeout=timeout)
        except Full:
            logger.error("MessageBus: sync publish queue full")
            return False
    
    def _process_events(self) -> None:
        """Worker thread: dequeue and deliver events."""
        logger.info("MessageBus worker started: %s", threading.current_thread().name)
        
        while not self._shutdown.is_set():
            try:
                # Block with timeout to check shutdown flag
                event = self._queue.get(timeout=0.5)
            except Empty:
                continue
            
            try:
                self._deliver_to_handlers(event)
            except Exception as e:
                logger.error(
                    "MessageBus: unexpected error delivering %s: %s",
                    type(event).__name__,
                    e,
                    exc_info=True
                )
            finally:
                self._queue.task_done()
        
        logger.info("MessageBus worker stopped: %s", threading.current_thread().name)
    
    def _deliver_to_handlers(self, event: Event) -> None:
        """Deliver event to all registered handlers."""
        event_type = type(event)
        
        with self._lock:
            handlers = self._handlers.get(event_type, []).copy()
        
        if not handlers:
            logger.debug("MessageBus: no handlers for %s", event_type.__name__)
            return
        
        for handler in handlers:
            try:
                start = time.perf_counter()
                handler.handle(event)
                duration_ms = (time.perf_counter() - start) * 1000
                
                if self._enable_metrics:
                    self._metrics.record_delivery(
                        event_type.__name__,
                        handler.__class__.__name__,
                        duration_ms
                    )
                
                logger.debug(
                    "MessageBus: delivered %s to %s (%.2fms)",
                    event_type.__name__,
                    handler.__class__.__name__,
                    duration_ms
                )
                
            except Exception as e:
                logger.warning(
                    "MessageBus: handler %s failed for %s: %s",
                    handler.__class__.__name__,
                    event_type.__name__,
                    e,
                    exc_info=True
                )
                if self._enable_metrics:
                    self._metrics.increment_handler_failed(
                        event_type.__name__,
                        handler.__class__.__name__
                    )
    
    def shutdown(self, timeout: float = 10.0) -> None:
        """
        Gracefully shutdown message bus.
        
        Waits for queue to drain, then stops workers.
        """
        logger.info("MessageBus: shutting down...")
        self._shutdown.set()
        
        # Wait for queue to drain
        try:
            self._queue.join()
        except:
            pass
        
        # Wait for workers to finish
        for worker in self._workers:
            worker.join(timeout=timeout / len(self._workers))
        
        logger.info("MessageBus: shutdown complete")
    
    def get_metrics(self) -> Dict[str, Any]:
        """Return current metrics snapshot."""
        return self._metrics.snapshot()
```

#### 4. EventHandler Protocol

```python
class EventHandler(Protocol):
    """Interface for event handlers."""
    
    def handle(self, event: Event) -> None:
        """
        Handle incoming event.
        
        Implementations should:
        - Not raise exceptions (catch and log)
        - Return quickly (offload heavy work to threads)
        - Be idempotent (may receive duplicates)
        """
        ...
```

#### 5. TUIEventHandler (Unified Bridge)

```python
class TUIEventHandler:
    """
    Unified TUI event handler.
    
    Replaces 6 separate bridge mixins:
    - _bridge_agent.py
    - _bridge_tools.py
    - _bridge_context.py
    - _bridge_session.py
    - _bridge_provider.py
    - _bridge_subscriptions.py
    """
    
    def __init__(self, app: "CodingAgentApp"):
        self._app = app
        self._logger = logging.getLogger(__name__)
    
    def setup_subscriptions(self, bus: MessageBus) -> None:
        """Register all TUI event subscriptions."""
        # Agent events
        bus.subscribe(AgentStarted, self)
        bus.subscribe(AgentResponse, self)
        bus.subscribe(AgentCompleted, self)
        bus.subscribe(AgentError, self)
        
        # Tool events
        bus.subscribe(ToolCallStarted, self)
        bus.subscribe(ToolCallFinished, self)
        
        # Session events
        bus.subscribe(SessionCreated, self)
        bus.subscribe(SessionStateUpdated, self)
        bus.subscribe(FilesChanged, self)
        
        # Provider events
        bus.subscribe(ProviderModelListUpdated, self)
        bus.subscribe(ProviderConnected, self)
        bus.subscribe(ProviderDisconnected, self)
    
    def handle(self, event: Event) -> None:
        """Dispatch event to appropriate handler method."""
        handler_name = f"_on_{type(event).__name__.lower()}"
        handler = getattr(self, handler_name, None)
        
        if handler is None:
            self._logger.warning("No handler for event: %s", type(event).__name__)
            return
        
        try:
            handler(event)
        except Exception as e:
            self._logger.error(
                "TUIEventHandler.%s failed: %s",
                handler_name,
                e,
                exc_info=True
            )
    
    # Agent event handlers
    
    def _on_agentstarted(self, event: AgentStarted) -> None:
        """Handle AgentStarted event."""
        self._app.call_from_thread(
            self._update_agent_status,
            event.session_id,
            "running",
            event.task
        )
    
    def _on_agentresponse(self, event: AgentResponse) -> None:
        """Handle AgentResponse event."""
        self._app.call_from_thread(
            self._append_message,
            event.session_id,
            "assistant",
            event.content,
            event.usage
        )
    
    def _on_agentcompleted(self, event: AgentCompleted) -> None:
        """Handle AgentCompleted event."""
        self._app.call_from_thread(
            self._update_agent_status,
            event.session_id,
            "completed",
            None
        )
    
    def _on_agenterror(self, event: AgentError) -> None:
        """Handle AgentError event."""
        self._app.call_from_thread(
            self._show_error,
            event.error,
            event.traceback
        )
    
    # Tool event handlers
    
    def _on_toolcallstarted(self, event: ToolCallStarted) -> None:
        """Handle ToolCallStarted event."""
        self._app.call_from_thread(
            self._add_tool_widget,
            event.tool_call_id,
            event.tool_name,
            event.args
        )
    
    def _on_toolcallfinished(self, event: ToolCallFinished) -> None:
        """Handle ToolCallFinished event."""
        self._app.call_from_thread(
            self._update_tool_widget,
            event.tool_call_id,
            event.result,
            event.ok,
            event.duration_ms
        )
    
    # Session event handlers
    
    def _on_sessioncreated(self, event: SessionCreated) -> None:
        """Handle SessionCreated event."""
        self._app.call_from_thread(
            self._add_session_tab,
            event.session_id,
            event.working_dir
        )
    
    def _on_sessionstateupdated(self, event: SessionStateUpdated) -> None:
        """Handle SessionStateUpdated event."""
        self._app.call_from_thread(
            self._refresh_session_state,
            event.session_id,
            event.state_changes
        )
    
    def _on_fileschanged(self, event: FilesChanged) -> None:
        """Handle FilesChanged event."""
        self._app.call_from_thread(
            self._refresh_file_tree,
            event.files,
            event.change_type
        )
    
    # Provider event handlers
    
    def _on_providermodellistupdated(self, event: ProviderModelListUpdated) -> None:
        """Handle ProviderModelListUpdated event."""
        self._app.call_from_thread(
            self._update_model_list,
            event.provider,
            event.models
        )
    
    def _on_providerconnected(self, event: ProviderConnected) -> None:
        """Handle ProviderConnected event."""
        self._app.call_from_thread(
            self._show_provider_status,
            event.provider,
            "connected"
        )
    
    def _on_providerdisconnected(self, event: ProviderDisconnected) -> None:
        """Handle ProviderDisconnected event."""
        self._app.call_from_thread(
            self._show_provider_status,
            event.provider,
            "disconnected",
            event.reason
        )
    
    # UI update methods (called via call_from_thread)
    
    def _update_agent_status(self, session_id: str, status: str, task: str | None):
        """Update agent status in UI (runs in main thread)."""
        # Implementation...
    
    def _append_message(self, session_id: str, role: str, content: str, usage: Dict):
        """Append message to chat (runs in main thread)."""
        # Implementation...
    
    # ... other UI update methods
```

#### 6. Metrics Collection

```python
@dataclass
class MessageBusMetrics:
    """Metrics for message bus observability."""
    
    published: Counter = field(default_factory=Counter)
    delivered: Counter = field(default_factory=Counter)
    dropped: Counter = field(default_factory=Counter)
    handler_failed: Counter = field(default_factory=Counter)
    delivery_duration_ms: Dict[str, List[float]] = field(default_factory=dict)
    
    def increment_published(self, event_type: str):
        self.published[event_type] += 1
    
    def increment_dropped(self, event_type: str):
        self.dropped[event_type] += 1
    
    def increment_handler_failed(self, event_type: str, handler: str):
        key = f"{event_type}:{handler}"
        self.handler_failed[key] += 1
    
    def record_delivery(self, event_type: str, handler: str, duration_ms: float):
        self.delivered[event_type] += 1
        key = f"{event_type}:{handler}"
        self.delivery_duration_ms.setdefault(key, []).append(duration_ms)
    
    def snapshot(self) -> Dict[str, Any]:
        """Return metrics snapshot for observability."""
        return {
            "published": dict(self.published),
            "delivered": dict(self.delivered),
            "dropped": dict(self.dropped),
            "handler_failed": dict(self.handler_failed),
            "p50_delivery_ms": {
                k: statistics.median(v) 
                for k, v in self.delivery_duration_ms.items()
            },
            "p99_delivery_ms": {
                k: statistics.quantiles(v, n=100)[98] if len(v) >= 100 else max(v)
                for k, v in self.delivery_duration_ms.items()
            },
        }
```

---

## Implementation Phases

### Phase 1: Foundation (Days 1-3)

**Goal**: Build core MessageBus infrastructure

**Tasks**:
1. ✅ Create `src/core/messaging/` package
2. ✅ Implement `Event` base class
3. ✅ Implement `MessageBus` class
4. ✅ Implement `EventHandler` protocol
5. ✅ Implement `MessageBusMetrics`
6. ✅ Write unit tests (90%+ coverage)
7. ✅ Update documentation

**Deliverables**:
- `src/core/messaging/events.py` - Event base class
- `src/core/messaging/bus.py` - MessageBus implementation
- `src/core/messaging/metrics.py` - Metrics collection
- `tests/unit/messaging/test_message_bus.py` - Unit tests
- `docs/architecture/MESSAGE_BUS.md` - Architecture doc

**Quality Gate 1**:
- [ ] All unit tests pass (90%+ coverage)
- [ ] MessageBus handles 1000 events/sec in benchmark
- [ ] Failed handlers don't kill worker thread
- [ ] Queue overflow logs ERROR and increments dropped metric
- [ ] Architecture doc reviewed and approved

### Phase 2: Event Definitions (Days 4-5)

**Goal**: Define all typed event classes

**Tasks**:
1. ✅ Audit 63 existing publish sites
2. ✅ Define 20-30 typed Event classes
3. ✅ Add serialization/deserialization methods
4. ✅ Write validation logic
5. ✅ Create migration guide for publishers
6. ✅ Update documentation

**Deliverables**:
- `src/core/messaging/event_types.py` - All event definitions
- `docs/guides/EVENT_MIGRATION.md` - Migration guide
- `tests/unit/messaging/test_event_types.py` - Event validation tests

**Quality Gate 2**:
- [ ] All 20-30 event types defined
- [ ] Each event has docstring with example usage
- [ ] Serialization round-trip tests pass
- [ ] Migration guide reviewed
- [ ] Event catalog generated (event → publishers/subscribers)

### Phase 3: TUI Bridge Consolidation (Days 6-7)

**Goal**: Replace 6 TUI bridge mixins with unified handler

**Tasks**:
1. ✅ Create `TUIEventHandler` class
2. ✅ Migrate `_bridge_agent.py` logic
3. ✅ Migrate `_bridge_tools.py` logic
4. ✅ Migrate `_bridge_session.py` logic
5. ✅ Migrate `_bridge_provider.py` logic
6. ✅ Migrate `_bridge_context.py` logic
7. ✅ Remove lambda captures (fix FRAG-8)
8. ✅ Add error handling (fix FRAG-3, FRAG-9)
9. ✅ Write integration tests
10. ✅ Update documentation

**Deliverables**:
- `tui/src/ui/event_handler.py` - Unified TUIEventHandler
- `tests/integration_real/test_tui_event_handling.py` - Integration tests
- `docs/architecture/TUI_EVENT_HANDLING.md` - TUI event handling doc

**Quality Gate 3**:
- [ ] All 6 bridge mixins replaced by TUIEventHandler
- [ ] Zero lambda captures in event subscriptions
- [ ] Zero bare `except Exception: pass` in event handling
- [ ] Integration tests verify UI updates for all events
- [ ] TUI stays synchronized under 100 events/sec load

### Phase 4: Publisher Migration (Days 8-9)

**Goal**: Migrate all 63 publish sites to typed events

**Tasks**:
1. ✅ Migrate orchestrator publish sites (20+ sites)
2. ✅ Migrate session manager publish sites (15+ sites)
3. ✅ Migrate tool execution publish sites (10+ sites)
4. ✅ Migrate provider manager publish sites (8+ sites)
5. ✅ Migrate remaining publish sites (10+ sites)
6. ✅ Add correlation IDs to event chains
7. ✅ Update integration tests
8. ✅ Run dual-bus validation (old + new in parallel)

**Deliverables**:
- All 63 publish sites migrated to typed events
- Dual-bus validation results (event count comparison)
- Updated integration tests

**Quality Gate 4**:
- [ ] All 63 publish sites migrated
- [ ] Dual-bus validation: old_count == new_count ± 5%
- [ ] No regressions in integration tests
- [ ] Correlation IDs flow through event chains
- [ ] Metrics dashboard shows 100% typed event usage

### Phase 5: Cleanup & Rollout (Day 10)

**Goal**: Remove old EventBus, finalize migration

**Tasks**:
1. ✅ Remove old `EventBus` class
2. ✅ Remove `_bridge_*.py` mixin files
3. ✅ Update all imports
4. ✅ Run full test suite
5. ✅ Update all documentation
6. ✅ Create rollout announcement
7. ✅ Monitor metrics for 1 day

**Deliverables**:
- Old EventBus removed
- 6 TUI bridge mixins deleted (5,488 LOC reduction)
- All documentation updated
- Rollout announcement
- Metrics dashboard

**Quality Gate 5**:
- [ ] All 4,388 tests pass
- [ ] Zero references to old EventBus
- [ ] TUI bridge LOC reduced by 80% (5,488 → ~1,000)
- [ ] Documentation fully updated
- [ ] Metrics show zero dropped events in production
- [ ] User acceptance testing passes

---

## Quality Gates

### Gate 1: Foundation Complete ✅

**Criteria**:
- [ ] MessageBus unit tests: 90%+ coverage
- [ ] Benchmark: 1000 events/sec throughput
- [ ] Worker thread isolation: failed handler doesn't kill worker
- [ ] Backpressure: queue full logs ERROR and increments metric
- [ ] Architecture doc: reviewed by 2+ engineers

**Evidence Required**:
- Test coverage report: `pytest --cov=src.core.messaging --cov-report=html`
- Benchmark results: `python benchmarks/message_bus_benchmark.py`
- Code review: 2+ approvals on PR
- Doc review: Approval from tech lead

**Exit Criteria**:
All checkboxes checked, evidence collected, PR merged.

---

### Gate 2: Event Definitions Complete ✅

**Criteria**:
- [ ] 20-30 typed events defined
- [ ] Each event has docstring + example
- [ ] Serialization tests: 100% pass
- [ ] Migration guide: reviewed and approved
- [ ] Event catalog: auto-generated from code

**Evidence Required**:
- Event catalog: `scripts/generate_event_catalog.py > docs/EVENT_CATALOG.md`
- Migration guide review: Approval from tech lead
- Test results: `pytest tests/unit/messaging/test_event_types.py -v`

**Exit Criteria**:
All events defined, documented, tested. Migration guide approved.

---

### Gate 3: TUI Bridge Consolidated ✅

**Criteria**:
- [ ] TUIEventHandler replaces all 6 mixins
- [ ] Zero lambda captures in subscriptions
- [ ] Zero bare `except Exception: pass`
- [ ] Integration tests: UI updates for all events
- [ ] Load test: UI stable under 100 events/sec

**Evidence Required**:
- Code diff: Show 6 mixin files deleted
- Lambda audit: `rg "lambda.*self\.app" tui/src/ui/` returns 0 results
- Exception audit: `rg "except Exception:\s*pass" tui/src/ui/` returns 0 results
- Load test: `pytest tests/integration_real/test_tui_load.py -v`

**Exit Criteria**:
TUI bridge consolidated, fragility patterns eliminated, tests pass.

---

### Gate 4: Publishers Migrated ✅

**Criteria**:
- [ ] All 63 publish sites migrated
- [ ] Dual-bus validation: counts match ± 5%
- [ ] Integration tests: no regressions
- [ ] Correlation IDs: flow through chains
- [ ] Metrics: 100% typed events

**Evidence Required**:
- Publisher audit: `scripts/audit_publishers.py`
- Dual-bus report: Event count comparison over 1 hour
- Test results: `pytest tests/integration/ -v`
- Metrics dashboard: Screenshot showing 100% typed events

**Exit Criteria**:
All publishers migrated, validation passed, metrics clean.

---

### Gate 5: Rollout Complete ✅

**Criteria**:
- [ ] All 4,388 tests pass
- [ ] Old EventBus removed
- [ ] TUI bridge: 5,488 → ~1,000 LOC (80% reduction)
- [ ] Documentation: 100% updated
- [ ] Production metrics: Zero dropped events
- [ ] User acceptance: TUI stable for 24 hours

**Evidence Required**:
- Test results: `pytest tests/ --ignore=tests/e2e/test_crud_lm_studio.py`
- LOC diff: Git diff showing file deletions
- Doc audit: All EVENT_BUS references updated
- Metrics: 24-hour dashboard showing zero drops
- User feedback: No desync issues reported

**Exit Criteria**:
Migration complete, old code removed, production stable.

---

## Testing Strategy

### Unit Tests

**Coverage Target**: 90%+

**Key Test Cases**:

1. **MessageBus Core**
   - `test_publish_delivers_to_handler`: Verify event delivery
   - `test_queue_full_drops_event`: Verify backpressure handling
   - `test_handler_exception_isolated`: Failed handler doesn't kill worker
   - `test_multiple_handlers`: Multiple subscribers get same event
   - `test_unsubscribe_stops_delivery`: Unsubscribe works
   - `test_shutdown_drains_queue`: Graceful shutdown
   - `test_metrics_accurate`: Metrics match actual delivery

2. **Event Types**
   - `test_event_serialization`: Round-trip to/from dict
   - `test_event_validation`: Invalid data raises error
   - `test_correlation_id_auto_generated`: Auto-generated if not provided
   - `test_timestamp_auto_set`: Auto-set if not provided

3. **TUIEventHandler**
   - `test_agent_started_updates_ui`: Verify UI update called
   - `test_tool_started_creates_widget`: Verify widget created
   - `test_handler_exception_logged`: Exception doesn't kill handler
   - `test_no_handler_logs_warning`: Missing handler logs warning

### Integration Tests

**Coverage Target**: All critical event flows

**Key Test Cases**:

1. **End-to-End Event Flow**
   ```python
   def test_agent_run_emits_expected_events(tmp_path):
       """Verify agent run emits AgentStarted → ToolCallStarted → AgentCompleted."""
       bus = MessageBus()
       events_received = []
       
       class Collector:
           def handle(self, event):
               events_received.append(event)
       
       collector = Collector()
       bus.subscribe(AgentStarted, collector)
       bus.subscribe(ToolCallStarted, collector)
       bus.subscribe(AgentCompleted, collector)
       
       orch = Orchestrator(working_dir=str(tmp_path), message_bus=bus)
       orch.run_agent_once(messages=[{"role": "user", "content": "Hello"}])
       
       # Give async delivery time to complete
       time.sleep(0.5)
       
       assert len(events_received) >= 3
       assert isinstance(events_received[0], AgentStarted)
       assert isinstance(events_received[-1], AgentCompleted)
   ```

2. **TUI Synchronization**
   ```python
   def test_tui_stays_synchronized_under_load(tmp_path):
       """Verify TUI receives all events under 100 events/sec load."""
       app = create_test_app(tmp_path)
       bus = MessageBus()
       handler = TUIEventHandler(app)
       handler.setup_subscriptions(bus)
       
       # Publish 1000 events over 10 seconds (100/sec)
       for i in range(1000):
           bus.publish(ToolCallStarted(
               tool_name=f"tool_{i}",
               args={},
               tool_call_id=f"id_{i}"
           ))
           time.sleep(0.01)
       
       # Wait for delivery
       time.sleep(2.0)
       
       # Verify all events processed
       metrics = bus.get_metrics()
       assert metrics["published"]["ToolCallStarted"] == 1000
       assert metrics["delivered"]["ToolCallStarted"] == 1000
       assert metrics["dropped"].get("ToolCallStarted", 0) == 0
   ```

3. **Dual-Bus Validation**
   ```python
   def test_dual_bus_event_parity():
       """Verify old EventBus and new MessageBus receive same events."""
       old_bus = EventBus()
       new_bus = MessageBus()
       
       old_events = []
       new_events = []
       
       old_bus.subscribe("tool.call.start", lambda p: old_events.append(p))
       new_bus.subscribe(ToolCallStarted, lambda e: new_events.append(e))
       
       # Run agent with both buses
       orch = Orchestrator(
           event_bus=old_bus,
           message_bus=new_bus,
           dual_bus_mode=True
       )
       orch.execute_tool({"name": "read_file", "arguments": {"path": "test.txt"}})
       
       time.sleep(0.5)
       
       # Should receive same number of events
       assert len(old_events) == len(new_events)
   ```

### Load Tests

**Goal**: Verify MessageBus handles production load

**Scenarios**:

1. **Sustained Load**: 100 events/sec for 1 hour
2. **Burst Load**: 1000 events in 1 second, then idle
3. **Handler Slow**: Handler takes 100ms, verify queue doesn't fill
4. **Handler Failure**: 50% handlers fail, verify others still process

**Success Criteria**:
- Queue never fills (dropped events = 0)
- p99 delivery latency < 100ms
- Worker threads stay alive
- Memory usage stable (no leaks)

### Regression Tests

**Goal**: Ensure no existing functionality breaks

**Approach**:
1. Run full test suite before migration
2. Capture baseline: 4,388 tests pass
3. Run full test suite after each phase
4. Compare: Same tests pass, no new failures

---

## Documentation Requirements

### 1. Architecture Documentation

**File**: `docs/architecture/MESSAGE_BUS.md`

**Contents**:
- System overview and motivation
- Architecture diagrams
- Event flow diagrams
- Threading model
- Error handling strategy
- Metrics and observability
- Comparison with old EventBus

**Audience**: Engineers, architects

**Must Include**:
- Sequence diagrams for key flows
- Example code snippets
- Performance characteristics
- Trade-offs and design decisions

---

### 2. Migration Guide

**File**: `docs/guides/EVENT_MIGRATION.md`

**Contents**:
- Step-by-step migration process
- Before/after code examples
- Common pitfalls
- Testing strategy
- Rollback procedure

**Audience**: Developers migrating code

**Must Include**:
- Search/replace patterns for common migrations
- Examples for each of 63 publish sites
- How to test migrated code
- Troubleshooting guide

**Example Section**:
```markdown
## Migrating a Publisher

### Before
```python
try:
    self.event_bus.publish("tool.call.start", {
        "tool_name": name,
        "args": args
    })
except Exception:
    pass  # Silent failure!
```

### After
```python
self.message_bus.publish(ToolCallStarted(
    tool_name=name,
    args=args
))
# No exception handling needed - MessageBus handles it
```

### Testing
```python
def test_my_code_publishes_event():
    bus = MessageBus()
    events = []
    bus.subscribe(ToolCallStarted, lambda e: events.append(e))
    
    my_function(bus)
    
    assert len(events) == 1
    assert events[0].tool_name == "expected_tool"
```
```

---

### 3. API Reference

**File**: `docs/api/MESSAGE_BUS_API.md`

**Contents**:
- `MessageBus` class API
- `Event` base class API
- All event type schemas
- `EventHandler` protocol
- Metrics API

**Audience**: Developers using MessageBus

**Format**: Auto-generated from docstrings

**Generation Command**:
```bash
python scripts/generate_api_docs.py \
  --module src.core.messaging \
  --output docs/api/MESSAGE_BUS_API.md
```

---

### 4. Event Catalog

**File**: `docs/EVENT_CATALOG.md`

**Contents**:
- List of all 20-30 event types
- For each event:
  - Description
  - Schema (fields + types)
  - Example payload
  - Publishers (who sends it)
  - Subscribers (who receives it)

**Audience**: All engineers

**Format**: Auto-generated from code

**Generation Command**:
```bash
python scripts/generate_event_catalog.py > docs/EVENT_CATALOG.md
```

**Example Entry**:
```markdown
### ToolCallStarted

**Description**: Published when a tool execution begins.

**Schema**:
```python
@dataclass
class ToolCallStarted(Event):
    tool_name: str          # Name of tool being executed
    args: Dict[str, Any]    # Tool arguments
    tool_call_id: str       # Unique ID for this call
```

**Example**:
```json
{
  "tool_name": "read_file",
  "args": {"path": "config.json"},
  "tool_call_id": "call_abc123",
  "correlation_id": "uuid-...",
  "timestamp": 1234567890.123
}
```

**Publishers**: 
- `ToolExecutor` (src/core/orchestration/tool_executor.py:156)

**Subscribers**:
- `TUIEventHandler` (tui/src/ui/event_handler.py:89)
- `AuditLogger` (src/core/audit/logger.py:45)
```

---

### 5. Runbook

**File**: `docs/runbooks/MESSAGE_BUS_OPERATIONS.md`

**Contents**:
- How to monitor MessageBus health
- Key metrics and alerts
- Troubleshooting common issues
- How to enable debug logging
- How to inspect queue state

**Audience**: SREs, support engineers

**Must Include**:
- Metric dashboard screenshots
- Alert thresholds
- Escalation procedures
- Known issues and workarounds

**Example Troubleshooting**:
```markdown
## Issue: Queue Filling Up

**Symptoms**:
- Metric `message_bus.dropped` increasing
- Log messages: "MessageBus: queue full"

**Diagnosis**:
1. Check handler duration: `message_bus.p99_delivery_ms`
   - If > 100ms, handlers are too slow
2. Check event rate: `message_bus.published`
   - If > 1000/sec, excessive publishing

**Resolution**:
- **Slow handlers**: Offload work to background threads
- **High rate**: Add backpressure at source, increase queue size
- **Emergency**: Restart MessageBus worker threads
```

---

### 6. CHANGELOG Entry

**File**: `CHANGELOG.md`

**Entry**:
```markdown
## [Unreleased]

### Changed
- **BREAKING**: Replaced EventBus with typed MessageBus
  - Old `event_bus.publish(event_name, dict)` → `message_bus.publish(Event())`
  - See EVENT_MIGRATION.md for migration guide
  - All 63 publish sites migrated to typed events
  - TUI bridge consolidated from 6 mixins to 1 handler (5,488 → ~1,000 LOC)

### Fixed
- FRAG-3: Event bus silent failures (DEBUG logging only)
- FRAG-8: Lambda capture bugs in TUI subscriptions
- FRAG-9: Session manager event publish silent drop
- FRAG-15: StreamView lambda capture race condition
- 111 bare `except Exception: pass` handlers in TUI removed

### Added
- MessageBus with typed event delivery
- 20-30 typed Event classes for all event types
- MessageBus metrics for observability
- Event correlation IDs for distributed tracing
- TUIEventHandler unified event handler
```

---

## Rollout Plan

### Pre-Rollout (1 week before)

**Communication**:
1. Send announcement email to engineering team
2. Post in #engineering Slack channel
3. Schedule migration workshop (1 hour)
4. Update sprint planning with migration tasks

**Preparation**:
1. Merge all Phase 1-5 PRs to `main`
2. Deploy to staging environment
3. Run 24-hour soak test in staging
4. Prepare rollback plan
5. Set up metrics dashboard

**Validation**:
1. All 4,388 tests pass in staging
2. Metrics show zero dropped events
3. TUI stable under load test (100 events/sec)

---

### Rollout (Staged over 3 days)

#### Day 1: Canary (10% traffic)

**Steps**:
1. Deploy to 10% of production instances
2. Monitor metrics for 8 hours
3. Check for any errors or anomalies
4. If stable, proceed to Day 2
5. If issues, rollback immediately

**Success Criteria**:
- Zero dropped events
- No increase in error rate
- No user-reported issues

---

#### Day 2: Ramp (50% traffic)

**Steps**:
1. Deploy to 50% of production instances
2. Monitor metrics for 8 hours
3. Check for any errors or anomalies
4. If stable, proceed to Day 3
5. If issues, rollback to Day 1 (10%)

**Success Criteria**:
- Zero dropped events
- No increase in error rate
- No user-reported issues

---

#### Day 3: Full Rollout (100% traffic)

**Steps**:
1. Deploy to 100% of production instances
2. Monitor metrics for 24 hours
3. Verify all old EventBus code is inactive
4. Celebrate! 🎉

**Success Criteria**:
- Zero dropped events
- No increase in error rate
- No user-reported issues
- Metrics show 100% typed events

---

### Post-Rollout (1 week after)

**Cleanup**:
1. Remove old EventBus code (if not already removed)
2. Remove dual-bus validation code
3. Archive migration guide
4. Update onboarding docs

**Retrospective**:
1. Schedule retro meeting
2. Document lessons learned
3. Update runbook with operational learnings
4. Share results with team

---

## Risk Mitigation

### Risk 1: Event Loss During Migration

**Probability**: Medium  
**Impact**: High (user data loss)

**Mitigation**:
- Run dual-bus mode (old + new) in parallel
- Compare event counts: `old_published == new_published`
- If mismatch > 5%, investigate before proceeding

**Detection**:
- Metrics dashboard shows event count comparison
- Alert if `new_published < old_published * 0.95`

**Rollback**:
- Feature flag: `USE_TYPED_MESSAGE_BUS=false`
- Reverts to old EventBus in < 5 minutes

---

### Risk 2: Performance Regression

**Probability**: Low  
**Impact**: Medium (UI lag)

**Mitigation**:
- Benchmark MessageBus throughput (target: 1000 events/sec)
- Load test in staging before production
- Monitor p99 delivery latency (target: < 100ms)

**Detection**:
- Alert if p99 latency > 100ms
- Alert if queue size > 500 events

**Rollback**:
- Feature flag rollback
- Scale up worker threads (1 → 4)

---

### Risk 3: Handler Compatibility Issues

**Probability**: Medium  
**Impact**: Medium (broken UI features)

**Mitigation**:
- Comprehensive integration tests for all handlers
- Manual testing of all TUI features before rollout
- Gradual rollout (10% → 50% → 100%)

**Detection**:
- User reports of broken features
- Error logs showing handler failures

**Rollback**:
- Feature flag rollback
- Fix handler in hotfix, redeploy

---

### Risk 4: Documentation Outdated

**Probability**: Low  
**Impact**: Low (developer confusion)

**Mitigation**:
- Documentation updates are part of Definition of Done
- Quality gates require doc review
- Auto-generate API docs and event catalog

**Detection**:
- Developers report outdated docs
- Doc review finds stale content

**Rollback**:
- Update docs in hotfix
- Add to post-rollout cleanup

---

## Success Metrics

### Reliability Metrics

| Metric | Baseline (Old EventBus) | Target (New MessageBus) | Current |
|--------|-------------------------|-------------------------|---------|
| Dropped events/day | Unknown (silent) | 0 | TBD |
| Handler failures/day | Unknown (silent) | < 10 | TBD |
| UI desync incidents/week | ~5 | 0 | TBD |
| Event delivery p99 latency | Unknown | < 100ms | TBD |

### Code Quality Metrics

| Metric | Before | After | Current |
|--------|--------|-------|---------|
| TUI bridge LOC | 5,488 | ~1,000 | TBD |
| Bare `except Exception` in TUI | 111 | 0 | TBD |
| Lambda captures in subscriptions | 15+ | 0 | TBD |
| Event payload types | `Any` | Typed | TBD |

### Developer Experience Metrics

| Metric | Before | After | Current |
|--------|--------|-------|---------|
| Time to add new event type | ~30 min | ~5 min | TBD |
| Event payload errors caught | Runtime | Dev time (mypy) | TBD |
| Event flow documentation | Ad-hoc | Auto-generated | TBD |

---

## Rollback Plan

### Trigger Conditions

Rollback if any of:
1. Dropped events > 100/hour
2. Handler failures > 50/hour
3. UI desync incidents > 3/day
4. p99 delivery latency > 200ms
5. Critical bug discovered

### Rollback Procedure

**Option 1: Feature Flag (Fast, 5 minutes)**

```python
# config.py
USE_TYPED_MESSAGE_BUS = os.getenv("USE_TYPED_MESSAGE_BUS", "true").lower() == "true"

# orchestrator.py
if USE_TYPED_MESSAGE_BUS:
    self.message_bus = MessageBus()
else:
    self.message_bus = EventBus()  # Old implementation
```

**Steps**:
1. Set environment variable: `USE_TYPED_MESSAGE_BUS=false`
2. Restart application
3. Verify old EventBus is active
4. Monitor for 1 hour

**Option 2: Git Revert (Medium, 30 minutes)**

```bash
git revert <migration-commit-sha>
git push origin main
# Deploy to production
```

**Steps**:
1. Identify migration commit SHA
2. Create revert commit
3. Deploy to production
4. Monitor for 1 hour

**Option 3: Redeploy Previous Version (Slow, 2 hours)**

```bash
git checkout <pre-migration-tag>
# Build and deploy
```

**Steps**:
1. Identify last stable release tag
2. Checkout that tag
3. Build and deploy to production
4. Monitor for 1 hour

### Post-Rollback Actions

1. Create incident report
2. Document rollback reason
3. Fix root cause in development
4. Re-test in staging
5. Plan re-rollout

---

## Appendix A: Event Type Reference

### Agent Lifecycle Events

```python
@dataclass
class AgentStarted(Event):
    """Published when agent begins execution."""
    session_id: str
    role: str  # "operational", "analyst", "planner", etc.
    task: str  # User's task description

@dataclass
class AgentResponse(Event):
    """Published when agent generates a response."""
    session_id: str
    content: str
    usage: Dict[str, int]  # {"prompt_tokens": 100, "completion_tokens": 50}

@dataclass
class AgentCompleted(Event):
    """Published when agent finishes successfully."""
    session_id: str
    result: Dict[str, Any]

@dataclass
class AgentError(Event):
    """Published when agent encounters an error."""
    session_id: str
    error: str
    traceback: str
```

### Tool Events

```python
@dataclass
class ToolCallStarted(Event):
    """Published when tool execution begins."""
    tool_name: str
    args: Dict[str, Any]
    tool_call_id: str

@dataclass
class ToolCallFinished(Event):
    """Published when tool execution completes."""
    tool_name: str
    tool_call_id: str
    result: Dict[str, Any]
    ok: bool
    duration_ms: float
```

### Session Events

```python
@dataclass
class SessionCreated(Event):
    """Published when new session is created."""
    session_id: str
    working_dir: str

@dataclass
class SessionStateUpdated(Event):
    """Published when session state changes."""
    session_id: str
    state_changes: Dict[str, Any]

@dataclass
class FilesChanged(Event):
    """Published when files in workspace change."""
    session_id: str
    files: List[str]
    change_type: Literal["created", "modified", "deleted"]
```

### Provider Events

```python
@dataclass
class ProviderModelListUpdated(Event):
    """Published when provider model list refreshes."""
    provider: str
    models: List[str]

@dataclass
class ProviderConnected(Event):
    """Published when provider connection established."""
    provider: str

@dataclass
class ProviderDisconnected(Event):
    """Published when provider connection lost."""
    provider: str
    reason: str
```

---

## Appendix B: Metrics Dashboard Configuration

### Grafana Dashboard JSON

```json
{
  "dashboard": {
    "title": "MessageBus Health",
    "panels": [
      {
        "title": "Events Published/sec",
        "targets": [
          {
            "expr": "rate(message_bus_published_total[1m])",
            "legendFormat": "{{event_type}}"
          }
        ]
      },
      {
        "title": "Events Dropped/sec",
        "targets": [
          {
            "expr": "rate(message_bus_dropped_total[1m])",
            "legendFormat": "{{event_type}}"
          }
        ]
      },
      {
        "title": "Delivery Latency (p99)",
        "targets": [
          {
            "expr": "histogram_quantile(0.99, message_bus_delivery_duration_ms)",
            "legendFormat": "{{event_type}}"
          }
        ]
      },
      {
        "title": "Queue Size",
        "targets": [
          {
            "expr": "message_bus_queue_size",
            "legendFormat": "queue_size"
          }
        ]
      },
      {
        "title": "Handler Failures/min",
        "targets": [
          {
            "expr": "rate(message_bus_handler_failed_total[1m])",
            "legendFormat": "{{handler}}"
          }
        ]
      }
    ]
  }
}
```

### Alerting Rules

```yaml
groups:
  - name: message_bus
    rules:
      - alert: MessageBusQueueFull
        expr: message_bus_queue_size > 900
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "MessageBus queue nearly full"
          description: "Queue size {{ $value }}, threshold 900"
      
      - alert: MessageBusHighDropRate
        expr: rate(message_bus_dropped_total[5m]) > 10
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "MessageBus dropping events"
          description: "Drop rate {{ $value }}/sec"
      
      - alert: MessageBusHighLatency
        expr: histogram_quantile(0.99, message_bus_delivery_duration_ms) > 200
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "MessageBus high delivery latency"
          description: "p99 latency {{ $value }}ms"
```

---

## Appendix C: Code Review Checklist

### For Reviewers

**Phase 1: Foundation**
- [ ] MessageBus uses bounded queue with explicit backpressure
- [ ] Worker thread handles exceptions without dying
- [ ] Shutdown drains queue gracefully
- [ ] Metrics collection is accurate
- [ ] Thread safety: RLock protects handler registry
- [ ] Unit tests cover edge cases (queue full, handler exception, shutdown)

**Phase 2: Event Definitions**
- [ ] Each event has clear docstring with example
- [ ] Event fields use appropriate types (not `Any`)
- [ ] Serialization round-trips correctly
- [ ] Validation catches invalid data
- [ ] Migration guide provides concrete examples

**Phase 3: TUI Bridge**
- [ ] Zero lambda captures (no `lambda p: self.app.handle(p)`)
- [ ] Zero bare `except Exception: pass`
- [ ] All exceptions logged with context
- [ ] UI updates use `call_from_thread`
- [ ] Handler methods are idempotent

**Phase 4: Publisher Migration**
- [ ] Payload structure matches event schema
- [ ] No `try/except` around publish calls
- [ ] Correlation IDs flow through chains
- [ ] Tests verify event is published

**Phase 5: Cleanup**
- [ ] Old EventBus completely removed
- [ ] No references to `event_bus.publish(str, dict)`
- [ ] All imports updated
- [ ] Documentation references updated

---

## Appendix D: Timeline Gantt Chart

```
Days 1-3: Foundation
  ├─ Day 1: Event base class, MessageBus skeleton
  ├─ Day 2: MessageBus worker thread, metrics
  └─ Day 3: Unit tests, documentation
          ↓ Quality Gate 1
          
Days 4-5: Event Definitions
  ├─ Day 4: Define 20-30 event types
  └─ Day 5: Validation, migration guide
          ↓ Quality Gate 2
          
Days 6-7: TUI Bridge
  ├─ Day 6: Create TUIEventHandler, migrate 3 mixins
  └─ Day 7: Migrate remaining 3 mixins, tests
          ↓ Quality Gate 3
          
Days 8-9: Publisher Migration
  ├─ Day 8: Migrate orchestrator, session, tool publishers
  └─ Day 9: Migrate provider publishers, dual-bus validation
          ↓ Quality Gate 4
          
Day 10: Cleanup & Rollout
  └─ Day 10: Remove old code, update docs, deploy
          ↓ Quality Gate 5
          
Days 11-13: Staged Rollout
  ├─ Day 11: 10% canary
  ├─ Day 12: 50% ramp
  └─ Day 13: 100% rollout
          ↓ SUCCESS
```

---

**END OF PLAN**

**Status**: Ready for implementation  
**Next Step**: Create Phase 1 implementation PR  
**Questions**: Contact tech lead or post in #engineering
