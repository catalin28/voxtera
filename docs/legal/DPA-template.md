# DATA PROCESSING AGREEMENT

**Between:**

**Controller:** _[Hotel Name]_, _[Address]_ ("Controller" / "Customer")

**Processor:** Voxtera Inc., a Canadian corporation, _[Address]_ ("Processor" / "Voxtera")

**Effective Date:** _[Date]_

This Data Processing Agreement ("DPA") forms part of the service agreement between the Controller and Processor for the provision of Voxtera's multilingual voice concierge services ("Services").

---

## 1. Definitions

- **Personal Data**: Any information relating to an identified or identifiable guest or staff member processed in connection with the Services.
- **Processing**: Any operation performed on Personal Data, including collection, recording, storage, retrieval, transmission, erasure, or destruction.
- **Subprocessor**: A third party engaged by the Processor to process Personal Data on behalf of the Controller.
- **Data Breach**: A breach of security leading to the accidental or unlawful destruction, loss, alteration, unauthorized disclosure of, or access to, Personal Data.

---

## 2. Scope and Purpose of Processing

### 2.1 Categories of Data Subjects
- Hotel guests
- Hotel staff members interacting with the system

### 2.2 Types of Personal Data Processed
- Voice recordings (audio of guest requests)
- Transcriptions of voice interactions
- Guest name (as spoken or provided)
- Room number
- Language spoken
- Request content (e.g., service orders, maintenance issues, inquiries)
- Timestamps of interactions

### 2.3 Purpose of Processing
Personal Data is processed solely to:
- Transcribe and understand guest voice requests
- Generate appropriate voice responses in the guest's language
- Route actionable tickets to hotel staff via configured channels
- Provide the Controller with conversation logs and analytics

### 2.4 Duration of Processing
Processing occurs for the duration of the service agreement. Upon termination, Section 10 applies.

---

## 3. Processor Obligations

The Processor shall:

a) Process Personal Data only on documented instructions from the Controller, unless required by law.

b) Ensure that persons authorized to process Personal Data are bound by confidentiality obligations.

c) Implement appropriate technical and organizational security measures (see Section 5).

d) Assist the Controller in responding to data subject rights requests (see Section 7).

e) Assist the Controller with data protection impact assessments where required.

f) Make available all information necessary to demonstrate compliance and allow for audits (see Section 9).

g) Notify the Controller without undue delay upon becoming aware of a Data Breach (see Section 6).

h) Delete or return all Personal Data upon termination of the agreement (see Section 10).

---

## 4. Controller Obligations

The Controller shall:

a) Ensure there is a lawful basis for processing guest Personal Data through the Services (e.g., legitimate interest in providing hotel services, or guest consent where required).

b) Inform guests that voice interactions may be recorded and processed, through appropriate signage or notice (e.g., in-room information cards, check-in disclosure).

c) Provide documented instructions to the Processor regarding the processing of Personal Data.

---

## 5. Security Measures

The Processor implements the following technical and organizational measures:

| Measure | Implementation |
|---------|---------------|
| Encryption in transit | TLS 1.3 for all API and audio connections |
| Encryption at rest | AES-256 for stored data |
| Access control | Role-based access; MFA for administrative systems |
| Logging | Audit logs of access to Personal Data |
| Infrastructure | Hosted on secured cloud infrastructure with regional deployment |
| Personnel | Confidentiality agreements with all staff |
| Incident response | Documented incident response procedure |
| Backups | Encrypted backups with access controls |

---

## 6. Data Breach Notification

6.1 The Processor shall notify the Controller of a confirmed Data Breach without undue delay and no later than **72 hours** after becoming aware of it.

6.2 Notification shall include:
- Nature of the breach and categories of data affected
- Approximate number of data subjects concerned
- Likely consequences
- Measures taken or proposed to address the breach

6.3 The Processor shall cooperate with the Controller in investigating and remediating the breach.

---

## 7. Data Subject Rights

