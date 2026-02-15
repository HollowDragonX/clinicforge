# Clinical Projections

> **Status**: Design — pending review before implementation.
> **Depends on**: `docs/clinical-event-taxonomy.md`, `docs/clinical-event-invariants-and-metadata.md`, `docs/aggregates-streams-consistency.md`
> **Governing rules**: `.aas/architecture-rules.yaml`

---

## Foundational Rules

1. **Events are the only source of truth.** Projections are derived, disposable views. If a projection disagrees with the event stream, the projection is wrong.
2. **Projections can always be rebuilt** from events. Deleting every projection and replaying the entire event history must reproduce the same state.
3. **Projections exist to support human understanding.** They answer questions that clinicians, staff, and the system itself need answered — they do not govern domain logic.
4. **Projections must tolerate eventual consistency.** They may be stale during offline operation or between sync cycles. Clinical workflows are designed around this (see consistency model).
5. **Projections are NOT domain entities.** They live in the infrastructure or application layer. The domain layer has no knowledge of projections.

---

# Part 1 — Projection Types

Projections are organized by the clinical workflow they serve.

---

## Clinical Care Delivery Projections

These projections support the physician and clinical team during direct patient care.

### 1. PatientChart

**Clinical purpose**: The comprehensive longitudinal view of a single patient. When a clinician opens a patient's chart, this projection answers: "Who is this person, what are their current problems, what allergies do they have, what happened during their recent visits, and what is coming up next?"

This is the **primary clinical read model** — the most frequently accessed projection in the system.

**Events consumed**:

| Event | What it contributes |
|-------|---------------------|
| `PatientRegistered` | Creates the chart. Populates demographics, identifiers. |
| `PatientIdentityCorrected` | Updates displayed identity fields. |
| `PatientContactInfoProvided` | Updates displayed contact information. |
| `PatientDeceasedRecorded` | Marks chart as deceased. Alters display (visual indicator, blocks scheduling actions in UI). |
| `PatientTransferredOut` | Marks chart as transferred. |
| `DiagnosisMade` | Adds to active problem list section. |
| `DiagnosisRevised` | Updates the displayed diagnosis. |
| `DiagnosisResolved` | Moves diagnosis from active to resolved section. |
| `AllergyIdentified` | Adds to allergy list section. |
| `AllergyRefuted` | Removes from active allergy list, moves to refuted section. |
| `AppointmentConfirmed` | Adds to upcoming appointments section. |
| `AppointmentCancelledByPatient` | Removes from upcoming appointments. |
| `AppointmentCancelledByPractice` | Removes from upcoming appointments. |
| `AppointmentRescheduled` | Updates appointment time in upcoming section. |
| `EncounterBegan` | Adds to recent encounters section. |
| `EncounterCompleted` | Updates encounter status in recent encounters. |

**Information produced**:
- Patient identity (name, DOB, identifiers, contact info)
- Patient status (active / deceased / transferred)
- Active problem list (unresolved diagnoses)
- Active allergy list
- Recent encounters (last N, with status and practitioner)
- Upcoming appointments
- Summary statistics (total encounters, last visit date)

**Why this exists for physicians**: A physician cannot deliver safe care without knowing who the patient is, what conditions they have, and what allergies might interact with treatment. The patient chart is the single view that provides this context at the start of every clinical interaction.

---

### 2. EncounterSummary

**Clinical purpose**: Everything that happened during a single encounter. When a physician reviews a past visit or is actively conducting one, this projection answers: "What were the vitals? What symptoms did the patient report? What did the examiner find? What diagnoses were made? What was the plan?"

**Events consumed**:

| Event | What it contributes |
|-------|---------------------|
| `PatientCheckedIn` | Establishes encounter. Records arrival time. |
| `PatientTriaged` | Adds triage assessment (chief complaint, acuity). |
| `EncounterBegan` | Records start time, practitioner. |
| `VitalSignsRecorded` | Adds to vitals section (may have multiple sets per encounter). |
| `SymptomReported` | Adds to subjective/history section. |
| `ExaminationFindingNoted` | Adds to objective/exam section. |
| `DiagnosisMade` | Adds to assessment section. |
| `DiagnosisRevised` | Updates assessment section. |
| `ProcedurePerformed` | Adds to procedures section. |
| `ReferralDecided` | Adds to plan section. |
| `TreatmentPlanFormulated` | Adds to plan section. |
| `LabResultReceived` | Adds to results section (if encounter-linked). |
| `ClinicalNoteAuthored` | Adds to documentation section. |
| `NoteAddendumAuthored` | Appends to the referenced note in documentation section. |
| `NoteCosigned` | Marks note as cosigned in documentation section. |
| `EncounterCompleted` | Records end time, updates status. |
| `PatientDischarged` | Records discharge time, updates status. |
| `EncounterReopened` | Updates status back to active. |

