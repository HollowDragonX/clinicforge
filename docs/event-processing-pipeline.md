# Event Processing Pipeline

> **Status**: Design — pending review before implementation.
> **Depends on**: `docs/clinical-event-taxonomy.md`, `docs/clinical-event-invariants-and-metadata.md`, `docs/aggregates-streams-consistency.md`, `docs/clinical-projections.md`
> **Governing rules**: `.aas/architecture-rules.yaml`

---

## Foundational Rules

1. **Events are written once.** The event store is append-only. Once persisted, an event is never modified or deleted.
2. **The pipeline distributes events to projections.** It does not transform, filter, or enrich events — it delivers them.
3. **Projections react independently.** Each projection processes events at its own pace. One slow or failing projection does not block others.
4. **No projection may query domain logic.** Projections consume events. They do not call aggregates, use cases, or command handlers.

---

## Pipeline Stages

The pipeline has five stages, executed in strict order for each event:

```
┌─────────┐    ┌─────────────┐    ┌──────────┐    ┌────────────┐    ┌────────────┐
│ Stage 1  │───►│   Stage 2   │───►│ Stage 3  │───►│  Stage 4   │───►│  Stage 5   │
│  Accept  │    │   Persist   │    │ Publish  │    │   Route    │    │  Process   │
└─────────┘    └─────────────┘    └──────────┘    └────────────┘    └────────────┘
```

---

### Stage 1: Accept

**Responsibility**: Receives an event from an aggregate (via the command handler) and validates that it carries all mandatory metadata and that the metadata is well-formed.

**What it does**:
- Validates the event envelope: all 17 mandatory metadata fields are present and correctly typed.
- Validates `occurredAt` is not in the future (INV-XX-1, with 5-minute tolerance).
- Validates `aggregateVersion` is a positive integer.
- Validates `eventType` is a recognized type in the event taxonomy.
- Does NOT validate domain invariants — those are the aggregate's responsibility. By the time an event reaches the pipeline, the aggregate has already accepted it.

**Input**: A domain event emitted by an aggregate.
**Output**: A validated event envelope, or a rejection with the validation error.

**Failure mode**: Validation failure → event is rejected back to the command handler. No data is written. The command handler reports the error to the caller.

---

### Stage 2: Persist

**Responsibility**: Appends the validated event to the event store. This is the **commit point** — once persisted, the event is a permanent fact.

**What it does**:
- Checks optimistic concurrency: the event's `aggregateVersion` must equal the current stream length + 1 (INV-XX-3).
- Deduplicates by `eventId`: if an event with this ID already exists, the append is a no-op (idempotent).
- Appends the event to the aggregate's stream.
- Records `recordedAt` timestamp (set by the system, not the caller).

**Input**: A validated event envelope from Stage 1.
**Output**: The persisted event (with final `recordedAt`), or a concurrency conflict error.

**Failure mode**:
- Version conflict → rejected back to command handler for retry.
- Duplicate `eventId` → silent success (idempotent, no error).
- Storage failure → rejected back to command handler. The event was NOT persisted. The aggregate state has not changed.

**Critical property**: After this stage completes successfully, the event is **durable**. Even if all subsequent stages fail, the event exists and will eventually be processed by projections (via recovery).

---

### Stage 3: Publish

**Responsibility**: Announces to the event bus that a new event has been persisted. This decouples persistence from projection processing.

**What it does**:
- Places the persisted event onto the in-process event bus.
- The event bus is a simple notification mechanism — it does not store events. It tells subscribers "there is a new event at position X in stream Y."
- Publication is **at-least-once**: if the publish fails, it will be retried. Subscribers must handle duplicates.

**Input**: The persisted event from Stage 2.
**Output**: A notification delivered to all registered subscribers.

**Failure mode**: If publication fails (bus unavailable), the event is still persisted (Stage 2 succeeded). A **catch-up mechanism** ensures projections eventually discover the event by polling the event store for events after their checkpoint. Publication failure is a latency issue, not a correctness issue.

