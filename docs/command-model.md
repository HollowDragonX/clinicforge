# Command Model

> **Status**: Design — pending review before implementation.
> **Depends on**: `docs/clinical-event-taxonomy.md`, `docs/clinical-event-invariants-and-metadata.md`, `docs/aggregates-streams-consistency.md`, `docs/event-processing-pipeline.md`
> **Governing rules**: `.aas/architecture-rules.yaml`

---

## Foundational Rules

1. **Commands express clinical intent.** A command is a request to do something — not a statement that something happened. "Register this patient" is a command. "Patient was registered" is an event.
2. **Commands may be rejected.** Unlike events (which are facts), commands can fail — because the patient is already deceased, because the encounter is not active, because the clinician lacks authorization.
3. **Only aggregates produce events.** A command handler never creates events directly. It passes the command to an aggregate, and the aggregate decides whether to accept it and which events to emit.
4. **Commands never mutate state directly.** State changes happen exclusively through events. A command results in events; events are persisted; state is derived from events.

---

# Part 1 — Command Categories

Commands are organized to mirror the six event categories and map directly to the 14 aggregates.

---

## 1. Patient Identity Commands

**Clinical intent**: Managing the patient-practice relationship — enrolling new patients, correcting identity errors, recording life events that affect care.

**Target aggregate**: PatientRegistration (lifecycle)

| Command | Clinical Intent | Resulting Event | Why It May Be Rejected |
|---------|----------------|-----------------|----------------------|
| `RegisterPatient` | "Enroll this person as a new patient of this practice." | `PatientRegistered` | Duplicate detection finds a matching patient already registered. |
| `CorrectPatientIdentity` | "We discovered an error in this patient's recorded identity — fix it." | `PatientIdentityCorrected` | Patient does not exist (INV-PL-4). Patient is in terminal state (deceased/transferred). |
| `ProvideContactInfo` | "The patient has given us new contact details." | `PatientContactInfoProvided` | Patient does not exist (INV-PL-1). |
| `RecordPatientDeceased` | "We have been informed this patient has died." | `PatientDeceasedRecorded` | Patient does not exist. Patient already deceased (INV-PL-2). |
| `TransferPatientOut` | "This patient is formally leaving our practice." | `PatientTransferredOut` | Patient does not exist. Patient already transferred (INV-PL-3). |

**Special case** (DuplicateResolution aggregate):

| Command | Clinical Intent | Resulting Event |
|---------|----------------|-----------------|
| `IdentifyDuplicatePatients` | "We discovered these two patient records are the same person." | `PatientDuplicateIdentified` |

---

## 2. Encounter Progression Commands

**Clinical intent**: Moving a clinical visit through its natural stages — from arrival through discharge.

**Target aggregate**: Encounter (lifecycle)

| Command | Clinical Intent | Resulting Event | Why It May Be Rejected |
|---------|----------------|-----------------|----------------------|
| `CheckInPatient` | "This patient has arrived for their visit." | `PatientCheckedIn` | Patient is not active (INV-EP-1, cross-aggregate). |
| `TriagePatient` | "Assess this patient's chief complaint and urgency before the doctor sees them." | `PatientTriaged` | Encounter not in checked-in state (INV-EP-2). |
| `BeginEncounter` | "The practitioner is starting the clinical interaction." | `EncounterBegan` | Encounter not in checked-in or triaged state (INV-EP-2). Concurrent encounter for same patient+practitioner (INV-EP-3, cross-aggregate). |
| `CompleteEncounter` | "The clinical interaction is finished." | `EncounterCompleted` | Encounter not in active state (INV-EP-2). |
| `DischargePatient` | "Release the patient with instructions." | `PatientDischarged` | Encounter not in completed state (INV-EP-2). |
| `ReopenEncounter` | "We need to revisit this completed encounter for additional clinical work." | `EncounterReopened` | Encounter not in completed or discharged state (INV-EP-2). |

---

## 3. Clinical Observation Commands