**Information produced**:
- Encounter metadata (patient, practitioner, facility, times, status)
- Vitals (all sets recorded during this encounter)
- Subjective (symptoms, chief complaint from triage)
- Objective (examination findings)
- Assessment (diagnoses made/revised during this encounter)
- Plan (treatment plans, referrals, procedures)
- Documentation (notes with addenda and cosignature status)
- Timeline (all events in `occurredAt` order)

**Why this exists for physicians**: The encounter summary mirrors the structure of a clinical note (Subjective-Objective-Assessment-Plan). It provides the complete picture of one visit, enabling a physician to review what happened, verify that documentation is complete, and make follow-up decisions. It also supports billing by providing all the elements needed for code justification.

---

### 3. ActiveProblemList

**Clinical purpose**: All currently active (unresolved) diagnoses for a patient, across all encounters. This is checked at the start of every visit and before any prescribing decision.

**Events consumed**:

| Event | What it contributes |
|-------|---------------------|
| `DiagnosisMade` | Adds a diagnosis to the list (condition, ICD code, diagnosing clinician, date, encounter reference). |
| `DiagnosisRevised` | Updates the displayed diagnosis (new condition/code replaces old, revision history preserved). |
| `DiagnosisResolved` | Removes from active list, moves to resolved history. |

**Information produced**:
- List of active diagnoses (condition name, code, onset date, diagnosing clinician)
- Resolved diagnosis history (condition, resolution date, resolving clinician)
- Diagnosis count and duration (how long each condition has been active)

**Why this exists for physicians**: The problem list is one of the most critical clinical tools. It drives treatment decisions, drug interaction checks, referral reasoning, and billing. A physician seeing a patient for a sore throat needs to know the patient also has diabetes — because it changes management. Without an accurate, current problem list, clinical care is blind.

---

### 4. AllergyList

**Clinical purpose**: All currently active allergies for a patient. Checked before every prescribing decision and displayed prominently on the patient chart.

**Events consumed**:

| Event | What it contributes |
|-------|---------------------|
| `AllergyIdentified` | Adds an allergy (substance, reaction type, severity, identifying clinician, date). |
| `AllergyRefuted` | Removes from active list, moves to refuted history (substance, refutation reason, clinician, date). |

**Information produced**:
- Active allergy list (substance, reaction, severity)
- Refuted allergy history
- "No Known Allergies" flag (when list is empty and patient has been explicitly asked)

**Why this exists for physicians**: Prescribing a medication to a patient with a known allergy is a leading cause of preventable adverse events. This projection must be immediately available at all times. It is one of the few projections where staleness has direct patient safety implications — which is why allergies are patient-level observations (INV-CO-2) that don't require an active encounter to record.

---

### 5. VitalSignsTrend

**Clinical purpose**: Historical vital sign measurements for a patient over time. Physicians need to see whether blood pressure is trending up, weight is stable, or heart rate has changed.

**Events consumed**:

| Event | What it contributes |
|-------|---------------------|
| `VitalSignsRecorded` | Adds a data point (timestamp, values, recording clinician, encounter reference). |

**Information produced**:
- Time-series data for each vital sign type (BP, HR, temp, RR, O2 sat, weight, height)
- Per-encounter grouping (which vitals were taken during which visit)
- Trend indicators (rising, falling, stable — computed from recent data points)

**Why this exists for physicians**: Vital sign trends reveal disease progression and treatment response. A single blood pressure reading is a data point; a series of readings over months tells a clinical story. A physician managing hypertension needs to see whether the medication is working — this projection provides that answer.

---

### 6. ClinicalNoteTimeline

**Clinical purpose**: All clinical notes for a patient or a specific encounter, with their addenda and cosignature status. The medical record in narrative form.

**Events consumed**:

| Event | What it contributes |
|-------|---------------------|
| `ClinicalNoteAuthored` | Adds a note (type, content, author, timestamp, encounter reference). |
| `NoteAddendumAuthored` | Appends addendum to referenced note (content, author, timestamp). |
| `NoteCosigned` | Marks referenced note as cosigned (cosigner, timestamp). |

**Information produced**:
- Chronological list of notes (most recent first)
- Per-note: original content, addenda chain, cosignature status
- Filter dimensions: by encounter, by author, by note type, by date range
- Unsigned/uncosigned indicator (notes that still require attestation)

