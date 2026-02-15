# Aggregates, Event Streams & Consistency Model

> **Status**: Design — pending review before implementation.
> **Depends on**: `docs/clinical-event-taxonomy.md`, `docs/clinical-event-invariants-and-metadata.md`
> **Governing rules**: `.aas/architecture-rules.yaml`

---

# Part 1 — Aggregate Candidates

## Design Principles

Aggregates in this system are **transactional clinical boundaries** — the smallest unit that enforces a meaningful set of invariants. They are NOT data containers and they are NOT "one aggregate per entity."

The guiding constraints:

1. **Minimize write conflicts** — two clinicians working on the same patient at the same time should almost never contend on the same aggregate.
2. **Reflect real clinical workflows** — aggregate boundaries should map to how clinicians actually work, not how data is stored.
3. **Support offline concurrency** — a nurse recording vitals on a tablet and a doctor dictating a note on a laptop must not block each other, even if both are offline.
4. **Patient is NOT an aggregate** — "Patient" as an aggregate would mean every clinical act (vitals, notes, diagnoses, appointments) writes to one stream. A busy patient could have 5 staff members writing simultaneously. This is a conflict disaster.

### Aggregate Classification

Aggregates fall into two types based on their lifecycle:

- **Lifecycle aggregates** — multi-event, enforce internal state transitions (e.g., encounter stages, diagnosis chain). These have invariants between their own events.
- **Fact aggregates** — represent a single recorded clinical fact (e.g., one vitals measurement, one lab result). They are created with one event and never modified. They exist as aggregates for addressability, reference, and stream identity. They have **zero internal contention** because there is only one write ever.

---

## Lifecycle Aggregates

### 1. PatientRegistration

**One instance per patient.**

| Property | Value |
|----------|-------|
| **Responsibility** | Owns the patient's identity and relationship with the practice. Manages the patient's lifecycle from registration through potential death or transfer. |
| **Why it is a consistency boundary** | Identity mutations must be serialized. If two staff members simultaneously correct a patient's name and DOB, one correction must be applied before the other to produce a coherent identity state. The patient's terminal status (deceased/transferred) is a gate that blocks downstream aggregates — this gate must be consistent. Invariants INV-PL-1 through INV-PL-5 are enforced here. |
| **Expected contention** | Very low. Identity changes are rare — a patient registers once, contact info changes occasionally, corrections are exceptional. |

**Events inside this aggregate:**

| Event | Role in aggregate |
|-------|-------------------|
| `PatientRegistered` | Creation event. Establishes the aggregate. |
| `PatientIdentityCorrected` | State transition: corrects identity fields. Must be serialized against other corrections (INV-PL-4). |
| `PatientContactInfoProvided` | Append: new contact declaration. Low invariant weight but belongs here because it's part of patient identity. |
| `PatientDeceasedRecorded` | Terminal state. Gates all downstream contexts (INV-PL-2). |
| `PatientTransferredOut` | Terminal state. Gates all downstream contexts (INV-PL-3). |

**Not in this aggregate:** `PatientDuplicateIdentified` — see Special Cases below.

---

### 2. Encounter

**One instance per clinical visit.**

| Property | Value |
|----------|-------|
| **Responsibility** | Owns the encounter's lifecycle state machine — from patient arrival through discharge. Does NOT own the clinical content of the encounter (observations, diagnoses, notes). |
| **Why it is a consistency boundary** | The encounter state machine (INV-EP-2) is inherently sequential: check-in → triage → began → completed → discharged, with a reopen loop. These transitions must be ordered — you cannot complete an encounter that hasn't begun. A single practitioner drives this progression. Separating lifecycle from clinical content is the key design decision: it keeps the Encounter aggregate small and low-contention while allowing unbounded parallel clinical work. |
| **Expected contention** | Very low. One practitioner drives the state machine. Transitions are infrequent (5–6 per visit) and naturally sequential. |

**Events inside this aggregate:**

| Event | Role in aggregate |
|-------|-------------------|
| `PatientCheckedIn` | Creation event. Establishes the aggregate. Requires active patient (INV-EP-1, cross-aggregate read). |
| `PatientTriaged` | Optional transition. Must follow check-in, precede began (INV-EP-2). |
| `EncounterBegan` | Transition to active clinical state. Gates observation and judgment aggregates. |
| `EncounterCompleted` | Transition to completed state. |
| `PatientDischarged` | Transition to discharged state. |
| `EncounterReopened` | Loop back to active state for late additions. |

