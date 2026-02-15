# Clinical Event Invariants & Mandatory Metadata

> **Status**: Design — pending review before implementation.
> **Depends on**: `docs/clinical-event-taxonomy.md`
> **Governing rules**: `.aas/architecture-rules.yaml`

---

# Part 1 — Clinical Invariants

Invariants are rules the system must **never** allow to be violated, regardless of network state, device, or user action. They are enforced by aggregates before an event is emitted. If an invariant would be violated, the command is **rejected** — no event is produced.

---

## 1. Patient Lifecycle Invariants

### INV-PL-1: Patient must exist before any reference

> No event in any category may reference a `patientId` for which `PatientRegistered` has not been emitted.

**Reasoning**: A patient is a real person who presented to the practice. Clinical observations, encounters, and appointments cannot exist for a person the practice has never seen. Without this invariant, orphaned clinical data could accumulate with no legal owner.

**Events enforced**: Every event that carries a `patientId` depends on a prior `PatientRegistered` for that ID.

---

### INV-PL-2: Deceased patients cannot enter new care

> After `PatientDeceasedRecorded`, the system must reject: `EncounterBegan`, `AppointmentRequested`, `AppointmentConfirmed`.

**Reasoning**: A deceased patient cannot present for care. Allowing new encounters or appointments after death recording would produce a logically impossible clinical timeline. Existing in-progress encounters at time of death recording may be completed (documentation of final care) but no new ones may begin.

**Events enforced**:
- `PatientDeceasedRecorded` → blocks → `EncounterBegan`, `AppointmentRequested`, `AppointmentConfirmed`
- `PatientDeceasedRecorded` → does NOT block → `EncounterCompleted`, `ClinicalNoteAuthored` (documentation of care already delivered)

---

### INV-PL-3: Transferred-out patients cannot enter new care at this practice

> After `PatientTransferredOut`, the system must reject: `EncounterBegan`, `AppointmentRequested`, `AppointmentConfirmed`.

**Reasoning**: A transferred patient has formally left this practice's care. New clinical interactions would occur at the receiving practice. Like deceased patients, in-progress documentation may be completed.

**Events enforced**: Same blocking pattern as INV-PL-2.

---

### INV-PL-4: Identity correction requires a registered patient

> `PatientIdentityCorrected` requires a prior `PatientRegistered` for the same `patientId`, and the patient must not be in a terminal state (deceased/transferred).

**Reasoning**: You cannot correct the identity of a person who was never registered. Corrections to deceased or transferred patients are handled through administrative processes outside the clinical event stream.

**Events enforced**: `PatientIdentityCorrected` depends on `PatientRegistered`.

---

### INV-PL-5: Duplicate identification requires two distinct registered patients

> `PatientDuplicateIdentified` must reference two different `patientId` values, both of which have a prior `PatientRegistered`.

**Reasoning**: A duplicate is the discovery that two records represent one person. Both records must exist for the discovery to be meaningful. Referencing the same ID twice or a non-existent ID is logically void.

**Events enforced**: `PatientDuplicateIdentified` depends on two distinct `PatientRegistered` events.

---

## 2. Encounter Progression Invariants

### INV-EP-1: Encounter requires an active patient

> `PatientCheckedIn` (the entry point to encounter progression) requires a `patientId` with `PatientRegistered` and no subsequent `PatientDeceasedRecorded` or `PatientTransferredOut`.

**Reasoning**: Only living, active patients of this practice can present for encounters. This is the cross-context enforcement point between Patient Identity and Clinical Encounter.

**Events enforced**: `PatientCheckedIn` depends on `PatientRegistered` (via ACL lookup of patient status).

---

### INV-EP-2: Encounter stages must follow clinical ordering

> The encounter state machine enforces this progression:
>
> ```
> PatientCheckedIn → PatientTriaged* → EncounterBegan → EncounterCompleted → PatientDischarged
>                                                              ↑                    │
>                                                              └── EncounterReopened ┘
> ```
>
> `*` Triage is optional (not all encounters require it), but when present, it must occur after check-in and before encounter began.