**Why this exists for physicians**: Clinical notes are the permanent medical record — they are legal documents. A physician reviewing a patient's history reads prior notes to understand prior reasoning. A supervisor needs to see which notes require cosignature. An auditor needs to see the complete note chain including addenda.

---

## Operational Workflow Projections

These projections support the daily operations of the clinical practice.

### 7. DailySchedule

**Clinical purpose**: A practitioner's schedule for a given day — all appointments, their status, and which patients have arrived.

**Events consumed**:

| Event | What it contributes |
|-------|---------------------|
| `AppointmentConfirmed` | Adds a time slot with patient and appointment type. |
| `AppointmentCancelledByPatient` | Removes or marks slot as cancelled-by-patient. |
| `AppointmentCancelledByPractice` | Removes or marks slot as cancelled-by-practice. |
| `AppointmentRescheduled` | Updates slot time/details. |
| `PatientCheckedIn` | Marks the appointment's patient as arrived. |
| `PatientNoShowed` | Marks the appointment as no-show. |
| `EncounterBegan` | Marks the appointment as in-progress. |
| `EncounterCompleted` | Marks the appointment as visit-complete. |
| `PatientDischarged` | Marks the appointment as fully done. |

**Information produced**:
- Time-ordered list of appointments for one practitioner on one date
- Per-slot: patient name, appointment type, time, status (confirmed / arrived / in-progress / completed / cancelled / no-show)
- Availability gaps (derived: time slots with no confirmed appointment)
- Running count (seen, remaining, cancelled, no-showed)

**Why this exists for physicians**: A physician starts the day by looking at their schedule. They need to know who is coming, who has arrived, and who is next. The schedule is also the operational backbone for the front desk, nursing staff, and practice management.

---

### 8. EncounterWorklist

**Clinical purpose**: A live, clinic-wide view of all encounters currently in progress today. Used by the entire clinical team to coordinate care.

**Events consumed**:

| Event | What it contributes |
|-------|---------------------|
| `PatientCheckedIn` | Adds patient to worklist with "waiting" status. |
| `PatientTriaged` | Updates status to "triaged." |
| `EncounterBegan` | Updates status to "in-progress," assigns practitioner. |
| `EncounterCompleted` | Updates status to "completed." |
| `PatientDischarged` | Removes from active worklist (or moves to "discharged" section). |
| `EncounterReopened` | Returns to active worklist. |

**Information produced**:
- List of all encounters for today at this facility
- Per-encounter: patient name, practitioner, status, wait time (time since check-in), duration (time since encounter began)
- Status distribution (N waiting, N in-progress, N completed)
- Alerts: patients waiting longer than threshold

**Why this exists for physicians**: In a busy clinic, the worklist is the "air traffic control" display. It shows the physician who is ready to be seen next, how long patients have been waiting, and which rooms are occupied. Nursing staff use it to coordinate triage and rooming. Practice managers use it to monitor throughput.

---

### 9. PendingCosignatures

**Clinical purpose**: Notes authored by trainees or supervised providers that require supervisory cosignature. A compliance-critical view.

**Events consumed**:

| Event | What it contributes |
|-------|---------------------|
| `ClinicalNoteAuthored` | If the author's `performerRole` requires supervision (e.g., `trainee`), adds the note to the pending list. |
| `NoteCosigned` | Removes the referenced note from the pending list. |

**Information produced**:
- List of notes awaiting cosignature, grouped by supervising physician
- Per-note: author, patient, encounter date, note type, days pending
- Overdue alerts (notes pending longer than policy threshold, e.g., 72 hours)

**Why this exists for physicians**: Medical training programs and state licensing boards require that supervisory physicians review and cosign trainee documentation within a defined timeframe. Failure to cosign is a compliance violation that can affect accreditation, billing, and malpractice coverage. This projection ensures nothing falls through the cracks.

---

## Care Coordination Projections

These projections support tracking care across encounters and over time.

### 10. ReferralLog

**Clinical purpose**: All referrals made for a patient, with their current status. Used to ensure patients follow through on specialist visits.

**Events consumed**:

| Event | What it contributes |
|-------|---------------------|
| `ReferralDecided` | Adds a referral (referred-to specialty/provider, reason, referring clinician, encounter reference, date). |

**Information produced**:
- List of referrals per patient (chronological)
- Per-referral: specialty, reason, referring clinician, date, originating encounter
- Open referral count (referrals without a subsequent encounter with the referred specialty — derived heuristic)

**Why this exists for physicians**: Referral follow-up is a major care gap. A physician refers a patient to cardiology, but the patient never schedules. Weeks later, the patient has a cardiac event. This projection tracks referral activity so the practice can identify patients who haven't followed through and intervene proactively.