**Not in this aggregate:** Observations, diagnoses, procedures, notes. These are separate aggregates that *reference* this encounter but do not write to its stream.

---

### 3. Diagnosis

**One instance per diagnosed condition per patient.**

| Property | Value |
|----------|-------|
| **Responsibility** | Owns the lifecycle of a single diagnosis — from the initial clinical judgment through potential revisions to resolution. Represents the evolution of one clinical opinion about one condition. |
| **Why it is a consistency boundary** | The diagnosis chain has strict sequential invariants: you cannot revise a diagnosis that wasn't made (INV-CJ-2), you cannot resolve a diagnosis that was already resolved (INV-CJ-3). These invariants are internal to one diagnosis — they don't span across different diagnoses. Making each diagnosis its own aggregate means two doctors diagnosing two different conditions for the same patient in the same encounter write to different streams with zero contention. |
| **Expected contention** | Extremely low. A single diagnosis is typically managed by one clinician. Revisions and resolutions are rare and sequential by nature. |

**Events inside this aggregate:**

| Event | Role in aggregate |
|-------|-------------------|
| `DiagnosisMade` | Creation event. Establishes the aggregate. Requires active encounter (INV-CJ-1, cross-aggregate read). |
| `DiagnosisRevised` | State transition: updates clinical judgment. References the prior state (INV-CJ-2). |
| `DiagnosisResolved` | Terminal state: condition no longer active (INV-CJ-3). |

---

### 4. ClinicalNote

**One instance per authored note.**

| Property | Value |
|----------|-------|
| **Responsibility** | Owns the lifecycle of a single clinical note — from initial authoring through addenda and cosignature. The note, its amendments, and its attestation form one legal document chain. |
| **Why it is a consistency boundary** | Addenda must reference a specific existing note (INV-CD-2). Cosignature must reference an existing note by a different author (INV-CD-3). These invariants are internal to one note chain. Different notes for the same encounter are independent — a doctor's SOAP note and a nurse's triage note are separate aggregates with zero contention. |
| **Expected contention** | Low. The primary author writes the note, then a supervisor cosigns later. Sequential by nature. |

**Events inside this aggregate:**

| Event | Role in aggregate |
|-------|-------------------|
| `ClinicalNoteAuthored` | Creation event. Establishes the aggregate. Requires encounter context (INV-CD-1, cross-aggregate read). |
| `NoteAddendumAuthored` | Append: supplementary information linked to the original note (INV-CD-2). |
| `NoteCosigned` | Attestation: supervisor reviewed the note (INV-CD-3, cosigner ≠ author). |

---

### 5. Appointment

**One instance per scheduled appointment.**

| Property | Value |
|----------|-------|
| **Responsibility** | Owns the scheduling lifecycle of a single appointment — from request through confirmation to terminal state (completed visit, cancellation, no-show, or reschedule). |
| **Why it is a consistency boundary** | Appointment transitions have strict preconditions: confirmation requires request (INV-CA-1), cancellation requires active appointment (INV-CA-2), no-show requires confirmed + past time + no check-in (INV-CA-3), reschedule requires confirmed and active (INV-CA-4). These invariants are internal to one appointment. Different appointments for the same patient are independent streams. |
| **Expected contention** | Very low. Appointment lifecycle is driven by administrative actions that are naturally sequential (request → confirm → one terminal outcome). |

**Events inside this aggregate:**

| Event | Role in aggregate |
|-------|-------------------|
| `AppointmentRequested` | Creation event. Establishes the aggregate. Requires active patient (cross-aggregate read). |
| `AppointmentConfirmed` | Transition: practice assigns time and provider (INV-CA-1). |
| `AppointmentCancelledByPatient` | Terminal state (INV-CA-2). |
| `AppointmentCancelledByPractice` | Terminal state (INV-CA-2). |
| `PatientNoShowed` | Terminal state (INV-CA-3). |
| `AppointmentRescheduled` | Transition: moves to new time/provider, appointment stays active (INV-CA-4). |

---

### 6. AllergyRecord

**One instance per identified allergy per patient.**