**Reasoning**: These stages reflect the physical reality of a clinical visit. A practitioner cannot begin an encounter with a patient who hasn't arrived. A patient cannot be discharged from an encounter that hasn't concluded. Violating this ordering would produce a clinically nonsensical timeline.

**Specific sub-rules**:
- `PatientTriaged` requires prior `PatientCheckedIn`, no prior `EncounterBegan` for this encounter.
- `EncounterBegan` requires prior `PatientCheckedIn`.
- `EncounterCompleted` requires prior `EncounterBegan`.
- `PatientDischarged` requires prior `EncounterCompleted`.
- `EncounterReopened` requires prior `EncounterCompleted` or `PatientDischarged`.

---

### INV-EP-3: No concurrent active encounters for the same patient with the same practitioner

> A patient may not have two encounters in `Began` state simultaneously with the same practitioner.

**Reasoning**: A single practitioner cannot be clinically interacting with the same patient in two separate encounters at the same time. This is physically impossible and would corrupt billing, documentation, and audit trails. Different practitioners may see the same patient concurrently (e.g., ER physician + consulting specialist).

**Events enforced**: `EncounterBegan` is rejected if an open encounter already exists for the same `patientId` + `practitionerId` pair.

---

## 3. Clinical Observation Invariants

### INV-CO-1: Encounter-bound observations require an active encounter

> `VitalSignsRecorded`, `SymptomReported`, `ExaminationFindingNoted` require an encounter in `Began` or `Reopened` state.

**Reasoning**: Vitals, symptoms, and exam findings are produced during a clinical interaction. Recording them outside an encounter context makes them unattributable — they lack the clinical context (who was present, what was the reason for visit) required for medical and legal interpretation.

**Events enforced**: These three events depend on a prior `EncounterBegan` (or `EncounterReopened`) with no subsequent `EncounterCompleted` for the same encounter.

---

### INV-CO-2: Patient-level observations require only an active patient

> `AllergyIdentified`, `AllergyRefuted`, `LabResultReceived` require a registered, active patient but do NOT require an active encounter.

**Reasoning**: Allergies can be discovered outside an encounter (e.g., patient calls to report a reaction). Lab results arrive asynchronously and may be received when no encounter is active. These observations attach to the patient's longitudinal record, not to a specific encounter, though they may optionally reference one.

**Events enforced**: These events depend on `PatientRegistered` with no terminal state.

---

### INV-CO-3: Allergy refutation requires a prior allergy identification

> `AllergyRefuted` must reference a prior `AllergyIdentified` for the same substance/agent on the same patient.

**Reasoning**: You cannot refute an allergy that was never recorded. Refutation is a clinical act — a clinician determined a previously identified allergy is incorrect. Without the original identification, the refutation has no clinical meaning.

**Events enforced**: `AllergyRefuted` depends on `AllergyIdentified` (matched by `patientId` + substance).

---

## 4. Clinical Judgment Invariants

### INV-CJ-1: Diagnoses require an active encounter

> `DiagnosisMade` requires an encounter in `Began` or `Reopened` state.

**Reasoning**: A diagnosis is a clinician's professional judgment made during a clinical interaction. It requires the context of an encounter — who made the diagnosis, under what circumstances, based on what observations. Diagnoses outside encounters are medically and legally unattributable.

**Events enforced**: `DiagnosisMade` depends on an active encounter.

---

### INV-CJ-2: Diagnosis revision requires a prior diagnosis

> `DiagnosisRevised` must reference a prior `DiagnosisMade` event (by its `eventId`) for the same patient.

**Reasoning**: Revision is the act of changing a clinical opinion. There must be an original opinion to change. The reference to the prior event creates an auditable chain of clinical reasoning — essential for understanding why a treatment plan changed.

**Events enforced**: `DiagnosisRevised` depends on a specific prior `DiagnosisMade`.