---

### Stage 4: Route

**Responsibility**: Determines which projection handlers should receive the published event, based on their subscription filters.

**What it does**:
- Maintains a registry of projection subscriptions (each projection declares which `eventType`, `aggregateType`, or other metadata fields it cares about).
- For each published event, evaluates every subscription filter.
- Delivers the event to each matching projection handler's inbox.
- Delivery order across projections is undefined — projections are independent.

**Input**: A published event notification from Stage 3.
**Output**: The event is placed into the inbox of each matching projection handler.

**Failure mode**: If routing fails for one projection, other projections are unaffected. The failed projection's inbox is empty for this event, but the catch-up mechanism will deliver it later.

---

### Stage 5: Process

**Responsibility**: Each projection handler receives the event and updates its read model. This is where the projection's fold function executes.

**What it does**:
- The handler checks if this event has already been processed (by `eventId`, compared against its checkpoint or deduplication set). If yes, skip.
- The handler calls its fold function: `new_state = handler(current_state, event)`.
- The handler persists the updated read model state.
- The handler advances its checkpoint to this event.

**Input**: An event from the projection's inbox.
**Output**: Updated read model state + advanced checkpoint.

**Failure mode**: If the handler fails (bug, transient error):
- The read model is NOT updated.
- The checkpoint is NOT advanced.
- The event remains unprocessed.
- On next processing cycle (or catch-up), the event will be retried.
- After N retries, the event is placed in a **dead letter queue** for that projection. The projection continues processing subsequent events.
- The dead letter queue is reviewed by operators to fix the root cause, then replayed.

---

## Data Flow

### Happy Path (Single Event)

```
Aggregate emits event
       │
       ▼
┌─── Stage 1: Accept ───┐
│ Validate metadata      │
│ Check occurredAt       │
│ Check eventType        │
└──────────┬─────────────┘
           │ validated event
           ▼
┌─── Stage 2: Persist ──┐
│ Check version (OCC)    │
│ Dedup by eventId       │
│ Append to stream       │
│ Set recordedAt         │
└──────────┬─────────────┘
           │ persisted event
           ▼
┌─── Stage 3: Publish ──┐
│ Notify event bus       │
└──────────┬─────────────┘
           │ notification
           ▼
┌─── Stage 4: Route ────┐
│ Match subscription     │
│ filters                │
│                        │
│ ┌─ Projection A: yes  │
│ ├─ Projection B: no   │
│ └─ Projection C: yes  │
└──────────┬─────────────┘
           │ to matching projections
      ┌────┴────┐
      ▼         ▼
┌─ Stage 5 ─┐ ┌─ Stage 5 ─┐
│ Proj A     │ │ Proj C     │
│ dedup      │ │ dedup      │
│ fold       │ │ fold       │
│ checkpoint │ │ checkpoint │
└────────────┘ └────────────┘
```

### Write Side vs. Read Side Boundary

```
═══════════════════════════════════════════════════════
         WRITE SIDE (synchronous, blocking)
═══════════════════════════════════════════════════════

  Command Handler → Aggregate → Stage 1 → Stage 2
                                              │
                                         commit point
                                              │
═══════════════════════════════════════════════════════
         READ SIDE (asynchronous, non-blocking)
═══════════════════════════════════════════════════════

                                   Stage 3 → Stage 4 → Stage 5
```

The **write side** (Stages 1–2) is synchronous. The command handler waits for persistence to succeed or fail before responding to the caller.

The **read side** (Stages 3–5) is asynchronous. The command handler does NOT wait for projections to update. This is why projections are eventually consistent.

---

## Failure Recovery Strategy

### Principle: The Event Store Is the Recovery Source

Every failure recovery mechanism ultimately reads from the event store. Projections are disposable. The event bus is a notification optimization. If everything fails except the event store, the system can recover fully.

### Recovery Mechanisms