**Clinical intent**: Recording what was observed, measured, or reported about the patient's health. Raw clinical data.

**Target aggregates**: VitalSigns, Symptom, ExaminationFinding, LabResult (fact), AllergyRecord (lifecycle)

| Command | Clinical Intent | Resulting Event | Why It May Be Rejected |
|---------|----------------|-----------------|----------------------|
| `RecordVitalSigns` | "Record the vitals I just measured." | `VitalSignsRecorded` | Encounter not active (INV-CO-1, cross-aggregate). |
| `ReportSymptom` | "The patient reports this symptom." | `SymptomReported` | Encounter not active (INV-CO-1, cross-aggregate). |
| `NoteExaminationFinding` | "I found this during the physical exam." | `ExaminationFindingNoted` | Encounter not active (INV-CO-1, cross-aggregate). |
| `ReceiveLabResult` | "A lab result has arrived for this patient." | `LabResultReceived` | Patient not active (INV-CO-2, cross-aggregate). |
| `IdentifyAllergy` | "This patient has an allergy to this substance." | `AllergyIdentified` | Patient not active (INV-CO-2, cross-aggregate). |
| `RefuteAllergy` | "The previously recorded allergy is clinically incorrect." | `AllergyRefuted` | No prior allergy identified for this substance (INV-CO-3). |

---

## 4. Clinical Judgment Commands

**Clinical intent**: Recording clinical decisions — diagnoses, procedures, referrals, treatment plans.

**Target aggregates**: Diagnosis (lifecycle), Procedure, Referral, TreatmentPlan (fact)

| Command | Clinical Intent | Resulting Event | Why It May Be Rejected |
|---------|----------------|-----------------|----------------------|
| `MakeDiagnosis` | "I have determined the patient has this condition." | `DiagnosisMade` | Encounter not active (INV-CJ-1, cross-aggregate). |
| `ReviseDiagnosis` | "My clinical judgment about this prior diagnosis has changed." | `DiagnosisRevised` | No prior diagnosis to revise (INV-CJ-2). |
| `ResolveDiagnosis` | "This condition is no longer active." | `DiagnosisResolved` | Diagnosis already resolved (INV-CJ-3). |
| `PerformProcedure` | "I performed this procedure on the patient." | `ProcedurePerformed` | Encounter not active (INV-CJ-4, cross-aggregate). |
| `DecideReferral` | "This patient needs to see a specialist." | `ReferralDecided` | Encounter not active (INV-CJ-4, cross-aggregate). |
| `FormulateTreatmentPlan` | "This is the care plan going forward." | `TreatmentPlanFormulated` | No active diagnosis in this encounter (INV-CJ-5, cross-aggregate). |

---

## 5. Clinical Documentation Commands

**Clinical intent**: Authoring the permanent medical record.

**Target aggregate**: ClinicalNote (lifecycle)

| Command | Clinical Intent | Resulting Event | Why It May Be Rejected |
|---------|----------------|-----------------|----------------------|
| `AuthorClinicalNote` | "I am writing a clinical note for this encounter." | `ClinicalNoteAuthored` | Encounter does not exist (INV-CD-1, cross-aggregate). |
| `AuthorNoteAddendum` | "I need to add supplementary information to my previous note." | `NoteAddendumAuthored` | Referenced note does not exist (INV-CD-2). |
| `CosignNote` | "As a supervisor, I attest to this trainee's note." | `NoteCosigned` | Referenced note does not exist. Cosigner is the same as the author (INV-CD-3). |

---

## 6. Care Access Commands

**Clinical intent**: Managing the patient's access to future clinical encounters.

**Target aggregate**: Appointment (lifecycle)

