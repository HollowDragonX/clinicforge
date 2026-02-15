# Command Gateway

> **Status**: Design — pending review before implementation.
> **Depends on**: `docs/command-model.md`, `docs/architecture.md`
> **Governing rules**: `.aas/architecture-rules.yaml`

---

## Purpose

The Command Gateway is the **single entry point** through which the external world interacts with the domain. It exposes domain capabilities safely by accepting commands and returning results. It is the boundary between untrusted external input and the trusted domain model.

---
---

# Part 1 — Gateway Responsibilities

The gateway has exactly four responsibilities. It does nothing else.

## 1. Receive External Requests

Accept raw input from the outside world — HTTP requests, CLI arguments, message queue payloads, test harnesses. The gateway is agnostic to the transport mechanism. It receives a **request dict** (or equivalent untyped structure) and begins processing.

What it does NOT do:
- Parse HTTP headers, manage sessions, or handle authentication. That is middleware, not the gateway.
- Read query parameters for projections. The gateway handles commands only.

## 2. Validate Input Shape

The gateway performs **structural validation only** — ensuring the request has the required fields with the correct types. This is validation of the request envelope, not domain validation.

| Gateway validates | Gateway does NOT validate |
|------------------|--------------------------|
| Required fields present | Business invariants (e.g., "encounter must be active") |
| Field types correct (UUID, datetime, string) | Cross-aggregate preconditions |
| Enum values within allowed set | Domain state machine rules |
| String lengths within bounds | Clinical correctness |

If structural validation fails, the gateway returns an error immediately. No command is created. No handler is invoked.

## 3. Map Request → Command

The gateway transforms a validated request dict into a **frozen command dataclass**. This is a pure mapping — no logic, no enrichment, no database lookups.

```
Request dict (untrusted, untyped)  →  Command dataclass (trusted, typed)
```

After this step, the command is a valid domain object that can be passed to a command handler.

## 4. Send Command to Handler and Return Result

The gateway dispatches the command to the appropriate `CommandHandler` and returns the result (success with events, or failure with error).