---

### 11. PatientAppointmentHistory

**Clinical purpose**: Complete history of a patient's scheduling activity — requests, confirmations, cancellations, reschedules, and no-shows.

**Events consumed**:

| Event | What it contributes |
|-------|---------------------|
| `AppointmentRequested` | Adds a request record. |
| `AppointmentConfirmed` | Updates the record to confirmed (time, provider). |
| `AppointmentCancelledByPatient` | Marks as cancelled by patient. |
| `AppointmentCancelledByPractice` | Marks as cancelled by practice. |
| `PatientNoShowed` | Marks as no-show. |
| `AppointmentRescheduled` | Adds reschedule record linked to the original. |

**Information produced**:
- Complete appointment history per patient
- No-show count and rate
- Cancellation count and rate (by patient vs. by practice)
- Attendance reliability indicator

**Why this exists for physicians**: A patient's scheduling behavior is clinically relevant. A high no-show rate may indicate barriers to access (transportation, cost, health literacy). A pattern of cancellations before specialist appointments may indicate anxiety about a diagnosis. This data helps clinicians identify at-risk patients and adapt care delivery.

---

## Cross-Aggregate Read Models (Internal)

These projections are consumed by the system itself, not directly by humans. They support the eventually consistent cross-aggregate invariant checks defined in the consistency model.

### 12. PatientStatus

**Clinical purpose**: Simple read model answering: "Does this patient exist, and are they active, deceased, or transferred?"

**Events consumed**:

| Event | What it contributes |
|-------|---------------------|
| `PatientRegistered` | Sets status to `active`. |
| `PatientDeceasedRecorded` | Sets status to `deceased`. |
| `PatientTransferredOut` | Sets status to `transferred`. |

**Information produced**:
- Per patient: `patientId`, `status` (active / deceased / transferred), `since` (timestamp of last status change)

**Why this exists**: Consumed by command handlers in other bounded contexts to enforce INV-PL-1, INV-PL-2, INV-PL-3, INV-EP-1. When the Encounter context needs to know if a patient is active before accepting `PatientCheckedIn`, it reads this projection. Under offline operation, this projection may be stale — the consistency model's compensation protocol handles violations detected on sync.

---

### 13. EncounterState

**Clinical purpose**: Simple read model answering: "What state is this encounter in?"

**Events consumed**:

| Event | What it contributes |
|-------|---------------------|
| `PatientCheckedIn` | Sets state to `checked_in`. |
| `PatientTriaged` | Sets state to `triaged`. |
| `EncounterBegan` | Sets state to `active`. |
| `EncounterCompleted` | Sets state to `completed`. |
| `PatientDischarged` | Sets state to `discharged`. |
| `EncounterReopened` | Sets state to `active`. |

**Information produced**:
- Per encounter: `encounterId`, `patientId`, `practitionerId`, `state`, `since`

**Why this exists**: Consumed by command handlers in Clinical Records context to enforce INV-CO-1, INV-CJ-1, INV-CJ-4, INV-CD-1. When a fact aggregate (e.g., VitalSigns) needs to verify the encounter is active before accepting `VitalSignsRecorded`, it reads this projection.

---

## Quality & Compliance Projections

### 14. CompensationReviewQueue

**Clinical purpose**: Events that were accepted despite failing an eventually consistent invariant check on sync. These require human clinical review.

**Events consumed**:

| Event | What it contributes |
|-------|---------------------|
| `CompensationRequired` (system event) | Adds a review item: the accepted event, the violated invariant, the stale vs. current state, and auto-compensation actions taken (if any). |

**Information produced**:
- Queue of items requiring clinical review
- Per item: original event summary, invariant violated, stale state at recording time, current state, auto-compensation (if applied), review status (pending / reviewed / resolved)
- Priority sorting (patient safety implications first)

**Why this exists for physicians**: The compensation protocol (defined in the consistency model) guarantees that clinical data is never silently discarded. But when an invariant was violated due to stale data, a clinician must review whether the event is still valid in the current context. This projection surfaces those cases and tracks their resolution.

---

### 15. LateDocumentationAudit

**Clinical purpose**: Identifies clinical events where the gap between `occurredAt` (when it happened) and `recordedAt` (when it was entered) exceeds a defined threshold.

**Events consumed**:

| Event | What it contributes |
|-------|---------------------|
| All clinical events | Compares `occurredAt` vs. `recordedAt`. If gap exceeds threshold (e.g., 24 hours), adds to the audit list. |

**Information produced**:
- List of late-documented events
- Per event: type, patient, performer, `occurredAt`, `recordedAt`, gap duration
- Grouping by performer (which clinicians are consistently documenting late)
- Trend analysis (is late documentation increasing or decreasing)