---

### INV-CJ-3: Diagnosis resolution requires an active, unresolved diagnosis

> `DiagnosisResolved` must reference a prior `DiagnosisMade` (or `DiagnosisRevised`) that has not already been resolved.

**Reasoning**: You cannot resolve a condition that was never diagnosed, and you cannot resolve it twice. A resolved diagnosis represents a clinician's determination that the condition is no longer active. Double-resolution would corrupt problem lists and billing.

**Events enforced**: `DiagnosisResolved` depends on `DiagnosisMade` with no prior `DiagnosisResolved` for the same diagnosis chain.

---

### INV-CJ-4: Procedures and referrals require an active encounter

> `ProcedurePerformed` and `ReferralDecided` require an encounter in `Began` or `Reopened` state.

**Reasoning**: Procedures are physically performed during encounters. Referrals are clinical decisions made in the context of evaluating a patient. Both require the encounter context for attribution, billing, and medical-legal documentation.

**Events enforced**: Both depend on an active encounter.

---

### INV-CJ-5: Treatment plan requires at least one diagnosis

> `TreatmentPlanFormulated` requires at least one active (unresolved) `DiagnosisMade` for the same patient within the same encounter.

**Reasoning**: A treatment plan is a response to a clinical problem. A plan with no diagnosis is clinically unanchored — there is nothing being treated. This invariant ensures every treatment plan is traceable to a specific clinical finding.

**Events enforced**: `TreatmentPlanFormulated` depends on at least one `DiagnosisMade` in the encounter's event stream.

---

## 5. Clinical Documentation Invariants

### INV-CD-1: Clinical notes require an encounter context

> `ClinicalNoteAuthored` must reference an `encounterId` for which an encounter exists (any state from `Began` onward, including `Completed` or `Reopened`).

**Reasoning**: Clinical notes document what happened during an encounter. A note without an encounter is an unattributed narrative with no clinical context. Notes may be written after an encounter completes (late documentation is common), but the encounter must exist.

**Events enforced**: `ClinicalNoteAuthored` depends on at least `EncounterBegan` having occurred for the referenced encounter.

---

### INV-CD-2: Addenda must reference an existing note

> `NoteAddendumAuthored` must reference a prior `ClinicalNoteAuthored` event (by its `eventId` or `noteId`).

**Reasoning**: An addendum supplements an existing note. Without the original note, the addendum has no context. The reference creates a linked chain — the original note plus all addenda form the complete documentation for that encounter.

**Events enforced**: `NoteAddendumAuthored` depends on a specific `ClinicalNoteAuthored`.

---

### INV-CD-3: Cosignature must reference an existing note by a different author

> `NoteCosigned` must reference a prior `ClinicalNoteAuthored`, and the cosigner's `performedBy` must differ from the original note's `performedBy`.

**Reasoning**: Cosignature is a supervisory act — a senior clinician attesting to a junior's work. Self-cosignature is meaningless and would circumvent supervision requirements mandated by training programs and state medical boards.

**Events enforced**: `NoteCosigned` depends on `ClinicalNoteAuthored` where `cosigner ≠ originalAuthor`.

---

## 6. Care Access Invariants

### INV-CA-1: Confirmation requires a prior request

> `AppointmentConfirmed` must reference a prior `AppointmentRequested` for the same `appointmentId`.

**Reasoning**: Confirmation is the practice's acknowledgment that a requested appointment will happen. Without a request, there is nothing to confirm. This two-step model captures the real-world flow: patient asks → practice assigns time/provider.

**Events enforced**: `AppointmentConfirmed` depends on `AppointmentRequested`.

---

### INV-CA-2: Cancellation requires a non-cancelled appointment

> `AppointmentCancelledByPatient` and `AppointmentCancelledByPractice` require a prior `AppointmentRequested` or `AppointmentConfirmed` with no prior cancellation event for the same `appointmentId`.