The gateway does NOT:
- Interpret the result (that is the caller's responsibility).
- Modify events (events are produced by aggregates, not the gateway).
- Access projections or read models.
- Produce events of its own.

---

## What the Gateway Is NOT

| Not the gateway's job | Who does it |
|----------------------|-------------|
| Authentication | Middleware / auth layer |
| Authorization (role-based) | Middleware or command handler precondition |
| Domain validation | Aggregate (via `execute()`) |
| Cross-aggregate checks | Command handler (e.g., `DiagnosisCommandHandler._check_encounter_active()`) |
| Event production | Aggregate |
| Event persistence | Command handler → event store |
| Event dispatch | Command handler → event dispatcher |
| Projection queries | Read API (separate from gateway) |

---
---

# Part 2 — Request Lifecycle

A request flows through the gateway in five deterministic steps.

```
External Client
      │
      ▼
┌─────────────────────────────────────────────────────┐
│ COMMAND GATEWAY                                      │
│                                                      │
│  Step 1: Receive raw request (dict / JSON / args)    │
│      │                                               │
│      ▼                                               │
│  Step 2: Validate input shape                        │
│      │    ✗ → return ValidationError                 │
│      ▼                                               │
│  Step 3: Map request → command dataclass              │
│      │                                               │
│      ▼                                               │
│  Step 4: Route command to handler                    │
│      │                                               │
│      ▼                                               │
│  Step 5: Return result                               │
│      │    success → GatewayResult(events=[...])      │
│      │    domain error → GatewayResult(error=...)    │
│      │    concurrency → GatewayResult(error=...)     │
│                                                      │
└─────────────────────────────────────────────────────┘
```

### Step 1: Receive

The gateway receives a raw request as a `dict[str, Any]`. The request must include:
- `command_type`: string identifying which command to create (e.g., `"ConfirmDiagnosis"`)
- `payload`: dict containing the command-specific fields

The gateway does not know or care how this dict was constructed (HTTP JSON body, CLI parsed args, test fixture).

### Step 2: Validate

Structural validation checks:
1. `command_type` is present and is a known command.
2. `payload` is present and is a dict.
3. All required fields for this command type are present in the payload.
4. Field types are correct (UUIDs are valid, datetimes parse, enums are valid).

If validation fails → `GatewayResult` with `ValidationError` is returned immediately.

### Step 3: Map

The gateway looks up a **command mapper** for the given `command_type`. The mapper is a pure function:

```python
def map_confirm_diagnosis(payload: dict) -> ConfirmDiagnosis:
    return ConfirmDiagnosis(
        diagnosis_id=UUID(payload["diagnosis_id"]),
        encounter_id=UUID(payload["encounter_id"]),
        ...
    )
```

Each command type has one mapper. Mappers are registered with the gateway at startup.

### Step 4: Route

The gateway looks up the `CommandHandler` registered for this command type and calls `handler.handle(command, aggregate_id)`.

Routing is a simple lookup table:
```
"ConfirmDiagnosis" → DiagnosisCommandHandler
"StartEncounter"   → EncounterCommandHandler
```

### Step 5: Return

The gateway wraps the handler's result in a `GatewayResult`:

| Handler outcome | GatewayResult |
|----------------|---------------|
| Success (events returned) | `GatewayResult(success=True, events=[...], error=None)` |
| `DomainError` raised | `GatewayResult(success=False, events=[], error=DomainError msg)` |
| `ConcurrencyError` raised | `GatewayResult(success=False, events=[], error=ConcurrencyError msg)` |
| `ValidationError` (step 2) | `GatewayResult(success=False, events=[], error=ValidationError msg)` |

The gateway never throws exceptions to the caller. All outcomes are returned as values.

---
---

# Part 3 — Boundary Between External World and Domain

## The Gateway Is the Membrane

```
┌──────────────────────────┐    ┌──────────────────────────────┐
│     EXTERNAL WORLD       │    │         DOMAIN CORE          │
│                          │    │                              │
│  HTTP/CLI/Queue/Tests    │    │  Commands (frozen, typed)    │
│  Raw dicts, strings      │    │  Aggregates (state machines) │
│  Untrusted input         │    │  Events (immutable facts)    │
│  Any transport           │    │  Handlers (orchestrators)    │
│                          │    │                              │
└──────────┬───────────────┘    └──────────────┬───────────────┘
           │                                    ▲
           │         ┌──────────────┐           │
           └────────►│   COMMAND    │───────────┘
                     │   GATEWAY    │
                     │              │
                     │  validates   │
                     │  maps        │
                     │  routes      │
                     │  returns     │
                     └──────────────┘
```

## What Crosses the Boundary

| Direction | What crosses | Format |
|-----------|-------------|--------|
| **Inward** (external → domain) | Request payload | `dict[str, Any]` → `Command` dataclass |
| **Outward** (domain → external) | Command result | `GatewayResult` (success/failure + events or error) |

Nothing else crosses. The external world never sees:
- Aggregate state
- Event store internals
- Projection data (that goes through a separate read API)
- Command handler internals

## Why This Boundary Matters

### 1. Transport independence
The gateway accepts `dict[str, Any]`. Whether it came from HTTP JSON, a CLI parser, a message queue, or a test fixture is irrelevant. The domain never knows.

### 2. Validation isolation
Structural validation (gateway) is separated from domain validation (aggregate). A malformed request never reaches the domain. A structurally valid but domain-invalid command reaches the aggregate and is rejected there.

### 3. Testability without infrastructure
The gateway can be tested with plain dicts. No HTTP server, no framework, no middleware needed. This is the design goal: **simulate requests locally**.

### 4. Single point of entry
Every command enters through the gateway. There is no backdoor. This means:
- All input is validated.
- All commands are routed through handlers.
- All results are wrapped in `GatewayResult`.
- Audit, rate-limiting, and authorization can be added at the gateway without touching the domain.

### 5. The gateway enforces the rules

| Rule | How the gateway enforces it |
|------|----------------------------|
| Gateway accepts commands only | Only `command_type` + `payload` structure is accepted |
| Gateway never produces events | No event construction in gateway code |
| Gateway never accesses projections | No projection imports, no read model queries |
| Gateway performs input shape validation only | Validation checks types and presence, not business rules |