**Why this exists for physicians**: Late documentation weakens the medical record's legal weight. A note written 3 days after the encounter is less credible than one written during the encounter. Regulatory bodies and malpractice insurers scrutinize documentation timeliness. This projection enables practice management to identify patterns and intervene.

---

### 16. NoShowTracker

**Clinical purpose**: Tracks no-show patterns across the patient population. Supports population health management and care gap identification.

**Events consumed**:

| Event | What it contributes |
|-------|---------------------|
| `PatientNoShowed` | Adds a no-show record (patient, appointment type, date, practitioner). |
| `AppointmentConfirmed` | Provides denominator context (total confirmed appointments). |

**Information produced**:
- No-show rate by time period, practitioner, appointment type
- Patients with ≥ N no-shows in a period (frequent no-show list)
- Correlation data: no-show patterns by day of week, time of day, appointment type

**Why this exists for physicians**: No-shows are a leading indicator of patients falling out of care. A diabetic patient who no-shows three times is at risk of uncontrolled blood sugar and hospitalization. This projection enables the practice to identify these patients and trigger outreach — a core population health function.

---

## Projection Summary

| # | Projection | Category | Key Events | Primary Consumer |
|---|-----------|----------|------------|-----------------|
| 1 | PatientChart | Care Delivery | 16 event types | Physician (chart view) |
| 2 | EncounterSummary | Care Delivery | 18 event types | Physician (visit view) |
| 3 | ActiveProblemList | Care Delivery | 3 event types | Physician (diagnosis review) |
| 4 | AllergyList | Care Delivery | 2 event types | Physician (prescribing safety) |
| 5 | VitalSignsTrend | Care Delivery | 1 event type | Physician (trend analysis) |
| 6 | ClinicalNoteTimeline | Care Delivery | 3 event types | Physician (record review) |
| 7 | DailySchedule | Operations | 9 event types | Physician + front desk |
| 8 | EncounterWorklist | Operations | 6 event types | Clinical team (coordination) |
| 9 | PendingCosignatures | Operations | 2 event types | Supervising physician |
| 10 | ReferralLog | Care Coordination | 1 event type | Physician (follow-up tracking) |
| 11 | PatientAppointmentHistory | Care Coordination | 6 event types | Physician + admin |
| 12 | PatientStatus | Internal Read Model | 3 event types | Command handlers (invariant checks) |
| 13 | EncounterState | Internal Read Model | 6 event types | Command handlers (invariant checks) |
| 14 | CompensationReviewQueue | Quality / Compliance | 1 system event type | Clinical reviewer |
| 15 | LateDocumentationAudit | Quality / Compliance | All event types | Practice management |
| 16 | NoShowTracker | Quality / Compliance | 2 event types | Practice management |

---
---

# Part 2 — Projection Processing Model

## Conceptual Pipeline

Every projection follows the same processing pipeline:

```
Event Store ──► Subscription ──► Filter ──► Handler ──► Read Model
                    │                                       │
                    └── Checkpoint ◄────────────────────────┘
```

1. **Subscription**: The projection subscribes to a source of events.
2. **Filter**: The subscription delivers only events the projection cares about (by `eventType`, `aggregateType`, `patientId`, etc.).
3. **Handler**: A pure function that takes the current read model state + an event and produces the new read model state.
4. **Checkpoint**: After processing, the projection records its position (which events it has seen), so it can resume from where it left off.

---

## How Projections Subscribe to Event Streams

Projections do NOT subscribe to individual aggregate streams. They subscribe to **virtual streams** — filtered views across the event store.

### Subscription Strategies

| Strategy | How it works | Used by |
|----------|-------------|---------|
| **By event type** | "Give me all `DiagnosisMade`, `DiagnosisRevised`, `DiagnosisResolved` events" | ActiveProblemList, AllergyList |
| **By patient** | "Give me all events where `patientId = X`" | PatientChart (for a specific patient) |
| **By encounter** | "Give me all events where `encounterId = Y`" | EncounterSummary (for a specific encounter) |
| **By organization** | "Give me all events where `organizationId = W`" | NoShowTracker, LateDocumentationAudit |
| **By aggregate type** | "Give me all events from `Encounter` aggregates" | EncounterWorklist, EncounterState |
| **Catch-all** | "Give me every event" | LateDocumentationAudit (inspects all events for timestamp gaps) |

### Live vs. On-Demand Subscriptions

Not all projections need to process events in real-time:

| Mode | Behavior | Used by |
|------|----------|---------|
| **Live (continuous)** | Processes events as they arrive. Always up to date (within eventual consistency bounds). | PatientStatus, EncounterState, EncounterWorklist, DailySchedule |
| **On-demand (lazy)** | Rebuilds when queried. Reads all relevant events at query time. Suitable for infrequently accessed views. | VitalSignsTrend (for a specific patient), PatientAppointmentHistory, LateDocumentationAudit |
| **Periodic (batch)** | Rebuilds on a schedule (e.g., nightly). Suitable for analytics that don't need real-time data. | NoShowTracker (aggregate statistics) |

Live projections maintain persistent state. On-demand projections are transient — computed, returned, discarded. Periodic projections are rebuilt from scratch on each cycle.

---

## How Projections Rebuild State

A projection's handler is a **fold function**: it takes an accumulator (current state) and an event, and returns the new state.

```
state₀ = empty
state₁ = handler(state₀, event₁)
state₂ = handler(state₁, event₂)
...
stateₙ = handler(stateₙ₋₁, eventₙ)
```

This is the same logic whether the projection is processing a live event or replaying history. There is no separate "replay mode" — the handler does not distinguish between a new event and a historical one.

### State Accumulation by Projection Type

| Projection | Accumulation pattern |
|-----------|---------------------|
| **PatientChart** | Merge: each event updates a section of a composite document. Identity events update demographics; diagnosis events update the problem list; allergy events update the allergy list. |
| **EncounterSummary** | Append + update: observations and judgments append to sections; lifecycle events update status fields. |
| **ActiveProblemList** | Add/remove: `DiagnosisMade` adds, `DiagnosisResolved` removes, `DiagnosisRevised` replaces. |
| **EncounterWorklist** | Add/update/remove: check-in adds, lifecycle events update status, discharge removes. |
| **PatientStatus** | Replace: each event overwrites the status field. Only the latest status matters. |
| **VitalSignsTrend** | Append-only: each vitals recording adds a data point. Nothing is ever removed. |

---

## How Projections Recover After Sync

When offline events arrive during synchronization, projections must incorporate them. The recovery model depends on whether the projection is order-sensitive.

### Order-Insensitive Projections

Many projections produce the same result regardless of the order events arrive in:

- **ActiveProblemList**: Adding diagnosis A then B produces the same list as B then A.
- **AllergyList**: Same — order of identification doesn't affect the current list.
- **PatientStatus**: Only the event with the latest `occurredAt` determines current status.
- **VitalSignsTrend**: Data points are displayed in `occurredAt` order regardless of arrival order.

For these projections, recovery is simple: **process the arriving events through the handler**. The result converges to the correct state.

### Order-Sensitive Projections

Some projections depend on event ordering for correct display:

- **EncounterSummary**: The timeline must show events in `occurredAt` order.
- **ClinicalNoteTimeline**: Notes and addenda must be in chronological order.
- **EncounterWorklist**: Status must reflect the latest lifecycle event.

For these projections, recovery follows this protocol:

```
1. Receive sync batch (N new events for this projection's scope)
2. Identify affected entities (which encounters, which patients)
3. For each affected entity:
   a. Read ALL events for that entity from the event store (now including synced events)
   b. Sort by occurredAt
   c. Replay through handler from empty state
   d. Replace the projection state for that entity
```

This is a **targeted rebuild** — not a full projection rebuild, but a rebuild scoped to the entities affected by the sync batch. It guarantees correctness by replaying the complete, now-consistent event sequence.

### Checkpoint Recovery

Every live projection maintains a **checkpoint**: a position marker indicating the last event it processed.

```
Checkpoint: { projectionName: "EncounterWorklist", lastProcessedEventId: "uuid-xyz", lastProcessedAt: "2026-02-14T23:30:00Z" }
```

After sync:
1. New events may have `recordedAt` timestamps earlier than the checkpoint (they were recorded on another device before the checkpoint time but arrived after).
2. The projection identifies events that fall within its scope but were not yet processed.
3. These are processed through the recovery protocol above.
4. The checkpoint advances to include the newly processed events.

---

## How Projections Remain Deterministic

A projection is **deterministic** if replaying the same events in the same order always produces the same state. This is a hard requirement — without determinism, rebuilds would produce inconsistent results.

### Rules for Deterministic Handlers

1. **No external state**: A handler may only read the current projection state and the incoming event. It must not read from other projections, the clock, random number generators, or external services.

2. **No side effects**: A handler updates the read model and nothing else. It does not emit events, send notifications, or write to external systems. Side effects belong in separate subscribers (reactors), not projections.

