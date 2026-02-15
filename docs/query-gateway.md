# Query Gateway

> **Status**: Design — pending review before implementation.
> **Depends on**: `docs/command-gateway.md`, `docs/clinical-projections.md`, `docs/architecture.md`
> **Governing rules**: `.aas/architecture-rules.yaml`

---

## Purpose

The Query Gateway is the **single entry point** through which external clients read clinical information. It exposes projection state safely, without direct access to aggregates, event stores, or domain logic. It is the read-side counterpart to the Command Gateway.

---
---

# Part 1 — Responsibilities

The Query Gateway has exactly four responsibilities. It does nothing else.

## 1. Receive Query Requests

Accept a raw query request (dict) from the external world. The request specifies which projection to query and any filter parameters. Like the Command Gateway, the Query Gateway is transport-agnostic — it works identically for HTTP, CLI, tests, or message queues.

What it does NOT do:
- Accept commands. Commands go through the Command Gateway exclusively.
- Trigger domain logic or event production.
- Modify any state.

## 2. Fetch Projection State

The gateway retrieves the current state of the requested projection. Projections are in-memory read models maintained by `ProjectionHandler` implementations. The gateway reads their `.state` property — a plain dict.

What it does NOT do:
- Query the event store directly. The event store is a write-side concern.
- Load or rehydrate aggregates. Aggregates are a command-side concern.
- Run business logic against the projection state.

## 3. Map Projection → Response DTO

The gateway transforms raw projection state into a structured response suitable for the external client. This is a **pure mapping** — no computation, no filtering by business rules, no aggregation.

```
Projection state (internal dict)  →  Response DTO (external dict)
```

The mapping may:
- Rename internal field names to external API names.
- Format dates for display.
- Flatten nested structures.
- Exclude internal fields not relevant to the client.

The mapping must NOT:
- Filter by domain rules (e.g., "only show active conditions for this role").
- Compute derived values (e.g., "days since last visit").
- Access any other data source.

## 4. Return Result

The gateway wraps the response in a `QueryResult` and returns it. Like the Command Gateway, the Query Gateway **never throws exceptions**. All outcomes are returned as values.

---

## What the Query Gateway Is NOT

| Not the gateway's job | Who does it |
|----------------------|-------------|
| Authentication / authorization | Middleware / auth layer |
| Visibility filtering (42 CFR Part 2) | A projection-level concern or middleware |
| Event production | Command side only |
| Aggregate loading | Command handler |
| Event store access | Command handler / sync engine |
| Business logic | Aggregates (write) or projections (read fold) |
| Cache management | Infrastructure concern |

---
---

# Part 2 — Query Lifecycle

A query request flows through the gateway in four deterministic steps.

```
External Client
      │
      ▼
┌─────────────────────────────────────────────────────┐
│ QUERY GATEWAY                                        │
│                                                      │
│  Step 1: Receive raw query request (dict)            │
│      │                                               │
│  Step 2: Validate query shape                        │
│      │    ✗ → return QueryResult(error=...)          │
│      ▼                                               │
│  Step 3: Fetch projection state + map to response    │
│      │                                               │
│  Step 4: Return result                               │
│      │    found → QueryResult(data={...})            │
│      │    not found → QueryResult(error=...)         │
│                                                      │
└─────────────────────────────────────────────────────┘
```

### Step 1: Receive

The gateway receives a raw request dict:
```python
{
    "query_type": "PatientSummary",
    "params": {
        "patient_id": "uuid-string"
    }
}
```

The `query_type` identifies which projection and mapper to use. `params` are optional filter parameters.

### Step 2: Validate

Structural validation only:
1. `query_type` is present and is a registered query.
2. `params` is a dict (if provided).
3. Required parameters for this query type are present.

No business-rule validation. If the projection state is empty, that is a valid result (not an error).

### Step 3: Fetch and Map

The gateway:
1. Looks up the registered projection for this `query_type`.
2. Reads the projection's current `.state`.
3. Passes the state and params to a **response mapper** (pure function).
4. The mapper produces the external response dict.

### Step 4: Return

```python
QueryResult(
    success=True,
    data={"active_conditions": [...], "active_treatments": [...]},
    error=""
)
```