**Reasoning**: You cannot cancel an appointment that doesn't exist or was already cancelled. Double-cancellation would corrupt scheduling projections and could trigger duplicate notification workflows.

**Events enforced**: Cancellation events depend on an active (non-cancelled) appointment.

---

### INV-CA-3: No-show requires a confirmed appointment whose time has passed

> `PatientNoShowed` requires a prior `AppointmentConfirmed` and the clinical timestamp must be at or after the appointment's scheduled time. There must be no `PatientCheckedIn` for this appointment.

**Reasoning**: A no-show is the factual determination that a patient failed to appear for a confirmed appointment. It cannot be declared before the appointment time (the patient might still arrive), and it cannot apply to an unconfirmed appointment or one where the patient checked in.

**Events enforced**: `PatientNoShowed` depends on `AppointmentConfirmed`, requires `occurredAt ≥ scheduledTime`, and requires absence of `PatientCheckedIn` for the same appointment.

---

### INV-CA-4: Rescheduling requires a confirmed, active appointment

> `AppointmentRescheduled` requires a prior `AppointmentConfirmed` with no prior cancellation or no-show for the same `appointmentId`.

**Reasoning**: Rescheduling moves an existing commitment to a new time. A cancelled appointment is dead — a new request must be made. A no-showed appointment is a historical fact — it cannot be retroactively moved.

**Events enforced**: `AppointmentRescheduled` depends on `AppointmentConfirmed` with no terminal events.

---

## Cross-Category Invariants

### INV-XX-1: Temporal consistency

> For any event, `occurredAt` (clinical timestamp) must not be in the future relative to the device's clock at recording time, with a tolerance of **5 minutes** (to account for clock drift across devices).

**Reasoning**: Clinical events describe things that happened. An event timestamped in the future is logically impossible and would corrupt all time-ordered projections. The 5-minute tolerance accommodates minor device clock discrepancies in offline-first environments.

---

### INV-XX-2: Performer must be authorized for the event type

> The `performedBy` actor must hold a role that is authorized to emit the event type. For example, only licensed clinicians may emit `DiagnosisMade`; administrative staff may emit `PatientCheckedIn`.

**Reasoning**: Scope-of-practice rules are legally mandated. An administrative assistant cannot make a diagnosis. A medical student cannot cosign a note. Enforcing this at the invariant level prevents clinically invalid records before they enter the event stream.

---

### INV-XX-3: Aggregate version must be sequential

> Within any aggregate's event stream, each event's `aggregateVersion` must be exactly `previousVersion + 1`.

**Reasoning**: Sequential versioning is the foundation of optimistic concurrency control. It detects concurrent writes to the same aggregate and prevents lost events. During offline sync, version conflicts trigger the conflict resolution protocol rather than silent data loss.

---

## Invariant–Event Dependency Map