3. **Canonical ordering**: When processing events, the ordering key is always `occurredAt` (clinical timestamp) with `eventId` as a tiebreaker (UUID v7 is time-sortable). This guarantees that two devices replaying the same events produce the same order.

4. **Idempotent processing**: Processing the same event twice must not change the result. The handler checks `eventId` against already-processed events (via checkpoint or deduplication set) and skips duplicates.

### Determinism Test

A projection passes the determinism test if:

```
rebuild(events) == rebuild(shuffle(events))
```

For order-insensitive projections, this holds trivially. For order-sensitive projections, the canonical ordering rule (sort by `occurredAt` + `eventId` before processing) ensures it holds after the sort step.

---

## Processing Pipeline Diagram

```
                          ┌─────────────────────────────────┐
                          │          EVENT STORE             │
                          │  (append-only, source of truth)  │
                          └──────────┬──────────────────────┘
                                     │
                    ┌────────────────┼────────────────┐
                    │                │                │
              ┌─────▼─────┐   ┌─────▼─────┐   ┌─────▼─────┐
              │   Live     │   │ On-demand │   │ Periodic  │
              │Subscription│   │  Query    │   │  Batch    │
              └─────┬─────┘   └─────┬─────┘   └─────┬─────┘
                    │               │               │
              ┌─────▼─────┐   ┌─────▼─────┐   ┌─────▼─────┐
              │  Filter   │   │  Filter   │   │  Filter   │
              │ (by type, │   │ (by scope:│   │ (by org,  │
              │  patient, │   │  patient, │   │  date     │
              │  etc.)    │   │  encounter│   │  range)   │
              └─────┬─────┘   └─────┬─────┘   └─────┬─────┘
                    │               │               │
              ┌─────▼─────┐   ┌─────▼─────┐   ┌─────▼─────┐
              │  Handler  │   │  Handler  │   │  Handler  │
              │  (fold)   │   │  (fold)   │   │  (fold)   │
              └─────┬─────┘   └─────┬─────┘   └─────┬─────┘
                    │               │               │
              ┌─────▼─────┐   ┌─────▼─────┐   ┌─────▼─────┐
              │ Persistent│   │ Transient │   │ Persistent│
              │ Read Model│   │  Result   │   │ Read Model│
              │ + Chkpt   │   │ (discard) │   │ + Chkpt   │
              └───────────┘   └───────────┘   └───────────┘
```

---
---

# Part 3 — Projection Rebuild Strategy

## Core Principle: Projections Are Disposable

Every projection in this system can be **deleted entirely and rebuilt from events**. This is not an emergency recovery procedure — it is a routine operational capability that must work reliably.

The event store is permanent. Projections are ephemeral.

---

## How Projections Can Be Deleted Safely

### Why deletion is safe

A projection contains **no information that doesn't exist in the event store**. It is a cached, shaped view of events. Deleting it loses nothing — it is equivalent to clearing a cache.

### Deletion procedure

```
1. STOP the projection's subscription (stop processing new events)
2. DELETE the projection's read model state
3. DELETE the projection's checkpoint
4. The projection is now gone — queries against it will return empty/unavailable
```

No other part of the system is affected because:
- **Events are untouched** — the source of truth is intact.
- **Other projections are independent** — no projection reads from another projection.
- **Aggregates don't know projections exist** — the domain layer has no dependency on projections.
- **Cross-aggregate read models** (PatientStatus, EncounterState) are projections too — if deleted, command handlers will fail their eventually consistent checks, which means commands are temporarily rejected until the projection is rebuilt. This is a brief availability impact, not a correctness issue.

### What about internal read models?

The PatientStatus and EncounterState projections are used by command handlers for invariant checks. If these are deleted:

- Commands that require cross-aggregate checks will be temporarily **rejected** (the check cannot be performed).
- No incorrect events will be emitted (fail-closed behavior).
- Once rebuilt, commands resume normally.

This is the correct behavior: **unavailability is preferable to incorrect data**.

---

## How Projections Are Rebuilt From Event History

### Full Rebuild

A full rebuild replays the entire event history through the projection handler:

```
1. DELETE the projection (state + checkpoint)
2. QUERY the event store for all events matching the projection's subscription filter
3. SORT events by occurredAt + eventId (canonical order)
4. INITIALIZE the projection state to empty
5. FOR each event in sorted order:
     state = handler(state, event)
6. SAVE the projection state
7. SET the checkpoint to the last processed event
8. RESUME live subscription from the checkpoint
```

After step 8, the projection is fully caught up and processing new events as they arrive.

### Incremental Rebuild (From Snapshot)

For large projections, full rebuild from event zero may be slow. Incremental rebuild uses a **snapshot**:

```
1. LOAD the last known-good snapshot of the projection state
2. LOAD the snapshot's checkpoint (the event position at snapshot time)
3. QUERY the event store for events AFTER the checkpoint
4. SORT and process through the handler (same as full rebuild steps 3–8)
```

Snapshots are periodic copies of the projection state + checkpoint. They are an optimization, not a requirement. If a snapshot is corrupted or unavailable, the system falls back to full rebuild.

### Targeted Rebuild (Per Entity)

After sync, a projection may only need to rebuild the portion affected by newly arrived events:

```
1. IDENTIFY the affected entity (e.g., patientId = X)
2. QUERY the event store for all events matching the projection's filter AND the entity
3. SORT by occurredAt + eventId
4. REBUILD only that entity's portion of the read model
5. REPLACE in the projection state
```

This is faster than a full rebuild because it processes a fraction of the events.

---

## Why Rebuildability Is Essential in Clinical Systems

### 1. Schema Evolution

Projections evolve as clinical workflows change. A new field is added to the EncounterSummary (e.g., "encounter duration"). The new projection handler extracts this from `EncounterBegan` and `EncounterCompleted` timestamps. To populate the field for historical encounters, the projection is rebuilt. Without rebuildability, historical data would be permanently incomplete.

**This happens routinely** — projection schemas change far more often than event schemas. Rebuildability makes projection evolution a zero-risk operation.

### 2. Bug Fixes

A projection handler has a bug: it miscounts no-shows because it doesn't filter out cancelled-then-rescheduled appointments. The bug is fixed, and the projection is rebuilt. All historical data is now correct.

Without rebuildability, the bug's effects would be **permanent**. In a clinical system, incorrect data in the problem list or allergy list could contribute to adverse patient outcomes. Rebuildability makes data correction routine.

### 3. New Projections

A new clinical workflow is introduced (e.g., chronic disease management requires a "Care Gap" projection that identifies patients overdue for follow-up). The new projection handler is deployed and rebuilt from the full event history. It immediately has complete historical data from day one.

Without rebuildability, new projections would only have data from the moment they were deployed. In clinical systems, historical context is essential — a care gap analysis that only looks at the last week is useless.

### 4. Regulatory Compliance

Regulators or auditors may request a report that requires data to be assembled in a way the system doesn't currently support. A purpose-built projection can be created, rebuilt from events, and used to produce the report. After the audit, it can be deleted.

Without rebuildability, the practice would need to manually reconstruct data from raw event logs — an error-prone, expensive process.

### 5. Disaster Recovery

A projection's storage is corrupted (disk failure, database corruption, operator error). Rebuild from events restores it completely with no data loss.

Without rebuildability, projection corruption would mean **permanent data loss**. In a clinical system, this is unacceptable.

### 6. Offline Device Recovery

A device's local projections diverge from reality after extended offline operation and a complex sync. Rather than attempting incremental correction (which may have edge cases), the projections are rebuilt from the now-synchronized event store. This guarantees convergence.

---

## Rebuild Safety Properties

| Property | Guarantee |
|----------|-----------|
| **Lossless** | Rebuild produces the same state as processing events live from the start. No data is lost. |
| **Deterministic** | Given the same events, the rebuild always produces the same result (see determinism rules in Part 2). |
| **Idempotent** | Running a rebuild twice produces the same result as running it once. |
| **Independent** | Rebuilding one projection does not affect any other projection. |
| **Non-destructive** | Rebuild reads from the event store. It never modifies, deletes, or reorders events. The source of truth is never at risk. |

---

## Rebuild Strategy Summary

```
                    EVENT STORE (permanent, immutable)
                              │
             ┌────────────────┼────────────────┐
             │                │                │
       ┌─────▼─────┐   ┌─────▼─────┐   ┌─────▼─────┐
       │   Full     │   │Incremental│   │ Targeted  │
       │  Rebuild   │   │  Rebuild  │   │  Rebuild  │
       │            │   │           │   │           │
       │ All events │   │ Snapshot  │   │ Events    │
       │ from t=0   │   │ + delta   │   │ for one   │
       │            │   │ events    │   │ entity    │
       └─────┬─────┘   └─────┬─────┘   └─────┬─────┘
             │                │                │
             └────────────────┼────────────────┘
                              │
                        ┌─────▼─────┐
                        │ Rebuilt   │
                        │ Read Model│
                        │ (correct) │
                        └───────────┘

When to use:
  Full:        schema change, bug fix, new projection, disaster recovery
  Incremental: routine catch-up after long downtime, large projections
  Targeted:    after sync, for specific entities affected by new events
```