| Command | Clinical Intent | Resulting Event | Why It May Be Rejected |
|---------|----------------|-----------------|----------------------|
| `RequestAppointment` | "This patient wants to schedule a visit." | `AppointmentRequested` | Patient not active (INV-PL-2/3, cross-aggregate). |
| `ConfirmAppointment` | "We are assigning this appointment a specific time and provider." | `AppointmentConfirmed` | No prior request (INV-CA-1). |
| `CancelAppointmentAsPatient` | "The patient is cancelling their appointment." | `AppointmentCancelledByPatient` | Appointment already cancelled (INV-CA-2). |
| `CancelAppointmentAsPractice` | "We are cancelling this patient's appointment." | `AppointmentCancelledByPractice` | Appointment already cancelled (INV-CA-2). |
| `RecordNoShow` | "The patient did not appear for their confirmed appointment." | `PatientNoShowed` | Appointment not confirmed, time not passed, or patient checked in (INV-CA-3). |
| `RescheduleAppointment` | "Move this appointment to a different time." | `AppointmentRescheduled` | Appointment not active (INV-CA-4). |

---

## Command–Aggregate–Event Map

| # | Command Category | Target Aggregate(s) | Commands | Events |
|---|-----------------|---------------------|----------|--------|
| 1 | Patient Identity | PatientRegistration, DuplicateResolution | 6 | 6 |
| 2 | Encounter Progression | Encounter | 6 | 6 |
| 3 | Clinical Observation | VitalSigns, Symptom, ExaminationFinding, LabResult, AllergyRecord | 6 | 6 |
| 4 | Clinical Judgment | Diagnosis, Procedure, Referral, TreatmentPlan | 6 | 6 |
| 5 | Clinical Documentation | ClinicalNote | 3 | 3 |
| 6 | Care Access | Appointment | 6 | 6 |
| | **Total** | **14 aggregates** | **33 commands** | **33 events** |

Every event in the taxonomy has exactly one command that triggers it. Commands and events are 1:1 — a command either succeeds (producing one event) or is rejected (producing nothing).

---
---

# Part 2 — Command Handling Flow

## The Command Path

A command travels through four stages from clinical intent to persisted fact:

```
┌──────────┐     ┌──────────────────┐     ┌─────────────┐     ┌─────────────┐
│ Command  │────►│ Command Handler  │────►│  Aggregate  │────►│ Event Store │
│ (intent) │     │ (orchestration)  │     │  (decision) │     │ (fact)      │
└──────────┘     └──────────────────┘     └─────────────┘     └─────────────┘
```

### Stage 1: Command Arrives

A command is a frozen data object expressing clinical intent. It carries:
- **What**: the action requested (e.g., `MakeDiagnosis`)
- **Who**: the performer and their role
- **Where**: organization, facility, device
- **Context**: the target aggregate ID (or none for creation commands)
- **Payload**: the clinical data (e.g., condition name, ICD code)

Commands do NOT carry event metadata. They do not know about event IDs, aggregate versions, or event types. The command handler is responsible for constructing event metadata.

### Stage 2: Command Handler Orchestrates

The command handler is an **application-layer service**. It sits between the outside world (interface layer) and the domain (aggregates). It performs these steps in order:

```
1. VALIDATE the command structure (required fields, types)
2. LOAD the aggregate's event stream from the event store
3. REHYDRATE the aggregate by replaying events through its apply function
4. CHECK cross-aggregate preconditions via read models (eventually consistent)
5. EXECUTE: pass the command to the aggregate
6. RECEIVE new events from the aggregate (or a rejection)
7. PERSIST events to the event store (pipeline Stage 2)
8. DISPATCH events to projections (pipeline Stage 3+4)
9. RETURN success or failure to the caller
```

The handler **never contains domain logic**. It does not decide whether a diagnosis is valid or whether an encounter can be completed. It loads, rehydrates, delegates, and persists.

### Stage 3: Aggregate Decides

The aggregate is the **domain-layer decision maker**. When it receives a command:

1. It examines its **current state** (derived from replayed events).
2. It checks **intra-aggregate invariants** (strong consistency — INV-EP-2, INV-CJ-2, etc.).
3. If invariants hold, it produces **new event(s)**.
4. If invariants are violated, it **rejects the command** with a domain error.

The aggregate never reads from the event store, never calls infrastructure, and never queries projections. It receives its state pre-loaded and makes a pure decision.