| Property | Value |
|----------|-------|
| **Responsibility** | Owns the lifecycle of a single allergy — from identification through potential refutation. Represents one substance-reaction pair on one patient. |
| **Why it is a consistency boundary** | Refutation requires a prior identification for the same substance (INV-CO-3). This invariant is internal to one allergy record. Different allergies for the same patient are independent — identifying a penicillin allergy and a latex allergy are separate aggregates. |
| **Expected contention** | Negligible. Allergies are identified rarely and refuted even more rarely. |

**Events inside this aggregate:**

| Event | Role in aggregate |
|-------|-------------------|
| `AllergyIdentified` | Creation event. Establishes the aggregate. Requires active patient (INV-CO-2, cross-aggregate read). |
| `AllergyRefuted` | Terminal state: allergy determined incorrect (INV-CO-3). |

---

## Fact Aggregates

Fact aggregates are created with a single event and are never modified. They represent a clinical observation or action that was performed once and recorded. They exist as aggregates to provide:

- A unique stream address (`aggregateId`) for referencing
- A home in the event store for projection consumption
- Consistency with the "everything is an aggregate stream" model

**They have zero write contention by definition — each instance is written once.**

### 7. VitalSigns (fact)

| Property | Value |
|----------|-------|
| **Responsibility** | Records one set of vital sign measurements taken at a specific moment. |
| **Why it is a consistency boundary** | No internal invariants (single event). Exists as its own aggregate so that multiple clinicians recording vitals for the same patient at the same time create independent streams with zero contention. |
| **Precondition** | Active encounter required (INV-CO-1, cross-aggregate read). |
| **Event** | `VitalSignsRecorded` (creation, single event, aggregate complete). |

### 8. Symptom (fact)

| Property | Value |
|----------|-------|
| **Responsibility** | Records one symptom reported by the patient to a clinician. |
| **Why it is a consistency boundary** | Same rationale as VitalSigns. Each reported symptom is an independent clinical fact. |
| **Precondition** | Active encounter required (INV-CO-1, cross-aggregate read). |
| **Event** | `SymptomReported` (creation, single event, aggregate complete). |

### 9. ExaminationFinding (fact)

| Property | Value |
|----------|-------|
| **Responsibility** | Records one finding from a physical examination. |
| **Why it is a consistency boundary** | Same rationale. Each finding is independent. A doctor noting "clear lungs" and a nurse noting "tenderness in RLQ" must not conflict. |
| **Precondition** | Active encounter required (INV-CO-1, cross-aggregate read). |
| **Event** | `ExaminationFindingNoted` (creation, single event, aggregate complete). |

### 10. LabResult (fact)

| Property | Value |
|----------|-------|
| **Responsibility** | Records one laboratory or diagnostic result received for a patient. |
| **Why it is a consistency boundary** | Lab results arrive asynchronously and independently. Two results arriving simultaneously must not conflict. |
| **Precondition** | Active patient required, encounter optional (INV-CO-2, cross-aggregate read). |
| **Event** | `LabResultReceived` (creation, single event, aggregate complete). |

### 11. Procedure (fact)

| Property | Value |
|----------|-------|
| **Responsibility** | Records one procedure performed on a patient during an encounter. |
| **Why it is a consistency boundary** | Each procedure is an independent clinical act. Two procedures in the same encounter (e.g., wound irrigation + suturing) must not conflict. |
| **Precondition** | Active encounter required (INV-CJ-4, cross-aggregate read). |
| **Event** | `ProcedurePerformed` (creation, single event, aggregate complete). |

### 12. Referral (fact)

| Property | Value |
|----------|-------|
| **Responsibility** | Records a clinician's decision to refer the patient to another provider. |
| **Why it is a consistency boundary** | Each referral is an independent clinical decision. |
| **Precondition** | Active encounter required (INV-CJ-4, cross-aggregate read). |
| **Event** | `ReferralDecided` (creation, single event, aggregate complete). |

### 13. TreatmentPlan (fact)

| Property | Value |
|----------|-------|
| **Responsibility** | Records a clinician's formulated treatment plan for the patient. |
| **Why it is a consistency boundary** | Each plan is an independent clinical decision. |
| **Precondition** | Active encounter + at least one active diagnosis required (INV-CJ-5, cross-aggregate read). |
| **Event** | `TreatmentPlanFormulated` (creation, single event, aggregate complete). |

---

## Special Cases

### PatientDuplicateIdentified

This event references **two** PatientRegistration aggregates. It cannot belong to either one without creating an asymmetric dependency. It is modeled as a **process-initiating fact aggregate**:

| Property | Value |
|----------|-------|
| **Aggregate** | `DuplicateResolution` |
| **Responsibility** | Records the discovery that two patient records represent the same person, and tracks the resolution process. |
| **Event** | `PatientDuplicateIdentified` (creation). May grow to include resolution events (`DuplicateMergeCompleted`, `DuplicateMergeRejected`) as the merge workflow is designed. |
| **Precondition** | Both referenced patients must exist (INV-PL-5, two cross-aggregate reads). |

---

## Aggregate Summary

| # | Aggregate | Type | Events | Internal Invariants | Expected Contention |
|---|-----------|------|--------|--------------------|--------------------|
| 1 | PatientRegistration | lifecycle | 5 | INV-PL-2, PL-3, PL-4 | very low |
| 2 | Encounter | lifecycle | 6 | INV-EP-2 | very low |
| 3 | Diagnosis | lifecycle | 3 | INV-CJ-2, CJ-3 | extremely low |
| 4 | ClinicalNote | lifecycle | 3 | INV-CD-2, CD-3 | low |
| 5 | Appointment | lifecycle | 6 | INV-CA-1 through CA-4 | very low |
| 6 | AllergyRecord | lifecycle | 2 | INV-CO-3 | negligible |
| 7 | VitalSigns | fact | 1 | none | zero |
| 8 | Symptom | fact | 1 | none | zero |
| 9 | ExaminationFinding | fact | 1 | none | zero |
| 10 | LabResult | fact | 1 | none | zero |
| 11 | Procedure | fact | 1 | none | zero |
| 12 | Referral | fact | 1 | none | zero |
| 13 | TreatmentPlan | fact | 1 | none | zero |
| 14 | DuplicateResolution | special | 1+ | INV-PL-5 | negligible |

### Contention Analysis: Real-World Scenario

A busy encounter with 1 physician, 1 nurse, and 1 scribe working simultaneously:

```
Nurse (tablet, offline):           Doctor (laptop, offline):          Scribe (desktop, online):
─────────────────────────          ────────────────────────           ──────────────────────────
VitalSigns-{uuid-1} v1            ExaminationFinding-{uuid-4} v1    ClinicalNote-{uuid-7} v1
Symptom-{uuid-2} v1               Diagnosis-{uuid-5} v1
VitalSigns-{uuid-3} v1            TreatmentPlan-{uuid-6} v1
```

**Result: 7 aggregate instances created, 0 conflicts.** Each person writes to their own aggregate instances. The Encounter lifecycle aggregate is not touched by any of them — the doctor advanced it to `Began` earlier and it will be `Completed` later.

---
---

# Part 2 — Event Stream Strategy

## Stream Partitioning

### One Stream Per Aggregate Instance

Every aggregate instance has exactly one event stream. The stream is identified by:

```
{aggregateType}-{aggregateId}
```

Examples:
- `PatientRegistration-a1b2c3d4` — all events for patient a1b2c3d4
- `Encounter-e5f6g7h8` — all lifecycle events for encounter e5f6g7h8
- `Diagnosis-d9e0f1g2` — the diagnosis chain for diagnosis d9e0f1g2
- `VitalSigns-v3w4x5y6` — a single vitals recording (one event)

This is the **physical stream** — the unit of storage, retrieval, and concurrency control.

### No Global Ordering

There is no global sequence number across all streams. Global ordering is:
- Unnecessary — no invariant requires it.
- Impossible to maintain across offline devices.
- A scalability bottleneck.

Each stream has its own `aggregateVersion` sequence (1, 2, 3, ...). Cross-stream ordering is derived from `occurredAt` (clinical time) and `recordedAt` (system time) during projection.

### Virtual Category Streams

Projections often need "all events of a certain type" or "all events for a certain patient." These are **virtual streams** — assembled at read time by filtering the event store, not by writing to a shared physical stream.

| Virtual Stream | Contents | Used By |
|---------------|----------|---------|
| Patient timeline | All events where `patientId = X` across all aggregate types | Patient chart projection |
| Encounter content | All events where `encounterId = Y` across fact and lifecycle aggregates | Encounter summary projection |
| Category stream | All events where `aggregateType = Z` | Type-specific projections (e.g., "all diagnoses") |
| Organization stream | All events where `organizationId = W` | Org-wide analytics, compliance |

