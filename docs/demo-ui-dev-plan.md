# Demo UI — Development Plan

## Goal

Build a branded browser page that replaces Daily's generic prebuilt UI.
A guest opens the page, clicks "Start", and talks to Voxtera — seeing a live
transcript of the conversation plus bot status. No server-side rendering; one
static HTML file using the Daily JS SDK.

---

## Architecture

```
┌────────────────────────────────────────────────────┐
│  Browser (demo page)                               │
│                                                    │
│  daily-js call object (audio only, no video)       │
│      ↕ WebRTC audio                                │
│      ↕ data channel (sendAppMessage / on app-msg)  │
│                                                    │
│  UI: hotel header · start button · transcript feed │
│       status badge · latency chips                 │
└──────────────────┬─────────────────────────────────┘
                   │ Daily room (voxtera-demo)
┌──────────────────┴─────────────────────────────────┐
│  Bot (Python — bot.py)                             │
│                                                    │
│  DailyTransport  ← existing                        │
│  DemoEventBroadcaster (new FrameProcessor)         │
│      pushes DailyOutputTransportMessageFrame with  │
│      JSON events at each pipeline stage            │
└────────────────────────────────────────────────────┘
```

**No extra WebSocket server.** Daily's `sendAppMessage` is the data channel.
The bot pushes structured JSON events; the browser page listens via
`callObject.on("app-message", handler)`.

---

## Phase 1 — Bot: Event Broadcaster

### New FrameProcessor: `DemoEventBroadcaster`

Insert it into the pipeline right after `PipelineTracer` (before TTS).
It watches the same frames PipelineTracer already watches and pushes
a `DailyOutputTransportMessageFrame` for each event.

**Location:** `src/voxtera/bot.py`, new class, ~60 lines.

**Message schema** (all messages share this envelope):

```json
{
  "type": "voxtera-event",
  "event": "<event-name>",
  "ts": 1714276800.123,
  "data": { ... }
}
```

**Events to emit:**

| Event name          | Trigger frame                       | `data` payload                                 |
|---------------------|-------------------------------------|------------------------------------------------|
| `user-started`      | `VADUserStartedSpeakingFrame`       | `{}`                                           |
| `user-stopped`      | `VADUserStoppedSpeakingFrame`       | `{}`                                           |
| `user-transcript`   | `TranscriptionFrame`                | `{ "text": "..." }`                            |
| `bot-thinking`      | `LLMFullResponseStartFrame`         | `{}`                                           |
| `bot-reply`         | `LLMFullResponseEndFrame`           | `{ "text": "...", "think_ms": 1234 }`          |
| `bot-speaking`      | `BotStartedSpeakingFrame`           | `{ "latency_ms": 2345 }` (end-to-end)         |
| `bot-done-speaking` | `BotStoppedSpeakingFrame`           | `{}`                                           |

**How to push:**

```python
from pipecat.transports.daily.transport import DailyOutputTransportMessageFrame

msg = {"type": "voxtera-event", "event": "user-transcript", "ts": time.time(), "data": {"text": text}}
await self.push_frame(DailyOutputTransportMessageFrame(message=msg), FrameDirection.DOWNSTREAM)
```

**Conditional:** Only instantiate `DemoEventBroadcaster` when
`settings.transport_mode == "daily"`. Local mode has no data channel.

### Pipeline wiring change

```python
processors.extend([
    llm,
    PipelineTracer("voxtera", hotel_id=...),
    DemoEventBroadcaster(),        # ← new, daily mode only
    tts,
    transport.output(),
    context_aggregator.assistant(),
])
```

---

## Phase 2 — Browser: Demo Page

### File: `demo-hotel/demo.html`

Single self-contained HTML file. No build step, no npm. Uses:
- **daily-js** via CDN (`https://unpkg.com/@daily-co/daily-js`)
- Vanilla JS (no framework)
- Inline CSS (minimal, clean)

### Page layout

```
┌─────────────────────────────────────────────┐
│  ★ Grand Hôtel Lumière — Virtual Concierge  │  ← header
├─────────────────────────────────────────────┤
│                                             │
│  [user bubble]  My TV is not working.       │
│  [bot bubble]   Try pressing the power...   │
│     └ answered in 1.2s                      │
│                                             │
│  [user bubble]  What are the spa hours?     │
│  [bot bubble]   The spa is open daily...    │
│     └ answered in 0.9s                      │
│                                             │
│  ● Listening...                             │  ← status badge
│                                             │
├─────────────────────────────────────────────┤
│   [ 🎙 Start Conversation ]  [ End ]       │  ← controls
└─────────────────────────────────────────────┘
```

