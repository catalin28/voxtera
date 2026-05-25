# Voxtera — Data Retention Policy

**Version:** 1.0  
**Effective Date:** _[Date]_  
**Owner:** _[Name / Role]_  
**Review Cycle:** Annually or upon material change to services

---

## 1. Purpose

This policy defines how long Voxtera retains personal data processed on behalf of hotel customers ("Controllers") through our multilingual voice concierge service. It ensures compliance with PIPEDA (Canada), GDPR (where applicable), and contractual obligations under our Data Processing Agreements.

---

## 2. Principles

- Data is retained **only as long as necessary** for the purpose it was collected.
- Retention periods are **configurable per customer** — Controllers may request shorter periods.
- When retention expires, data is **automatically deleted** unless a legal hold applies.
- **Anonymized/aggregated data** (no personal identifiers) may be retained indefinitely for service improvement.

---

## 3. Retention Schedule

| Data Category | Description | Default Retention | Deletion Method |
|---------------|-------------|-------------------|-----------------|
| Audio recordings | Raw audio of guest voice interactions | **30 days** | Automated purge |
| Transcriptions | Text output of speech-to-text processing | **90 days** | Automated purge |
| Ticket/action data | Structured data sent to hotel staff (guest name, room, request) | **Duration of contract** | Deleted within 30 days of termination |
| Conversation metadata | Timestamps, language detected, duration, session IDs | **90 days** | Automated purge |
| Analytics (aggregated) | Call volume, language distribution, response times — no PII | **Indefinite** | N/A (anonymized) |
| Admin/audit logs | Staff access logs, system events | **1 year** | Automated purge |
| Billing records | Invoice data, usage counts | **7 years** | Manual review (CRA requirement) |

---

## 4. Customer-Configurable Options

Controllers may request the following adjustments via written instruction:

| Option | Description |
|--------|-------------|
| Disable audio storage | Audio is processed in real-time only; no recordings are retained |
| Shorter transcript retention | E.g., 30 days instead of 90 |
| Immediate deletion on request | Specific conversations deleted within 10 business days |
| Extended retention | Where legally required by the Controller (must be documented) |

---

## 5. Deletion Process

### 5.1 Automated Deletion
- A scheduled job runs daily to identify and permanently delete data past its retention period.
- Deletion is irreversible — data is removed from primary storage and backups within 30 days of the retention expiry.

### 5.2 Manual Deletion Requests
- Data subject requests (right to erasure) are fulfilled within **10 business days**.
- Controller termination triggers deletion of all customer data within **30 days**.
- Written confirmation of deletion is provided upon request.

### 5.3 Subprocessor Data
- Upon deletion from Voxtera systems, we instruct subprocessors to delete corresponding data per their own retention commitments:
  - **Gladia**: Audio deleted after processing (not retained)
  - **Daily**: Session data retained per their policy; recordings (if enabled) stored per our configuration
  - **LLM Provider**: Prompts/completions — _[confirm provider policy; e.g., OpenAI API does not retain on paid plans]_

---

## 6. Legal Holds

If Voxtera becomes aware of litigation, regulatory investigation, or legal proceedings that may involve retained data:
- Normal deletion schedules are **suspended** for affected data.
- A legal hold notice is documented with scope and duration.
- Deletion resumes when the hold is lifted.

---

## 7. Backup Retention

- Backups are encrypted and retained for **14 days** on a rolling basis.
- Data deleted from primary systems will naturally age out of backups within this window.
- In no case will backup data be restored for purposes inconsistent with this policy.

---

## 8. Implementation Status

| Item | Status |
|------|--------|
| Automated deletion job | _[ ] Implemented / [ ] Planned_ |
| Per-customer retention config | _[ ] Implemented / [ ] Planned_ |
| Subprocessor deletion verification | _[ ] Implemented / [ ] Planned_ |
| Backup rotation (14-day) | _[ ] Implemented / [ ] Planned_ |

---

## 9. Review and Updates

This policy is reviewed:
- Annually
- When a new subprocessor is added
- When entering a new jurisdiction
- After any data breach

---

*This is an internal policy document. It is shared with customers as part of due diligence and DPA discussions, but is not published publicly.*