Virtual streams are assembled by index lookups, not by reading a single physical stream. This is a projection concern, not an aggregate concern.

---

## Aggregate-to-Stream Mapping

### Lifecycle Aggregates: Long-Lived Streams

Lifecycle aggregates produce streams with multiple events that grow over time:

```
Stream: PatientRegistration-{patientId}
  v1: PatientRegistered       (2026-01-15)
  v2: PatientContactInfoProvided  (2026-03-20)
  v3: PatientIdentityCorrected    (2026-06-01)
```

```
Stream: Encounter-{encounterId}
  v1: PatientCheckedIn    (2026-02-14 09:00)
  v2: PatientTriaged      (2026-02-14 09:05)
  v3: EncounterBegan      (2026-02-14 09:15)
  v4: EncounterCompleted  (2026-02-14 09:45)
  v5: PatientDischarged   (2026-02-14 09:50)
```

```
Stream: Diagnosis-{diagnosisId}
  v1: DiagnosisMade     (2026-02-14 09:30)
  v2: DiagnosisRevised  (2026-02-28 10:00)
  v3: DiagnosisResolved (2026-04-15 11:00)
```

### Fact Aggregates: Single-Event Streams

Fact aggregates produce streams with exactly one event:

```
Stream: VitalSigns-{observationId}
  v1: VitalSignsRecorded  (2026-02-14 09:08)
```

This is intentional. The stream exists to:
1. Give the fact a unique address in the event store.
2. Make it discoverable by virtual stream queries (e.g., "all vitals for patient X in encounter Y").
3. Maintain consistency with the aggregate model — every event belongs to a stream.

The overhead of one-event streams is a storage concern (addressed by infrastructure), not a domain concern.

---

## How Concurrent Doctors Avoid Conflicts

The aggregate design eliminates nearly all write conflicts through **stream isolation**. Here is the reasoning for each conflict scenario:

### Scenario 1: Multiple clinicians, same patient, same encounter

This is the most common concurrency scenario — a physician, nurse, and medical assistant all work on the same patient visit simultaneously.

**Why it doesn't conflict**: Each person's clinical actions create **new aggregate instances** (fact aggregates) or write to **different lifecycle aggregates** (their own notes, their own diagnoses). Nobody writes to the same stream.

```
Nurse:    VitalSigns-{new-uuid}  → new stream, v1
Doctor:   Diagnosis-{new-uuid}   → new stream, v1
MA:       Symptom-{new-uuid}     → new stream, v1
```

The **only shared aggregate** is the Encounter lifecycle, which is driven by one person at a time (the admitting practitioner). There is no contention.

### Scenario 2: Two doctors treating the same patient for different conditions

Dr. A diagnoses hypertension. Dr. B (consulting) diagnoses a skin lesion. Both happen in the same encounter.

**Why it doesn't conflict**: Each diagnosis is its own Diagnosis aggregate. Dr. A writes to `Diagnosis-{uuid-1}`, Dr. B writes to `Diagnosis-{uuid-2}`. Independent streams, zero contention.

### Scenario 3: One doctor revises a diagnosis while another resolves a different one

Dr. A revises the hypertension diagnosis. Dr. B resolves the skin lesion diagnosis.

**Why it doesn't conflict**: Each operates on a different Diagnosis aggregate. `DiagnosisRevised` writes to `Diagnosis-{uuid-1}`, `DiagnosisResolved` writes to `Diagnosis-{uuid-2}`. No shared stream.

### Scenario 4: Two staff members update the same patient's identity

Front desk corrects the patient's name. Simultaneously, another staff member records a new phone number.

**Why it might conflict**: Both write to `PatientRegistration-{patientId}` — the same stream. The second write will encounter a version mismatch.

**Why this is acceptable**: Identity changes are rare (a few times per patient lifetime). The conflict is detected by optimistic concurrency and the second command is retried with the updated version. This retry is invisible to the user in normal operation.

### Scenario 5: The only real conflict — concurrent encounter lifecycle transitions

Two people try to advance the encounter state simultaneously (e.g., nurse marks triage while doctor marks encounter began).

**Why it might conflict**: Both write to `Encounter-{encounterId}`.

**Why this is acceptable and correct**: The encounter state machine is sequential by design. Triage must happen before began (INV-EP-2). The optimistic concurrency check forces serialization, which is *clinically correct* — these events must be ordered. The retry resolves it.