```
PatientRegistered ◄───────── ALL events (INV-PL-1)
       │
       ├──► PatientIdentityCorrected (INV-PL-4)
       ├──► PatientContactInfoProvided (INV-PL-1)
       ├──► PatientDeceasedRecorded (INV-PL-1)
       │         └──► BLOCKS: EncounterBegan, AppointmentRequested,
       │                      AppointmentConfirmed (INV-PL-2)
       ├──► PatientTransferredOut (INV-PL-1)
       │         └──► BLOCKS: same as deceased (INV-PL-3)
       └──► PatientDuplicateIdentified (INV-PL-5, requires 2 patients)

PatientCheckedIn ◄───── requires active patient (INV-EP-1)
       │
       ├──► PatientTriaged (INV-EP-2, optional)
       │
       └──► EncounterBegan (INV-EP-2)
                │
                ├──► VitalSignsRecorded (INV-CO-1)
                ├──► SymptomReported (INV-CO-1)
                ├──► ExaminationFindingNoted (INV-CO-1)
                ├──► DiagnosisMade (INV-CJ-1)
                │       ├──► DiagnosisRevised (INV-CJ-2)
                │       ├──► DiagnosisResolved (INV-CJ-3)
                │       └──► TreatmentPlanFormulated (INV-CJ-5)
                ├──► ProcedurePerformed (INV-CJ-4)
                ├──► ReferralDecided (INV-CJ-4)
                │
                └──► EncounterCompleted (INV-EP-2)
                        │
                        ├──► PatientDischarged (INV-EP-2)
                        ├──► ClinicalNoteAuthored (INV-CD-1)
                        │       ├──► NoteAddendumAuthored (INV-CD-2)
                        │       └──► NoteCosigned (INV-CD-3)
                        │
                        └──► EncounterReopened (INV-EP-2)
                                └──► (re-enters EncounterBegan flow)

AppointmentRequested ◄───── requires active patient (INV-PL-1, INV-PL-2, INV-PL-3)
       │
       └──► AppointmentConfirmed (INV-CA-1)
               │
               ├──► AppointmentCancelledByPatient (INV-CA-2)
               ├──► AppointmentCancelledByPractice (INV-CA-2)
               ├──► PatientNoShowed (INV-CA-3)
               └──► AppointmentRescheduled (INV-CA-4)

AllergyIdentified ◄───── requires active patient only (INV-CO-2)
       └──► AllergyRefuted (INV-CO-3)

LabResultReceived ◄───── requires active patient only (INV-CO-2)
```

---
---

# Part 2 — Mandatory Event Metadata

Every clinical event, regardless of category, must carry the following metadata fields. These are not optional. They are written once when the event is created and are **immutable** thereafter.

---

## Identity Fields

### `eventId` — Globally unique event identifier

**Type**: UUID v7 (time-sortable)

**Why it exists**: Every event must be individually addressable. Required for:
- **Deduplication** during offline sync — the same event arriving twice from different sync cycles must be recognized as one event.
- **Referencing** — `DiagnosisRevised` must point to the specific `DiagnosisMade` it revises. `NoteAddendumAuthored` must point to the specific note. Without a stable ID, these references break.
- **Idempotency** — command handlers can detect reprocessing of already-applied events.

UUID v7 is chosen over v4 because its embedded timestamp enables chronological sorting without requiring a separate index.

---

### `eventType` — Fully qualified event type name

**Type**: String (e.g., `clinical.encounter.EncounterBegan`)

**Why it exists**: The event store is append-only and heterogeneous. Consumers must know what schema to apply when deserializing. The type name drives:
- **Routing** — event bus subscribers filter by type.
- **Projection mapping** — read model handlers register for specific types.
- **Schema evolution** — combined with `schemaVersion`, the type enables upcasting.

The dot-separated namespace (`category.context.EventName`) prevents collisions as the system grows.

---

### `schemaVersion` — Event payload schema version

**Type**: Integer (starting at 1)

**Why it exists**: Event stores are forever — events written today must be readable in 10 years. As event payloads evolve (fields added, renamed, restructured), the schema version tells deserializers which shape to expect. This enables **upcasting**: transforming old event shapes into current shapes at read time without modifying stored data.

---

## Aggregate Fields

### `aggregateId` — Identity of the owning aggregate

**Type**: UUID

**Why it exists**: Events belong to aggregates (Patient, Encounter, Appointment, etc.). The aggregate ID partitions the event stream — all events for a single aggregate form a causally ordered sequence. Required for:
- **Stream retrieval** — rehydrating an aggregate means loading all events for its ID.
- **Consistency boundary** — invariants are checked within a single aggregate's stream.
- **Conflict detection** — offline sync conflicts are detected per aggregate.

---

### `aggregateType` — Type of the owning aggregate

**Type**: String (e.g., `Encounter`, `Patient`, `Appointment`)

**Why it exists**: The same event store may hold events for multiple aggregate types. The aggregate type enables:
- **Stream filtering** without deserializing events.
- **Projection routing** — different projections subscribe to different aggregate types.
- **Monitoring** — operational dashboards can report event rates per aggregate type.

---

