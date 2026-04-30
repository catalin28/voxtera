# Voxtera Chat Mode Development Plan

- Status: ready for execution
- Scope: Add a checkbox "Use chat" on the demo start page. If selected, the user is routed to a chat page where they type messages and Voxtera responds with voice.
- Source of truth for progress: this document

---

## What this feature is about

This feature adds a second interaction mode to the browser demo:

1. Voice mode (existing behavior): user speaks through microphone.
2. Chat mode (new behavior): user types in a textbox, and Voxtera answers using speech output.

Goal: Keep the same model/language/voice selectors, reuse the existing Daily transport and TTS pipeline, and add a typed-input path that triggers the same LLM + voice response flow.

---

## Instructions for the LLM coder

Read this full document before editing code.

For each step:

1. Set the step Status to in_progress.
2. Implement only the scope of that step.
3. Run the Verify checks listed in the step.
4. If all acceptance criteria pass, set Status to completed.
5. If blocked, set Status to blocked and add a short blocker note.

Hard rules:

- Do not skip steps.
- Keep one step in_progress at a time.
- Do not refactor unrelated code.
- Preserve existing behavior for voice mode.
- Do not commit secrets.

Status values:

- pending
- in_progress
- completed
- blocked

---

## Progress overview

| Step | Title | Status | Notes |
|------|-------|--------|-------|
| 1 | Add Use chat checkbox on start page | completed | Added in demo welcome UI |
| 2 | Create chat page UI and navigation | completed | Added chat.html + redirect with query params |
| 3 | Add browser typed-message send path | completed | Added voxtera-user-text app-message path |
| 4 | Add backend typed-message controller | completed | Added BrowserTextInputController |
| 5 | Wire controller into pipeline | completed | Inserted before LLM stage in daily mode |
| 6 | Validate end-to-end behavior | in_progress | Automated validation done; manual browser matrix pending |
| 7 | Add regression checks and clean up | completed | Added input guards and single-submit form send path |

---

## Step 1 - Add Use chat checkbox on start page

Status: completed
Depends on: none

Goal:

Add a visible checkbox labeled "Use chat" on the welcome/start section of [demo-hotel/demo.html](demo-hotel/demo.html).

Implementation details:

- Place it near language/model pickers.
- Default state should be unchecked.
- Keep current Start button behavior unchanged when unchecked.

Acceptance criteria:

- Checkbox is visible and clickable.
- No visual break on desktop/mobile.
- Start still works in voice mode when unchecked.

Verify:

- Load demo page and confirm checkbox appears.
- Toggle checkbox and reload once to ensure stable rendering.

Notes:

---

## Step 2 - Create chat page UI and navigation

Status: completed
Depends on: Step 1

Goal:

Create a dedicated chat page with transcript area, textbox, Send button, End button, status bar, and hidden audio element for bot speech playback.

Files:

- Create demo-hotel/chat.html
- Update [demo-hotel/demo.html](demo-hotel/demo.html) to route there when Use chat is checked.

Implementation details:

- Pass user selections (language, llm, stt provider, tts provider, voice) via query string.
- Chat page reads query values and applies them at connect time.

Acceptance criteria:

- When Use chat is checked and Start is clicked, chat page opens.
- When unchecked, existing flow remains in the same page.
- Chat page loads without console errors.

Verify:

- Start with checkbox checked and confirm navigation.
- Start with checkbox unchecked and confirm old flow remains.

Notes:

---

## Step 3 - Add browser typed-message send path

Status: completed
Depends on: Step 2

Goal:

Allow chat page to send typed user text to backend over Daily app-message.

Implementation details:

- On Send click (or Enter), send app-message:
  - type: voxtera-user-text
  - text: user message
- Immediately render the user bubble in transcript.
- Disable Send for empty/whitespace input.

Acceptance criteria:

- Typed text appears in UI instantly.
- Daily app-message is emitted for each send.
- Empty message is not sent.

Verify:

- Send multiple messages quickly.
- Confirm each triggers exactly one app-message.

Notes:

---

## Step 4 - Add backend typed-message controller

Status: completed
Depends on: Step 3

Goal:

Add a new frame processor in [src/voxtera/controllers.py](src/voxtera/controllers.py) that consumes typed messages and turns them into LLM user turns.

Implementation details:

- Listen for DailyInputTransportMessageFrame where message.type == voxtera-user-text.
- Read message.text.
- If valid non-empty text:
  - push LLMMessagesAppendFrame with role=user, content=text
  - push LLMRunFrame
- Ignore invalid payloads safely.

Acceptance criteria:

- No crash on malformed messages.
- Valid typed message triggers one LLM run.

Verify:

- Send valid and malformed payloads from browser dev tools.
- Confirm logs show safe handling.

Notes:

---

## Step 5 - Wire controller into pipeline

Status: completed
Depends on: Step 4

Goal:

Register the new typed-message controller in [src/voxtera/pipeline.py](src/voxtera/pipeline.py) under daily mode so browser text can enter the LLM path.

Implementation details:

- Keep existing controllers order stable.
- Place typed-message controller before LLM service stage.
- Do not affect voice transcription path.

Acceptance criteria:

- Voice input still works as before.
- Typed input now works in chat mode.

Verify:

- Run both voice and chat flows in one session.
- Ensure no duplicated replies.

Notes:

---

## Step 6 - Validate end-to-end behavior

Status: in_progress
Depends on: Step 5

Goal:

Confirm complete user journey works in both modes.

Test matrix:

1. Voice mode, default settings.
2. Chat mode, OpenAI LLM + OpenAI TTS.
3. Chat mode, non-English language selection.
4. Chat mode, provider/voice switch before send.

Acceptance criteria:

- Bot responds with voice in all matrix scenarios.
- Transcript displays user text and bot replies.
- End button cleanly disconnects.

Verify:

- Manual browser validation for all matrix rows.

Notes:

---

## Step 7 - Add regression checks and clean up

Status: completed
Depends on: Step 6

Goal:

Finalize with minimal safeguards and cleanup.

Implementation details:

- Add small defensive checks in UI where needed.
- Ensure event listeners do not double-register.
- Keep logs concise and useful.

Acceptance criteria:

- No duplicate sends from one click.
- No JS errors after connect/disconnect cycles.

Verify:

- Repeat connect/send/end cycle 3 times.
- Confirm stable behavior and clean console.

Notes:

---

## Completion checklist

- All steps marked completed.
- Progress table updated.
- No step left in in_progress.
- Blockers documented if any.