#### 1. Catch-Up Polling (Primary Recovery)

Every live projection periodically polls the event store for events after its checkpoint, independent of the event bus:

```
every N seconds:
  last_checkpoint = projection.get_checkpoint()
  new_events = event_store.read_events_after(last_checkpoint, filter=projection.subscription)
  for event in new_events:
    projection.process(event)
```

This ensures that even if Stage 3 (Publish) or Stage 4 (Route) fails entirely, projections still converge. The event bus is an optimization for low latency — catch-up polling is the correctness guarantee.

**Catch-up interval**: Configurable per projection. Internal read models (PatientStatus, EncounterState) poll frequently (1–5 seconds). Analytics projections poll less frequently (30–60 seconds).

#### 2. Idempotent Processing (Duplicate Safety)

Events may be delivered more than once (at-least-once delivery). Every projection handler deduplicates by `eventId`:

```
if event.eventId in projection.processed_event_ids:
  skip  # already processed
else:
  new_state = handler(current_state, event)
  save(new_state)
  advance_checkpoint(event)
```

This makes the pipeline **exactly-once in effect** even though delivery is at-least-once.

#### 3. Dead Letter Queue (Per-Projection)

If a projection handler fails to process an event after N retries (default: 3), the event is moved to that projection's dead letter queue:

```
Dead Letter Entry:
  - eventId
  - eventType
  - projectionName
  - failureReason
  - failureCount
  - firstFailedAt
  - lastFailedAt
```

The projection continues processing subsequent events. The dead letter queue is:
- Monitored by operators.
- Reviewed to identify bugs in projection handlers.
- Replayed after the bug is fixed.

**The dead letter queue is per-projection.** A bug in the ActiveProblemList handler does not affect the EncounterWorklist.

#### 4. Full Projection Rebuild (Nuclear Recovery)

If a projection is irrecoverably corrupted, it is deleted and rebuilt from the event store (as defined in the projection rebuild strategy). This is the recovery of last resort but is always available.

```
Recovery escalation:
  1. Catch-up polling fills gaps automatically
  2. Retry handles transient failures
  3. Dead letter queue isolates persistent failures
  4. Full rebuild recovers from corruption
```

---

### Failure Scenario Matrix

| Failure | Impact | Recovery | Data at Risk |
|---------|--------|----------|-------------|
| **Stage 1 fails** (validation) | Event rejected | Command handler retries or reports error | None — nothing was written |
| **Stage 2 fails** (storage) | Event not persisted | Command handler retries | None — nothing was written |
| **Stage 2 version conflict** | Event rejected | Command handler retries with current version | None — nothing was written |
| **Stage 3 fails** (bus down) | Event persisted but not announced | Catch-up polling delivers it | None — event is safe in store |
| **Stage 4 fails** (routing error) | One or more projections miss the event | Catch-up polling delivers it | None — event is safe in store |
| **Stage 5 fails** (handler bug) | One projection not updated | Retry → dead letter → fix → replay | None — event is safe in store |
| **Projection corrupted** | Read model wrong | Full rebuild from events | None — event is safe in store |
| **Event store failure** | System cannot accept new events | System is unavailable until store recovers | Events not yet persisted are lost (caller must retry) |

**Key insight**: In every failure scenario except event store failure, **no clinical data is at risk**. The event store is the single point of durability. Everything else is recoverable from it.

---

## Pipeline Invariants

These must always hold:

1. **No event is persisted without passing Stage 1 validation.** Malformed events never enter the store.
2. **No event is persisted with a version gap.** Aggregate version sequence is contiguous (INV-XX-3).
3. **No projection processes an event before it is persisted.** The read side never sees uncommitted data.
4. **No projection blocks another projection.** Independent processing, independent failure.
5. **Every persisted event is eventually processed by every subscribed projection.** Catch-up polling guarantees convergence.
6. **Processing is idempotent.** Duplicate delivery does not corrupt read models.
