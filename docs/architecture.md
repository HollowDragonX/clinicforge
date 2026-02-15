# ClinicForge — Clinical Core Architecture

> **Status**: Design phase — no implementation yet.
> **Governing rules**: `.aas/architecture-rules.yaml`

---

## 1. Bounded Contexts

The Clinical Core is decomposed into four bounded contexts.
Each context owns its domain language, aggregates, and events.

| # | Bounded Context | Responsibility | Key Aggregates |
|---|----------------|----------------|----------------|
| 1 | **Patient Identity** | Canonical patient record, demographics, identifiers | `Patient`, `ContactInfo`, `Identifier` |
| 2 | **Clinical Encounter** | Visits, consultations, encounter lifecycle | `Encounter`, `Practitioner`, `Location` |
| 3 | **Clinical Records** | Diagnoses, observations, procedures, clinical notes (append-only history) | `ClinicalNote`, `Diagnosis`, `Observation`, `Procedure` |
| 4 | **Scheduling** | Appointment booking, availability, calendar | `Appointment`, `Schedule`, `TimeSlot` |

### Context Map

```
┌──────────────────┐       ┌──────────────────────┐
│ Patient Identity │◄──ACL──│  Clinical Encounter  │
└────────┬─────────┘       └──────────┬───────────┘
         │ publishes                   │ publishes
         │ PatientRegistered           │ EncounterOpened
         │ PatientUpdated              │ EncounterClosed
         ▼                             ▼
┌──────────────────┐       ┌──────────────────────┐
│   Scheduling     │◄──ACL──│  Clinical Records    │
└──────────────────┘       └──────────────────────┘
```

- **ACL** = Anti-Corruption Layer (translation between contexts)
- Contexts communicate **only via domain events** — never direct calls.

---

## 2. Module Responsibilities

Each bounded context follows **Clean Architecture** layers internally.
The directory convention per context is:

```
<context>/
  domain/           # Entities, Value Objects, Domain Events, Repository ports
  application/      # Use Cases (command/query handlers), Application Services
  infrastructure/   # Repository adapters, Event Store adapters, external I/O
  interface/        # Controllers / API handlers (thin — no business logic)
```

### Layer Rules

| Layer | May Depend On | Must NOT Depend On |
|-------|--------------|-------------------|
| **domain** | nothing (pure) | application, infrastructure, interface |
| **application** | domain | infrastructure, interface |
| **infrastructure** | domain, application (via ports) | interface |
| **interface** | application (via use cases) | domain internals, infrastructure internals |

> **AAS rule enforced**: `domain-isolation` — domain → infrastructure dependency is **forbidden**.
> **AAS rule enforced**: `no-business-logic-in-controllers` — interface layer delegates to application layer only.

### Module Breakdown

#### Patient Identity
| Module | Responsibility |
|--------|---------------|
| `domain/patient.ts` | `Patient` aggregate root, invariants, identity rules |
| `domain/events.ts` | `PatientRegistered`, `PatientUpdated`, `PatientDeactivated` |
| `domain/repository.ts` | `PatientRepository` port (interface) |
| `application/register-patient.ts` | Use case: validate + emit `PatientRegistered` |
| `application/update-patient.ts` | Use case: validate + emit `PatientUpdated` |

#### Clinical Encounter
| Module | Responsibility |
|--------|---------------|
| `domain/encounter.ts` | `Encounter` aggregate root, state machine (opened → in-progress → closed) |
| `domain/events.ts` | `EncounterOpened`, `EncounterNoteAdded`, `EncounterClosed` |
| `domain/repository.ts` | `EncounterRepository` port |
| `application/open-encounter.ts` | Use case: open encounter for a patient |
| `application/close-encounter.ts` | Use case: finalize encounter, emit `EncounterClosed` |

#### Clinical Records
| Module | Responsibility |
|--------|---------------|
| `domain/clinical-note.ts` | `ClinicalNote` entity (immutable once written) |
| `domain/diagnosis.ts` | `Diagnosis` value object |
| `domain/events.ts` | `NoteRecorded`, `DiagnosisAdded`, `ObservationRecorded` |
| `domain/repository.ts` | `ClinicalRecordRepository` port |
| `application/record-note.ts` | Use case: append clinical note to history |
| `application/add-diagnosis.ts` | Use case: append diagnosis |

#### Scheduling
| Module | Responsibility |
|--------|---------------|
| `domain/appointment.ts` | `Appointment` aggregate, booking rules |
| `domain/schedule.ts` | `Schedule` aggregate, availability logic |
| `domain/events.ts` | `AppointmentBooked`, `AppointmentCancelled`, `SlotReleased` |
| `domain/repository.ts` | `ScheduleRepository` port |
| `application/book-appointment.ts` | Use case: validate slot + emit `AppointmentBooked` |
| `application/cancel-appointment.ts` | Use case: cancel + emit `AppointmentCancelled` |

---

## 3. Event Flow

### 3.1 Event Sourcing Model