### `aggregateVersion` — Sequence number within the aggregate stream

**Type**: Integer (monotonically increasing, starting at 1)

**Why it exists**: Enforces **INV-XX-3** (sequential versioning). This is the foundation of optimistic concurrency. When appending an event, the expected version is checked against the stored version. A mismatch means another event was written concurrently — the command must be retried or rejected. During offline sync, version gaps indicate missing events.

---

## Temporal Fields

### `occurredAt` — Clinical timestamp (when it happened in reality)

**Type**: ISO 8601 with timezone (e.g., `2026-02-14T17:30:00-06:00`)

**Why it exists**: This is the **medically and legally significant time**. A blood pressure measured at 14:00 but synced at 18:00 happened at 14:00. All clinical reasoning, billing, audit trails, and malpractice timelines reference this timestamp. It must include timezone because a practice with multiple facilities may span zones, and a telehealth encounter may cross zones.

This is the clinician's assertion of when the real-world event occurred.

---

### `recordedAt` — System timestamp (when the event was persisted locally)

**Type**: ISO 8601 with timezone

**Why it exists**: Distinct from `occurredAt` because offline-first architecture creates an inherent gap between "when it happened" and "when it was written down." Required for:
- **Sync ordering** — events are synced in `recordedAt` order.
- **Audit trail** — regulators may ask "when was this entered into the system?" separately from "when did it happen?"
- **Late documentation detection** — a large gap between `occurredAt` and `recordedAt` flags late entries, which have different legal weight.

---

## Actor Fields

### `performedBy` — Identity of the person who performed the clinical act

**Type**: UUID (references a practitioner/staff identity)

**Why it exists**: **Non-negotiable for healthcare.** Every clinical event must be attributable to a specific human. Required by:
- **HIPAA** — access and modification audit trails.
- **Medical licensing** — scope-of-practice enforcement (INV-XX-2).
- **Malpractice** — determining who did what and when.
- **Supervision** — identifying which events require cosignature.