### Behavior

1. **Page load:** Show hotel branding + "Start Conversation" button. No audio yet.

2. **Click "Start":**
   - Create Daily call object (`DailyIframe.createCallObject()`)
   - Join room `https://voxtera.daily.co/voxtera-demo` (audio only, camera off)
   - Request mic permission
   - Listen for `app-message` events
   - Update status badge to "🟢 Connected — Listening"
   - Bot auto-greets (audio plays via Daily)

3. **During conversation:**
   - `user-started` → status: "🎤 You're speaking..."
   - `user-transcript` → add user bubble to transcript feed, scroll down
   - `bot-thinking` → status: "🟡 Thinking..."
   - `bot-reply` → add bot bubble with text + latency chip
   - `bot-speaking` → status: "🔵 Voxtera is speaking..."
   - `bot-done-speaking` → status: "🟢 Listening"

4. **Click "End":**
   - Leave the Daily room
   - Show "Session ended" message

### Daily JS — Key API calls

```javascript
// Create (audio only, no video)
const callObject = DailyIframe.createCallObject({
  audioSource: true,
  videoSource: false,
});

// Join
await callObject.join({
  url: "https://voxtera.daily.co/voxtera-demo",
  userName: "Guest",
});

// Listen for bot events
callObject.on("app-message", (evt) => {
  const msg = evt.data;
  if (msg.type !== "voxtera-event") return;
  handleEvent(msg.event, msg.data, msg.ts);
});

// Leave
await callObject.leave();
callObject.destroy();
```

### Styling notes

- Dark background (#1a1a2e or similar), light text — looks professional on projector
- User bubbles: right-aligned, subtle blue
- Bot bubbles: left-aligned, subtle gray
- Monospace latency chips in muted color
- Responsive but optimized for laptop/desktop screen share
- Hotel logo placeholder (replace with real asset later)

---

## Phase 3 — Polish (optional, post-MVP)

- **Animated waveform** during bot speaking (CSS-only pulse animation)
- **Sound effect** on connect/disconnect
- **Language badge** showing detected language per turn
- **Dark/light toggle** for different demo environments
- **Copy transcript** button for follow-up emails
- **RAG debug panel** (operator view): show retrieved chunks + scores

---

## Implementation Order

| Step | What                                              | Files changed                  |
|------|---------------------------------------------------|--------------------------------|
| 1    | Add `DemoEventBroadcaster` class to bot.py        | `src/voxtera/bot.py`           |
| 2    | Wire broadcaster into pipeline (daily mode only)  | `src/voxtera/bot.py`           |
| 3    | Test: run bot, open Daily prebuilt UI, check       | (manual)                       |
|      | browser console for `app-message` events          |                                |
| 4    | Create `demo-hotel/demo.html` with layout + CSS   | `demo-hotel/demo.html`         |
| 5    | Add Daily JS SDK, join logic, mic handling         | `demo-hotel/demo.html`         |
| 6    | Add app-message listener, wire to transcript UI   | `demo-hotel/demo.html`         |
| 7    | Add status badge + latency chips                  | `demo-hotel/demo.html`         |
| 8    | End-to-end test: start bot, open demo.html,       | (manual)                       |
|      | verify voice + transcript work together           |                                |

---

## Config / Environment

No new env vars needed. The page uses the same Daily room URL that's
already configured. The bot needs `TRANSPORT_MODE=daily` (already set).

To serve the page locally for testing:
```bash
cd demo-hotel && python -m http.server 8080
# Then open http://localhost:8080/demo.html
```

---

## Risks & Mitigations

| Risk                                   | Mitigation                                      |
|----------------------------------------|-------------------------------------------------|
| `sendAppMessage` has size limits        | Our messages are small JSON (<1KB). No risk.    |
| Browser blocks mic without HTTPS        | localhost is exempt. For remote demo, use ngrok. |
| Whisper mis-transcribes (accent/noise)  | Transcript shows what bot heard — honest demo.  |
| Stale bot in room from previous run     | `_eject_stale_bots()` already handles this.     |
| RTVI warnings from prebuilt UI clients  | Already filtered in `configure_logging()`.      |