All state changes are captured as **immutable domain events** persisted to an **append-only event store**.

> **AAS rule enforced**: `event-store-access` — only `clinical-core` may access the event store.

```
Command ──► Use Case ──► Aggregate ──► Domain Event(s) ──► Event Store (append-only)
                                              │
                                              ├──► Event Bus (in-process)
                                              │       │
                                              │       ├──► Projection Handlers (read models)
                                              │       └──► Cross-context subscribers
                                              │
                                              └──► Sync Queue (offline-first)
```

### 3.2 Core Event Catalog

| Bounded Context | Event | Trigger | Consumers |
|----------------|-------|---------|-----------|
| Patient Identity | `PatientRegistered` | New patient created | Scheduling (create default schedule), Clinical Records |
| Patient Identity | `PatientUpdated` | Demographics changed | Clinical Encounter (refresh context) |
| Patient Identity | `PatientDeactivated` | Patient leaves practice | Scheduling (cancel future appointments) |
| Clinical Encounter | `EncounterOpened` | Practitioner starts visit | Clinical Records (prepare note context) |
| Clinical Encounter | `EncounterClosed` | Visit finalized | Scheduling (mark slot completed) |
| Clinical Records | `NoteRecorded` | Clinician writes note | — (append-only, internal) |
| Clinical Records | `DiagnosisAdded` | Diagnosis attached | Clinical Encounter (enrich encounter) |
| Scheduling | `AppointmentBooked` | Patient books slot | Clinical Encounter (pre-create encounter shell) |
| Scheduling | `AppointmentCancelled` | Cancellation | Clinical Encounter (void pending encounter) |

### 3.3 Offline-First Event Handling

```
┌─────────────┐      ┌───────────────┐      ┌─────────────────┐
│ Local Event  │ ───► │ Outbox Queue  │ ───► │ Remote Event     │
│ Store        │      │ (pending sync)│      │ Store            │
└─────────────┘      └───────────────┘      └─────────────────┘
       ▲                                            │
       │           conflict resolution              │
       └────────────────────────────────────────────┘
```

- Events are **always written locally first** (offline-first).
- An **Outbox** tracks unsynced events.
- On connectivity, events are pushed upstream and **conflict resolution** applies (last-writer-wins with vector clocks at the aggregate level).
- Remote events are pulled and **replayed** into local projections.

---

## 4. Dependency Directions

### 4.1 Clean Architecture — Dependency Rule

Dependencies point **inward only**. Outer layers depend on inner layers; never the reverse.

```
┌─────────────────────────────────────────────────────┐
│                    INTERFACE                         │
│  (Controllers, API handlers, CLI)                   │
│                                                     │
│   ┌─────────────────────────────────────────────┐   │
│   │              INFRASTRUCTURE                 │   │
│   │  (Repos impl, Event Store, HTTP, DB)        │   │
│   │                                             │   │
│   │   ┌─────────────────────────────────────┐   │   │
│   │   │           APPLICATION               │   │   │
│   │   │  (Use Cases, App Services)          │   │   │
│   │   │                                     │   │   │
│   │   │   ┌─────────────────────────────┐   │   │   │
│   │   │   │          DOMAIN             │   │   │   │
│   │   │   │  (Entities, Value Objects,  │   │   │   │
│   │   │   │   Events, Repo Ports)       │   │   │   │
│   │   │   └─────────────────────────────┘   │   │   │
│   │   └─────────────────────────────────────┘   │   │
│   └─────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘

Arrows: interface → infrastructure → application → domain
                                                  ▲
                                          NOTHING depends
                                          outward from here
```

### 4.2 Inter-Context Dependencies

Contexts **never import directly** from each other. Communication is mediated by:

1. **Domain Events** — async, via event bus
2. **Anti-Corruption Layers (ACL)** — translate foreign events into local domain language
3. **Shared Kernel** (minimal) — only for truly shared value objects: `PatientId`, `PractitionerId`, `EncounterId`, `Timestamp`

```
Patient Identity ──events──► Event Bus ◄──events── Clinical Encounter
                                 │
                    ┌────────────┼────────────┐
                    ▼            ▼             ▼
              Scheduling   Clinical Records   (future contexts)
```

### 4.3 Infrastructure Boundary

Infrastructure adapters implement **ports** defined in the domain/application layers (Dependency Inversion):

```
domain/repository.ts          ◄── defines interface (port)
infrastructure/event-store-    ── implements interface (adapter)
  repository.ts
```

The domain never knows about:
- Database engines
- HTTP frameworks
- File systems
- External services

---

## Summary of AAS Rule Compliance

| AAS Rule | How Enforced |
|----------|-------------|
| `domain-isolation` | Domain layer has zero imports from infrastructure. Ports (interfaces) live in domain; adapters live in infrastructure. |
| `event-store-access` | Only `clinical-core` modules interact with the event store. No external context or module writes/reads events directly. |
| `no-business-logic-in-controllers` | Interface layer delegates entirely to application use cases. Controllers handle HTTP/serialization only. |