Or on failure:
```python
QueryResult(success=False, data={}, error="Unknown query type: ...")
```

---
---

# Part 3 — Separation from Command Gateway

## CQRS: Two Gateways, Two Paths

The system implements **Command Query Responsibility Segregation** (CQRS) through two physically separate gateways.

```
                 ┌───────────────────┐
  Commands ─────►│  COMMAND GATEWAY  │───► Handlers ──► Aggregates ──► Events ──► Store
                 └───────────────────┘                                     │
                                                                           ▼
                                                                      Dispatcher
                                                                           │
                                                                           ▼
                 ┌───────────────────┐                               Projections
  Queries  ─────►│  QUERY GATEWAY   │◄──────────────────────────────── (read)
                 └───────────────────┘
```

### Why separate gateways?

| Reason | Explanation |
|--------|-------------|
| **Different responsibilities** | Command gateway validates + maps + routes commands. Query gateway fetches + maps projections. Mixing them creates a god object. |
| **Different scaling characteristics** | Reads are typically 10x more frequent than writes. Separate gateways allow independent scaling. |
| **Different failure modes** | A command failure (DomainError) is fundamentally different from a query failure (projection not found). Separate result types reflect this. |
| **Different security models** | Commands require write authorization. Queries require read authorization. Different middleware chains. |
| **Enforces CQRS** | Physical separation makes it impossible to accidentally read projections during command processing or write events during query processing. |

### What they share

| Shared property | Implementation |
|----------------|---------------|
| Transport agnosticism | Both accept `dict[str, Any]` — no framework coupling |
| Never throw exceptions | Both return result objects (`GatewayResult`, `QueryResult`) |
| Structural validation only | Neither performs domain validation |
| Pure mapping | Both have mapper registries (command mappers, response mappers) |

### What they do NOT share

| Command Gateway | Query Gateway |
|----------------|---------------|
| Accesses handlers | Accesses projections |
| Produces side effects (events) | Produces no side effects |
| Returns `GatewayResult` with events | Returns `QueryResult` with data |
| Registers command types | Registers query types |

---
---

# Part 4 — Why Projections Are the Read Model

## The Problem

Aggregates are write-optimized. They enforce invariants and produce events. But reading an aggregate requires:
1. Loading the full event stream.
2. Replaying every event to rebuild state.
3. Extracting the fields the client needs.

This is expensive, slow, and wasteful for reads. A patient with 500 events doesn't need to be rehydrated every time someone views their summary.

## The Solution: Projections

Projections are **pre-computed read models** maintained by folding events as they arrive. They are:

### 1. Always up to date (eventually)
Projections subscribe to events via the EventDispatcher. When a `DiagnosisConfirmed` event is dispatched, the PatientSummaryProjection immediately incorporates it. The read model is ready before the next query.

### 2. Query-optimized
Projections are shaped for how clients want to read data, not for how the domain stores it. The PatientSummaryProjection provides `active_conditions` and `active_treatments` as flat dicts — ready to serialize, no further processing needed.

### 3. Disposable and rebuildable
Projections carry no authoritative state. If a projection is corrupted or outdated, it can be destroyed and rebuilt by replaying all events from the event store. The events are the source of truth, not the projection.

### 4. Decoupled from the write model
Projections don't know about aggregates, commands, or handlers. They consume events. This means:
- Adding a new projection doesn't change the write side.
- Modifying a projection doesn't affect command processing.
- Multiple projections can consume the same events for different purposes.

### 5. Idempotent
Projections track processed event IDs. Receiving the same event twice (e.g., during sync) does not corrupt the projection state. This is critical for offline-first operation.

## Why the Query Gateway reads projections (not aggregates, not the event store)

| Alternative | Why it's wrong |
|-------------|---------------|
| **Read aggregate state** | Requires loading and replaying the full event stream for every query. O(n) per read where n = number of events. Violates CQRS. |
| **Read event store directly** | Returns raw events, not structured read models. The client would have to implement its own fold logic. Exposes internal event structure. |
| **Read projections** | Pre-computed, query-optimized, always ready. O(1) per read. Decoupled from write model. Rebuildable. Idempotent. **This is the correct answer.** |
