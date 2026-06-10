# `feat/VOX-booking-handoff` — Build Spec

**Project:** VOX — Voxtera Voice Agent  
**Branch:** `feat/VOX-booking-handoff`  
**Branched from:** `main`  
**Version:** 1.0 · June 2026  
**Confidential — Internal Document**

---

## Overview

This branch adds a **booking conclusion flow** to the existing Voxtera voice agent pipeline. It activates when the agent detects the conversation has reached a natural closing point where the guest is ready to book.

The flow covers four sequential stages: package summary → dates collection → availability check → prefilled booking form.

---

## What to Build

### 1. Conversation Conclusion Detector

A function that monitors the conversation state and triggers the booking handoff flow when:

- Guest has expressed clear booking intent
- A specific hotel has been identified and discussed during the session
- The agent has enough context accumulated to summarize a package

### 2. Package Summary

When triggered, the agent must:

- Verbally summarize the discussed hotel, room type, board basis, and any extras mentioned during the conversation
- Ask the guest to confirm the summary is correct before proceeding

### 3. Dates Collection

After summary confirmation:

- Ask the guest for their **check-in date** and **check-out date**
- Parse and validate the dates (future dates only, check-out must be after check-in)
- Confirm the dates back to the guest verbally before proceeding

### 4. Availability Check (Function Call)

Once dates are confirmed, call:

```python
check_availability(
    hotel_id: str,
    checkin_date: date,
    checkout_date: date,
    room_type: str
) -> AvailabilityResult
```

Handle three outcomes:

| Outcome | Agent Behaviour |
|---|---|
| **Available** | Proceed to prefilled booking form |
| **Not available** | Inform guest, offer alternative dates or alternative hotel |
| **Error / timeout** | Apologize, offer to have a human follow up |

### 5. Prefilled Booking Form

If available, generate or display a booking confirmation form pre-populated with:

- Hotel name
- Room type
- Check-in / check-out dates
- Number of guests (if collected during session)
- Total price (if returned by the availability function)

Ask the guest to **confirm and proceed to payment**.

The form display mechanism depends on the transport layer:

| Transport | Delivery Method |
|---|---|
| Web widget | Render form inline in the UI |
| Phone (Twilio) | Send SMS link to guest |

---

## Flow

```
Booking intent detected
        ↓
Agent summarizes package verbally
        ↓
Guest confirms summary
        ↓
Agent asks for check-in and check-out dates
        ↓
Dates parsed and confirmed
        ↓
check_availability() called
        ↓
Available?     → Prefilled form displayed → Guest confirms → Payment
Not available? → Offer alternative dates or hotel
Error?         → Human handoff
```

---

## Technical Notes

- Follow existing **async/await** patterns throughout — no blocking calls
- Use **loguru** for all logging: `logger.info` / `logger.error` / `logger.debug`
- All new config (API endpoints, timeouts) loaded via `.env` — never hardcode
- The `check_availability()` function must be implemented as a **stub with a clear interface** — the actual API integration is out of scope for this branch
- Reuse existing **session state** to extract `hotel_id`, `traveller_type`, `children_ages`, and `requirements` already accumulated during the conversation — do not re-collect what the session already holds
- The flow must be **interruptible** — if the guest changes their mind mid-flow, the agent gracefully resets to the previous conversation state
- Add concise inline comments for all non-obvious logic
- Use type hints on all function signatures
- Follow PEP 8

---

## Out of Scope for This Branch

- Actual payment processing
- Real availability API integration (stub only — interface must be defined, not implemented)
- CRM webhook after booking (separate branch)
- Admin dashboard updates

---

## Acceptance Criteria

- [ ] Booking intent correctly triggers the handoff flow from an active conversation
- [ ] Agent verbally summarizes the package and waits for guest confirmation before proceeding
- [ ] Dates are parsed, validated, and confirmed back to the guest
- [ ] `check_availability()` stub is called with correct arguments
- [ ] All three availability outcomes (available, not available, error) are handled gracefully
- [ ] Prefilled form is generated with all collected session data
- [ ] Flow is interruptible at any stage without crashing or losing session state
- [ ] All new code covered by unit tests for the stub and flow logic
- [ ] No blocking calls — full async/await throughout

---

*Voxtera · VOX-booking-handoff Build Spec v1.0 · June 2026 · Confidential*