7.1 The Processor shall assist the Controller in fulfilling requests from data subjects exercising their rights, including:
- Right of access
- Right to rectification
- Right to erasure ("right to be forgotten")
- Right to restriction of processing
- Right to data portability
- Right to object

7.2 The Processor shall respond to Controller's instructions regarding data subject requests within **10 business days**.

---

## 8. Subprocessors

### 8.1 Authorized Subprocessors

The Controller authorizes the use of the following subprocessors:

| Subprocessor | Purpose | Location |
|--------------|---------|----------|
| Gladia SAS | Speech-to-text transcription | France (EU) |
| Daily.co (Daily, Inc.) | Real-time audio transport | United States |
| _[LLM Provider]_ | Language understanding and response generation | _[Region]_ |
| _[TTS Provider]_ | Text-to-speech voice synthesis | _[Region]_ |
| DigitalOcean, LLC | Infrastructure hosting | Region selected per deployment |

### 8.2 Changes to Subprocessors

The Processor shall notify the Controller at least **30 days** in advance of any intended addition or replacement of subprocessors. The Controller may object within 14 days. If no resolution is reached, the Controller may terminate the affected Services.

### 8.3 Subprocessor Obligations

The Processor shall impose the same data protection obligations on subprocessors as set out in this DPA through written contracts.

---

## 9. Audits

9.1 The Processor shall make available to the Controller all information reasonably necessary to demonstrate compliance with this DPA.

9.2 The Controller may conduct or commission an audit **once per calendar year**, with at least 30 days' written notice, during normal business hours, and at the Controller's expense.

9.3 The Processor may satisfy audit requests by providing:
- SOC 2 reports (when available)
- Written responses to a reasonable security questionnaire
- Evidence of implemented controls

---

## 10. Data Deletion and Return

10.1 Upon termination of the service agreement, or upon Controller's written request, the Processor shall:

a) Delete all Personal Data within **30 days**, or

b) Return all Personal Data to the Controller in a standard machine-readable format, then delete all copies.

10.2 The Processor may retain Personal Data only where required by applicable law, and shall inform the Controller of such requirement.

10.3 The Processor shall provide written confirmation of deletion upon request.

---

## 11. International Data Transfers

11.1 Where Personal Data is transferred outside the Controller's jurisdiction, the Processor shall ensure appropriate safeguards are in place, which may include:
- Standard Contractual Clauses (SCCs) as approved by the European Commission
- Adequacy decisions (e.g., Canada's PIPEDA adequacy for EU transfers)
- Binding Corporate Rules of subprocessors

11.2 The Processor shall inform the Controller of the specific transfer mechanisms relied upon, upon request.

---

## 12. Recording Opt-Out

12.1 The Controller may instruct the Processor to disable storage of audio recordings at the property level. In this mode:
- Audio is processed in real-time for transcription only
- No audio files are retained after processing
- Only text transcriptions and ticket data are stored

---

## 13. Data Retention

| Data type | Default retention | Notes |
|-----------|------------------|-------|
| Audio recordings | 30 days | Configurable; can be disabled entirely |
| Transcriptions | 90 days | Configurable per Controller instruction |
| Ticket/action data | Duration of agreement | Deleted per Section 10 on termination |
| Aggregated analytics | Duration of agreement | Anonymized; not Personal Data |

The Controller may request shorter retention periods at any time via written instruction.

---

## 14. Governing Law

This DPA shall be governed by and construed in accordance with _[the laws of the Province of _______, Canada / the laws applicable to the main service agreement]_.

---

## 15. Term

This DPA shall remain in effect for the duration of the service agreement between the parties. Obligations regarding data deletion (Section 10) and confidentiality survive termination.

---

## Signatures

**Controller:**

Name: ___________________________

Title: ___________________________

Date: ___________________________

Signature: ___________________________

---

**Processor (Voxtera Inc.):**

Name: ___________________________

Title: ___________________________

Date: ___________________________

Signature: ___________________________

---

*This DPA template is provided as a starting point. Legal review is recommended before use.*
