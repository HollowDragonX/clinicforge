# Clinical Event Taxonomy

> **Status**: Design — pending review before implementation.
> **Governing principle**: Events represent real-world clinical facts, not database mutations.

---

## Foundational Rules

1. Every event is **immutable** — it happened; it cannot un-happen.
2. Every event must be **meaningful to a physician** — a clinician could read the event name and understand what occurred in reality.
3. Every event must **survive offline sync conflicts** — two devices recording the same clinical fact must converge, not corrupt.
4. No event may represent a CRUD operation. `PatientUpdated`, `RecordModified`, `EntitySaved` are **forbidden**.

---

## Event Categories

### 1. Patient Lifecycle

**Real-world meaning**: Something changed in a person's relationship with this clinical practice. A patient arrived, was identified, left, died, or corrected their identity. These are facts about the patient *as a person in the real world* — not about a database row.

**Why this category exists clinically**: A practice must know who its patients are, when they entered care, and when they left. Regulatory, billing, and continuity-of-care requirements all depend on an accurate timeline of the patient-practice relationship. Identity corrections (not "updates") reflect the discovery of errors — a clinically distinct act from the original registration.

| Event | What happened in reality |
|-------|--------------------------|
| `PatientRegistered` | A new person presented to the practice for the first time and was enrolled as a patient. |
| `PatientIdentityCorrected` | The practice discovered that recorded identity information (name, DOB, etc.) was wrong and corrected it. This is NOT "demographics changed" — it means "we learned we had it wrong." |
| `PatientContactInfoProvided` | The patient informed the practice of current contact details (address, phone, email). Each occurrence is a new declaration by the patient, not a mutation of old data. |
| `PatientDeceasedRecorded` | The practice was informed or determined that the patient has died. |
| `PatientTransferredOut` | The patient formally left this practice's care (transferred to another provider, relocated, etc.). |
| `PatientDuplicateIdentified` | Two separate patient records were discovered to belong to the same real-world person. |

**Retired / forbidden events**: ~~`PatientUpdated`~~ — this is a CRUD mutation with no clinical meaning. Replaced by `PatientIdentityCorrected` (factual error) and `PatientContactInfoProvided` (new information from patient).

---

### 2. Encounter Progression

**Real-world meaning**: A clinical encounter — a visit, consultation, or telehealth session — moved through a stage in its natural lifecycle. These are things that physically happened in the clinic: the patient walked in, the doctor began examining, the visit concluded.

**Why this category exists clinically**: The encounter is the fundamental unit of clinical care delivery. Its lifecycle drives documentation requirements, billing (CPT coding depends on encounter type and duration), malpractice timelines, and care quality metrics. Each stage has distinct legal and clinical significance — a patient who checked in but was never seen is very different from a patient who was examined.

| Event | What happened in reality |
|-------|--------------------------|
| `PatientCheckedIn` | The patient arrived at the practice and was marked as present for their encounter. |
| `PatientTriaged` | A clinician assessed the patient's chief complaint and acuity level prior to full examination. |
| `EncounterBegan` | The practitioner started the clinical interaction with the patient (entered the room, connected to telehealth, etc.). |
| `EncounterCompleted` | The practitioner concluded the clinical interaction. All clinical work for this visit is done. |
| `PatientDischarged` | The patient was formally released from the encounter context with instructions. |
| `EncounterReopened` | A completed encounter was revisited because a clinician needed to add a late addendum or because additional clinical work was necessary (e.g., patient returned same day). |

---

### 3. Clinical Observation

**Real-world meaning**: A clinician observed, measured, or received information about the patient's current health state. These are **raw clinical data points** — what was seen, measured, or reported. They do not include the clinician's interpretation (that belongs in Clinical Judgment).

**Why this category exists clinically**: Observations are the evidentiary foundation of medicine. A blood pressure reading, a patient's reported symptom, a lab result — these are facts that exist independent of any diagnosis. They must be recorded exactly as observed, at the moment they were observed, because they may be reinterpreted later. Observations recorded offline must merge cleanly because two nurses measuring vitals at different times are recording independent facts.

| Event | What happened in reality |
|-------|--------------------------|
| `VitalSignsRecorded` | A clinician measured the patient's vital signs (blood pressure, heart rate, temperature, respiratory rate, O2 saturation, weight, height). |
| `SymptomReported` | The patient described a symptom to the clinician (chief complaint or review-of-systems finding). |
| `ExaminationFindingNoted` | The clinician noted a specific finding during physical examination (e.g., "tenderness in RLQ", "clear lung sounds bilaterally"). |
| `AllergyIdentified` | A new allergy or adverse reaction was identified for this patient. |
| `AllergyRefuted` | A previously recorded allergy was clinically determined to be incorrect (patient tolerates the substance). |
| `LabResultReceived` | A laboratory or diagnostic test result was received and associated with this patient. |

---

### 4. Clinical Judgment

**Real-world meaning**: A clinician made an **interpretive decision** about the patient's health — a diagnosis, a revision to a prior diagnosis, or the determination that a condition has resolved. These are acts of clinical reasoning, distinct from raw observations.