This is NOT "who pressed the button" (that's the system user) — it's "who performed the clinical act." In most cases they are the same; in scribe workflows they differ.

---

### `performerRole` — Clinical role at time of event

**Type**: Enum (e.g., `physician`, `nurse`, `medical_assistant`, `admin_staff`, `trainee`)

**Why it exists**: The same person may hold different roles over time (e.g., a resident becomes an attending). The role **at the time of the event** determines:
- **Scope-of-practice validity** — was this person authorized to perform this act?
- **Supervision requirements** — trainee notes require cosignature.
- **Billing implications** — services rendered by a physician vs. a nurse practitioner may be billed differently.

Stored as a snapshot at event time, not a reference to a mutable role assignment.

---

## Organizational Context Fields

### `organizationId` — The practice or organization

**Type**: UUID

**Why it exists**: Multi-tenancy and data isolation. A single ClinicForge deployment may serve multiple practices. Events from Practice A must never leak into Practice B's projections. Also required for:
- **Regulatory jurisdiction** — different organizations may be in different states with different rules.
- **Billing entity** — the organization is the billing entity for insurance claims.

---

### `facilityId` — The physical location where the event occurred

**Type**: UUID

**Why it exists**: An organization may operate multiple facilities (main office, satellite clinic, telehealth hub). Facility matters for:
- **Regulatory compliance** — some regulations are facility-specific (e.g., CLIA for lab work).
- **Resource management** — which facility's equipment/rooms are in use.
- **Care coordination** — knowing where the patient was seen aids referral and follow-up routing.

For telehealth encounters, this references a virtual facility identifier.

---

## Device & Sync Fields

### `deviceId` — The originating device

**Type**: String (device fingerprint or registered device UUID)

**Why it exists**: Core to offline-first architecture. Required for:
- **Conflict resolution** — when two devices emit events for the same aggregate, the conflict resolver needs to know the origin.
- **Security auditing** — detecting unauthorized devices.
- **Replay isolation** — if a device's local store is corrupted, its events can be identified and quarantined without affecting other devices.

---

### `connectionStatus` — Online or offline at time of recording

**Type**: Enum (`online`, `offline`)

**Why it exists**: Events recorded offline have different trust characteristics. They may arrive out of order, reference stale state, or conflict with events from other devices. Marking this at creation time enables:
- **Conflict resolution policies** — offline events may receive different merge treatment.
- **Audit flagging** — regulators or quality reviewers can filter for offline-recorded events that may need extra scrutiny.
- **Sync debugging** — understanding why an event arrived late.

---

## Traceability Fields

### `correlationId` — Shared ID linking a causal chain of events across aggregates

**Type**: UUID

**Why it exists**: Clinical workflows span multiple aggregates and bounded contexts. A single patient visit produces events across Scheduling, Encounter, Records, and Documentation. The correlation ID groups them:
- **End-to-end tracing** — "show me everything that happened as a result of this appointment."
- **Debugging** — when a projection is wrong, trace the full chain.
- **Analytics** — measuring time from `AppointmentConfirmed` to `PatientDischarged`.

Typically set by the first command in a workflow and propagated to all downstream events.

---

### `causationId` — The specific event that directly caused this one

**Type**: UUID (references another `eventId`) — nullable for root events

**Why it exists**: While `correlationId` groups a broad chain, `causationId` captures the **direct parent**. This enables:
- **Event graph reconstruction** — building a DAG of causation.
- **Selective replay** — if a causing event is found to be invalid, its downstream effects can be identified.
- **Debugging precision** — "this projection broke because of this specific event."

Root events (e.g., `PatientRegistered` from a walk-in) have no causation and this field is null.

---

## Visibility Field

### `visibility` — Who is permitted to see this event

**Type**: Enum set (e.g., `[clinical_staff]`, `[clinical_staff, patient]`, `[clinical_staff, billing]`)

**Why it exists**: Not all clinical events should be visible to all consumers.
- **Psychiatric and behavioral health notes** have restricted visibility under 42 CFR Part 2 — even other clinicians may not see them without explicit consent.
- **Patient portal projections** should only include patient-appropriate events (e.g., `AppointmentConfirmed` yes, `DiagnosisMade` only after clinical review).
- **Billing projections** need access to procedures and diagnoses but not raw clinical notes.

Visibility is set at event creation time based on the event type and context. It is metadata, not a mutable access control list — changing who can see an event requires a new compensating event, not mutation.

---

## Metadata Summary Table

| Field | Type | Nullable | Set By | Purpose |
|-------|------|----------|--------|---------|
| `eventId` | UUID v7 | no | system | Identity, deduplication, referencing |
| `eventType` | string | no | system | Routing, deserialization, projection mapping |
| `schemaVersion` | integer | no | system | Event evolution, upcasting |
| `aggregateId` | UUID | no | system | Stream partitioning, consistency boundary |
| `aggregateType` | string | no | system | Stream filtering, monitoring |
| `aggregateVersion` | integer | no | system | Optimistic concurrency, ordering |
| `occurredAt` | ISO 8601+tz | no | performer | Clinical/legal time of the event |
| `recordedAt` | ISO 8601+tz | no | system | Audit trail, sync ordering |
| `performedBy` | UUID | no | auth context | Accountability, HIPAA, malpractice |
| `performerRole` | enum | no | auth context | Scope-of-practice, supervision, billing |
| `organizationId` | UUID | no | auth context | Multi-tenancy, data isolation, jurisdiction |
| `facilityId` | UUID | no | auth context | Regulatory compliance, care coordination |
| `deviceId` | string | no | device | Conflict resolution, security audit |
| `connectionStatus` | enum | no | device | Offline trust, sync debugging |
| `correlationId` | UUID | no | command handler | End-to-end tracing across aggregates |
| `causationId` | UUID | yes | command handler | Direct causal parent, event graph |
| `visibility` | enum set | no | event type rules | Access control, regulatory compliance |