### Stage 4: Events Become Facts

The command handler takes the events produced by the aggregate and:
1. Constructs full `EventMetadata` (event ID, aggregate version, timestamps, etc.).
2. Appends to the event store (pipeline Stage 2 — optimistic concurrency check).
3. On success: dispatches to projections and returns success.
4. On concurrency conflict: retries (reload, rehydrate, re-execute) or reports failure.

---

## How Validation Occurs

Validation happens at three distinct levels:

### Level 1: Command Structure (Command Handler)

Is the command well-formed?
- Are required fields present and correctly typed?
- Is the performer ID a valid UUID?
- Is the organization ID present?

This is **syntactic validation**. It prevents garbage from reaching the domain.

**Failure**: `CommandValidationError` — returned immediately, no aggregate loaded.

### Level 2: Cross-Aggregate Preconditions (Command Handler via Projections)

Does the cross-aggregate context allow this command?
- Is the patient active? (INV-PL-1, INV-PL-2, INV-PL-3)
- Is the encounter in the right state? (INV-CO-1, INV-CJ-1)
- Is the performer authorized? (INV-XX-2)

These are **eventually consistent checks** read from projections. Under offline operation they may be stale. The consistency model's compensation protocol handles violations discovered on sync.

**Failure**: `PreconditionFailedError` — returned before aggregate execution.

### Level 3: Domain Invariants (Aggregate)

Does the aggregate's own state allow this transition?
- Can an encounter move from checked-in to completed? No — must go through began first (INV-EP-2).
- Can a diagnosis be resolved if it's already resolved? No (INV-CJ-3).
- Can a note be cosigned by the original author? No (INV-CD-3).

These are **strongly consistent checks** against the aggregate's own event stream. They are never stale.

**Failure**: `DomainError` — returned by the aggregate, propagated by the handler.

---

## How Events Are Produced

The aggregate's `execute` method is a **pure function**:

```
execute(state, command) → list[event] | error
```

- **Input**: current aggregate state + the command.
- **Output**: a list of new events to be appended, or a domain error.
- **Purity**: it reads only state and command. It does not call I/O or modify external state.

For lifecycle aggregates, `execute` typically produces one event per command. For fact aggregates (single-event, creation-only), `execute` always produces exactly one event — the creation event.

The command handler then wraps each event with full `EventMetadata`:
- `event_id`: generated (UUID v7)
- `aggregate_version`: current stream version + 1 (+ 2, etc. if multiple events)
- `occurred_at`: from the command's clinical timestamp
- `recorded_at`: set by the event store at persist time
- `correlation_id`, `causation_id`: propagated from command context

---

## How Failures Are Handled

| Failure Type | Source | Handler Response | Retry? |
|-------------|--------|-----------------|--------|
| `CommandValidationError` | Command handler (Level 1) | Return error immediately | No — fix the command |
| `PreconditionFailedError` | Command handler (Level 2) | Return error immediately | Maybe — if projection is stale, retry after sync |
| `DomainError` | Aggregate (Level 3) | Return domain error | No — the domain forbids it |
| `ConcurrencyError` | Event store (persist) | Retry: reload, rehydrate, re-execute | Yes — automatic, up to N retries |
| Infrastructure failure | Event store / network | Return transient error | Yes — caller retries |

### Concurrency Retry Loop

When the event store rejects an append due to version mismatch (another command wrote to the same aggregate between load and persist), the handler retries:

```
for attempt in range(max_retries):
    stream = event_store.read_stream(aggregate_id)
    state = aggregate.rehydrate(stream)
    result = aggregate.execute(state, command)
    try:
        for event in result.events:
            event_store.append(event)
        return success
    except ConcurrencyError:
        continue  # retry with fresh state
raise ConcurrencyError  # all retries exhausted
```

This is safe because the aggregate's `execute` is pure — replaying with the updated state may produce different events (valid) or reject the command (also valid, if the concurrent write changed state such that the command is no longer allowed).