---

## How Streams Enable Offline Synchronization

### Write Locally, Sync Later

Every device has a local event store. Events are always written locally first, immediately. The user never waits for network connectivity.

```
Device A (offline):                    Device B (offline):
  VitalSigns-{uuid-1} v1                Diagnosis-{uuid-2} v1
  Symptom-{uuid-3} v1                   ClinicalNote-{uuid-4} v1
  (outbox: 2 events pending)            (outbox: 2 events pending)
```

### Sync Unit: Individual Aggregate Stream

When connectivity returns, each device pushes its pending events **per aggregate stream**. The remote store processes them one stream at a time:

1. **New streams** (fact aggregates, new lifecycle aggregates): Accepted unconditionally — there is nothing to conflict with. Most offline events fall into this category.

2. **Existing streams, no conflict**: The event's `aggregateVersion` matches the remote store's expected next version. Accepted.

3. **Existing streams, version conflict**: The event's `aggregateVersion` does NOT match. This means another device wrote to the same lifecycle aggregate while this device was offline. Conflict resolution is triggered (see below).

### Conflict Resolution Strategy

Conflicts can only occur on **lifecycle aggregates** (PatientRegistration, Encounter, Diagnosis, ClinicalNote, Appointment, AllergyRecord). Fact aggregates never conflict because each creates a new stream.

Resolution rules:

| Aggregate | Conflict Type | Resolution |
|-----------|--------------|------------|
| **PatientRegistration** | Concurrent identity corrections | Accept both in `recordedAt` order. Both corrections are clinical facts (someone discovered an error). If they correct the same field to different values, flag for human review. |
| **Encounter** | Concurrent state transitions | Apply in `occurredAt` order. If ordering is valid per INV-EP-2, accept both. If not (e.g., two devices both emitted `EncounterCompleted`), deduplicate by `eventId`. |
| **Diagnosis** | Concurrent revision + resolution | Apply in `occurredAt` order. A revision followed by a resolution is valid. A resolution followed by a revision is rejected (cannot revise a resolved diagnosis). |
| **ClinicalNote** | Concurrent addenda | Accept both — addenda are independent. Concurrent cosignature is deduplicated. |
| **Appointment** | Concurrent cancel + reschedule | First event in `occurredAt` order wins. A cancelled appointment cannot be rescheduled. |
| **AllergyRecord** | Concurrent refutation attempts | Deduplicate — only one refutation is needed. |

### Why Fact Aggregates Make Offline Sync Easy

The majority of clinical work during an encounter is **observations, notes, and judgments** — all of which are fact aggregates or independent lifecycle aggregates. Each creates a new stream. On sync, these are new streams with no remote counterpart, so they are accepted unconditionally.

This means: **in the common case (80%+ of events), offline sync is conflict-free by design.**

Conflicts are limited to the rare case where two devices advance the same lifecycle aggregate — typically the Encounter state machine or an Appointment. These are low-frequency events that are easily resolved.

---
---

# Part 3 — Consistency Model

## The Two Consistency Domains

The system has two distinct consistency domains:

1. **Intra-aggregate** — events within a single aggregate stream
2. **Inter-aggregate** — relationships between events in different aggregate streams

These require different consistency guarantees.

---

## Strongly Consistent (Immediate, Enforced Before Event Is Emitted)

All intra-aggregate invariants are **strongly consistent**. The aggregate checks its own state before emitting an event. If the invariant would be violated, the command is rejected.

| Invariant | Aggregate | What is checked |
|-----------|-----------|-----------------|
| INV-EP-2 | Encounter | State machine ordering: current state permits the requested transition |
| INV-CJ-2 | Diagnosis | `DiagnosisRevised` — a prior `DiagnosisMade` exists in this stream |
| INV-CJ-3 | Diagnosis | `DiagnosisResolved` — diagnosis is not already resolved |
| INV-CD-2 | ClinicalNote | `NoteAddendumAuthored` — the original note exists in this stream |
| INV-CD-3 | ClinicalNote | `NoteCosigned` — original note exists and cosigner ≠ author |
| INV-CA-1 | Appointment | `AppointmentConfirmed` — a prior `AppointmentRequested` exists |
| INV-CA-2 | Appointment | Cancellation — appointment is not already cancelled |
| INV-CA-3 | Appointment | No-show — confirmed, past time, no check-in |
| INV-CA-4 | Appointment | Reschedule — confirmed, not cancelled, not no-showed |
| INV-CO-3 | AllergyRecord | `AllergyRefuted` — a prior `AllergyIdentified` exists |
| INV-PL-2 | PatientRegistration | `PatientDeceasedRecorded` — patient is not already deceased |
| INV-PL-3 | PatientRegistration | `PatientTransferredOut` — patient is not already transferred |
| INV-PL-4 | PatientRegistration | `PatientIdentityCorrected` — patient exists and is not in terminal state |
| INV-XX-3 | All | Aggregate version is sequential (optimistic concurrency) |