**Why this category exists clinically**: Diagnoses drive treatment plans, prescriptions, referrals, and billing codes. A diagnosis is not a fact of nature — it is a clinician's professional judgment applied to observations. Diagnoses can be revised (clinician changes their mind based on new evidence) or resolved (condition is no longer active). Each of these is a distinct clinical act with different legal and documentation implications. Offline resilience is critical: two clinicians independently diagnosing the same patient represent two valid clinical opinions, not a conflict.

| Event | What happened in reality |
|-------|--------------------------|
| `DiagnosisMade` | A clinician determined that the patient has a specific condition (e.g., ICD-coded diagnosis). |
| `DiagnosisRevised` | A clinician changed their clinical judgment about a prior diagnosis (e.g., "suspected pneumonia" → "confirmed bronchitis"). References the prior `DiagnosisMade` event. |
| `DiagnosisResolved` | A clinician determined that a previously active diagnosis is no longer present or clinically relevant. |
| `ProcedurePerformed` | A clinical procedure was performed on the patient during this encounter (e.g., laceration repair, joint injection). |
| `ReferralDecided` | A clinician determined the patient should be seen by another provider or specialist. |
| `TreatmentPlanFormulated` | A clinician decided on a course of action for the patient's care going forward. |

---

### 5. Clinical Documentation

**Real-world meaning**: A clinician authored, amended, or attested to a piece of the permanent medical record. This is the act of **writing**, distinct from the clinical observations or judgments being written *about*.

**Why this category exists clinically**: The medical record is a legal document. Who wrote what, when, and whether it was reviewed by a supervisor are all independently significant facts. A note authored at 2pm but co-signed at 6pm produces two events — both matter for audit trails, malpractice timelines, and regulatory compliance. Addenda are clinically distinct from original notes because they indicate that the author returned to the record after initial completion.

| Event | What happened in reality |
|-------|--------------------------|
| `ClinicalNoteAuthored` | A clinician wrote a clinical note (SOAP note, progress note, procedure note, etc.) and committed it to the record. |
| `NoteAddendumAuthored` | A clinician added supplementary information to a previously authored note. The original note is unchanged; the addendum is a new, linked record. |
| `NoteCosigned` | A supervising clinician reviewed and attested to a note authored by a trainee or supervised provider. |

---

### 6. Care Access (Scheduling)

**Real-world meaning**: A patient arranged, rearranged, or lost access to a future clinical encounter. These are logistical facts about the patient's path to care — not database state changes on a calendar.

**Why this category exists clinically**: Access to care is a clinical concern, not just an administrative one. A cancelled appointment may indicate a patient at risk of falling out of care. A no-show has different clinical implications than a cancellation. Rescheduling preserves continuity; cancellation may break it. These distinctions matter for population health management, chronic disease follow-up, and care gap identification.

| Event | What happened in reality |
|-------|--------------------------|
| `AppointmentRequested` | A patient (or their representative) requested a future visit with the practice. |
| `AppointmentConfirmed` | The practice confirmed that a requested appointment will occur at a specific time with a specific provider. |
| `AppointmentCancelledByPatient` | The patient cancelled their upcoming appointment. |
| `AppointmentCancelledByPractice` | The practice cancelled the patient's upcoming appointment (provider unavailable, etc.). |
| `PatientNoShowed` | The patient did not arrive for a confirmed appointment and did not cancel. |
| `AppointmentRescheduled` | A confirmed appointment was moved to a different time or provider, preserving continuity with the original request. |

**Retired / forbidden events**: ~~`SlotReleased`~~ — this is an infrastructure-level side effect (a calendar slot becoming available), not a clinical fact. Slot availability is a **projection** derived from the above events.

---

## Category–Context Mapping

| Event Category | Primary Bounded Context | Rationale |
|---------------|------------------------|-----------|
| Patient Lifecycle | Patient Identity | Owns the patient-practice relationship |
| Encounter Progression | Clinical Encounter | Owns the encounter state machine |
| Clinical Observation | Clinical Records | Observations are part of the permanent clinical record |
| Clinical Judgment | Clinical Records | Diagnoses, procedures, and plans are part of the permanent clinical record |
| Clinical Documentation | Clinical Records | Notes and attestations are part of the permanent clinical record |
| Care Access | Scheduling | Owns appointment lifecycle |

---

## Offline Conflict Properties

Each category is designed so that events from different devices can be **merged without semantic conflict**:

| Category | Why it merges cleanly |
|----------|----------------------|
| **Patient Lifecycle** | Each event is a discrete real-world occurrence. Two devices recording `PatientContactInfoProvided` produce two declarations — both are valid. `PatientIdentityCorrected` carries the corrected value and the reason; concurrent corrections can be resolved by timestamp + clinical review. |
| **Encounter Progression** | Encounter stages are sequential by nature. Vector clocks on the encounter aggregate detect out-of-order arrivals and resequence. |
| **Clinical Observation** | Observations are inherently additive. Two devices recording different vitals are two independent measurements — no conflict. |
| **Clinical Judgment** | Each diagnosis/revision is an independent clinical act by a specific clinician. Concurrent diagnoses are not conflicts — they are two opinions. |
| **Clinical Documentation** | Notes are append-only. Two clinicians writing notes offline produce two notes — no conflict. Cosignatures reference a specific note ID. |
| **Care Access** | Each event references a specific appointment. Concurrent cancellation from patient and practice are two valid facts about the same appointment. |