**Guarantee**: These invariants are **never violated**, even under offline operation. They are checked against the aggregate's own event stream, which is always locally available.

---

## Eventually Consistent (Checked at Command Time, May Be Stale)

All inter-aggregate invariants are **eventually consistent**. They are checked at command time by reading a **projection** of the referenced aggregate's state. Under offline operation, this projection may be stale.

| Invariant | What is checked | Read from |
|-----------|-----------------|-----------|
| INV-PL-1 | Patient exists | PatientRegistration projection (patient status read model) |
| INV-PL-2/3 (cross-context) | Patient is not deceased/transferred | PatientRegistration projection |
| INV-EP-1 | Patient is active | PatientRegistration projection |
| INV-EP-3 | No concurrent encounter for same patient+practitioner | Encounter projection |
| INV-CO-1 | Encounter is active (for encounter-bound observations) | Encounter projection |
| INV-CO-2 | Patient is active (for patient-level observations) | PatientRegistration projection |
| INV-CJ-1 | Encounter is active (for diagnoses) | Encounter projection |
| INV-CJ-4 | Encounter is active (for procedures, referrals) | Encounter projection |
| INV-CJ-5 | At least one active diagnosis exists | Diagnosis projection |
| INV-CD-1 | Encounter exists | Encounter projection |
| INV-XX-2 | Performer is authorized for event type | Role/authorization projection |

**Guarantee**: These invariants are checked with **best-effort** data. Under online operation, projections are current and checks are accurate. Under offline operation, projections may be stale. Violations detected on sync are handled by the compensation protocol (see below).

---

## Why Eventually Consistent Cross-Aggregate Checks Are Clinically Safe

### The Core Principle: Clinical Data Is Sacred

When an eventually consistent check passes locally but fails on sync (because the projection was stale), the system faces a choice:

- **Option A**: Reject the event, discarding the clinical data.
- **Option B**: Accept the event, flag the inconsistency, and trigger a clinical review.

**This system always chooses Option B.** A clinician's recorded observation, diagnosis, or note represents a real-world clinical fact. The system may not unilaterally discard it.

### Specific Scenarios and Why They Are Safe

#### Scenario: Vitals recorded for an encounter that was closed on another device

- **What happened**: Nurse recorded vitals offline. Meanwhile, doctor completed the encounter on another device.
- **Stale check**: Nurse's device believed the encounter was still active (INV-CO-1 passed locally).
- **On sync**: Encounter is completed, but vitals event references it.
- **Why it's safe**: The nurse physically measured the patient's blood pressure. The measurement happened. The fact that the encounter was completed seconds before or after is a timing artifact, not a clinical error.
- **Compensation**: Accept the vitals. Optionally, reopen the encounter for late documentation or link the vitals as a post-encounter addendum. Flag for clinical review.

#### Scenario: Diagnosis made for a patient who was just transferred out

- **What happened**: Doctor recorded a diagnosis offline. Meanwhile, front desk processed a transfer on another device.
- **Stale check**: Doctor's device believed the patient was active (INV-PL-2/3 passed locally).
- **On sync**: Patient is now in transferred state.
- **Why it's safe**: The doctor was examining the patient and made a clinical judgment. The transfer paperwork being filed concurrently doesn't invalidate the clinical finding. The diagnosis is medically real.
- **Compensation**: Accept the diagnosis. Flag for clinical review. The receiving practice needs this diagnosis information anyway.

#### Scenario: Appointment confirmed for a deceased patient

- **What happened**: Scheduler confirmed an appointment. Meanwhile, another staff member recorded the patient's death.
- **Stale check**: Scheduler's device believed the patient was active.
- **On sync**: Patient is deceased.
- **Why it's safe**: The appointment is now invalid, but no clinical harm occurred — it's an administrative artifact.
- **Compensation**: Accept the confirmation event (it records what the scheduler did). Automatically emit `AppointmentCancelledByPractice` as a compensating event. Notify the scheduler.

#### Scenario: Treatment plan created but the referenced diagnosis was resolved on another device

- **What happened**: Doctor formulated a treatment plan referencing a diagnosis. Meanwhile, another doctor resolved that diagnosis.
- **Stale check**: Doctor's device believed the diagnosis was active (INV-CJ-5 passed locally).
- **On sync**: The referenced diagnosis is resolved.
- **Why it's safe**: The treatment plan represents the doctor's clinical intent at the time. It's a valid medical record entry. The resolution may have been premature, or the plan may need revision.
- **Compensation**: Accept the treatment plan. Flag for clinical review with both doctors. The clinical team resolves the discrepancy — either the plan is revised or the diagnosis is re-diagnosed.

---

## Compensation Protocol

When an eventually consistent invariant is violated on sync, the system follows this protocol:

```
1. ACCEPT the event into the event store (clinical data is sacred)
2. EMIT a CompensationRequired event:
   - References the accepted event
   - References the violated invariant
   - Includes the stale vs. current state
3. CREATE a clinical review task for the appropriate staff
4. IF automatic compensation is possible (e.g., cancel appointment for deceased patient):
   - EMIT the compensating event
   - STILL create the review task (human must verify)
5. NEVER silently discard the original event
```

### Which Invariant Violations Allow Automatic Compensation?

| Violation | Auto-Compensation | Rationale |
|-----------|-------------------|-----------|
| Appointment for deceased/transferred patient | Yes — auto-cancel | No clinical ambiguity. The appointment cannot occur. |
| Encounter-bound observation after encounter closed | No — flag only | The observation is real. A clinician must decide whether to reopen the encounter or accept the timing. |
| Diagnosis for transferred patient | No — flag only | The diagnosis is clinically significant. The transfer and diagnosis may need to be communicated to the receiving practice. |
| Treatment plan referencing resolved diagnosis | No — flag only | Requires clinical judgment about whether the diagnosis should be re-activated or the plan revised. |
| Concurrent active encounters (INV-EP-3) | No — flag only | Two practitioners may have legitimately started encounters concurrently. A clinician must determine which is the correct record. |

---

## Consistency Boundary Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                     STRONG CONSISTENCY                            │
│              (enforced within aggregate boundary)                │
│                                                                  │
│  ┌──────────────┐  ┌──────────┐  ┌──────────┐  ┌────────────┐  │
│  │ PatientReg.  │  │Encounter │  │Diagnosis │  │ClinicalNote│  │
│  │  v1→v2→v3    │  │ v1→v2→v3 │  │ v1→v2→v3 │  │ v1→v2→v3   │  │
│  │  sequential  │  │ state    │  │ chain    │  │ chain      │  │
│  │  versions    │  │ machine  │  │ integrity│  │ integrity  │  │
│  └──────────────┘  └──────────┘  └──────────┘  └────────────┘  │
│                                                                  │
│  ┌────────────┐  ┌──────────────┐  ┌───────────────────────┐   │
│  │Appointment │  │AllergyRecord │  │ Fact Aggregates (x7)  │   │
│  │ lifecycle  │  │ id→refute    │  │ single event, v1 only │   │
│  └────────────┘  └──────────────┘  └───────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                              │
                 cross-aggregate reads
                      (projections)
                              │
┌─────────────────────────────────────────────────────────────────┐
│                    EVENTUAL CONSISTENCY                           │
│            (checked via projections, may be stale)               │
│                                                                  │
│  "Does patient exist?"          → PatientRegistration projection │
│  "Is patient active?"           → PatientRegistration projection │
│  "Is encounter active?"         → Encounter projection           │
│  "Does a diagnosis exist?"      → Diagnosis projection           │
│  "Any concurrent encounters?"   → Encounter projection           │
│  "Is performer authorized?"     → Role projection                │
│                                                                  │
│  On violation during sync:                                       │
│    → ACCEPT event (clinical data is sacred)                      │
│    → EMIT CompensationRequired                                   │
│    → CREATE clinical review task                                 │
│    → Auto-compensate ONLY if unambiguous (e.g., deceased cancel) │
└─────────────────────────────────────────────────────────────────┘
```
