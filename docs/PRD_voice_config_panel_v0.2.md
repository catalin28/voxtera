# PRD — Voice Configuration Panel
**Product:** Voxtera Admin Dashboard
**Feature:** Voice Selection & Configuration
**Version:** 0.3 · June 2026
**Status:** Draft
**Owner:** Voxtera

---

## 1. Purpose & Context

Voxtera deployments use a TTS voice to speak to hotel guests. Different
properties have different brand identities — a luxury boutique in Lisbon
wants a different voice than a family resort in Belek. Currently the voice
is hardcoded per deployment. This panel gives operators and Voxtera staff
the ability to browse, preview, and select a TTS voice per property without
touching configuration files or redeploying.

The system draws from three TTS providers (Google Chirp 3 HD, Cartesia
Sonic-3, ElevenLabs Flash v2.5) but operators do not need to know or care
about providers. They pick a voice — the system resolves the provider
automatically.

---

## 2. Users

| User | Context | Goal |
|---|---|---|
| Voxtera staff | During property onboarding | Configure voice before go-live |
| Hotel operator | Self-serve, post-onboarding only | Adjust voice to match brand feedback |

**Access control:** Hotel operators cannot access this panel until Voxtera
staff has completed onboarding and marked the property as live. Before that
point the panel is visible to Voxtera staff only. No role-based feature
differences between the two user types once access is granted.

---

## 3. User Flow

```
Admin dashboard index
  └── Voice Configuration card → /admin/voice/
        │
        ├── Preview text input (shared)
        │
        ├── Filter bar: All · Google · Cartesia · ElevenLabs
        │              Gender: All · Female · Male · Neutral
        │
        ├── Voice card grid
        │     ├── Card: Kore (Google)       [▶ Preview] [Select]
        │     ├── Card: Jameson (Cartesia)  [▶ Preview] [Select]
        │     ├── Card: Rachel (ElevenLabs) [▶ Preview] [Select]
        │     └── ...
        │                                    ↓ on Select
        └── Parameter drawer (slides in from right)
              ├── Voice name + provider header
              ├── Parameter controls (provider-specific)
              ├── Preview text input (mirrors shared input)
              ├── [▶ Preview with current params]
              ├── [Reset to defaults] [Reset to saved]
              └── [Save] button
```

**Step by step:**

1. User clicks the Voice Configuration card on the admin dashboard index.
   Lands on `/admin/voice/` — a standalone page.
2. Currently active voice is highlighted in the card grid with its saved
   parameters shown as a summary badge on the card.
3. User optionally types custom preview text in the shared input field.
   Default placeholder: *"Good evening. How can I help you today?"*
4. User clicks ▶ on any voice card — audio plays using that voice's
   provider defaults (no parameters applied yet).
5. User clicks Select on a voice card — parameter drawer slides in from
   the right. Card grid remains visible and scrollable behind the drawer.
6. User adjusts parameters using sliders and toggles in the drawer.
   Auto-preview fires 1 second after the user stops moving any slider.
   User can also click ▶ Preview manually at any time.
7. User clicks Save in the drawer — voice + parameters are persisted.
8. Drawer closes. Card updates to show new parameter summary badge.
9. Toast confirmation: *"Voice updated. Changes are live."*

---

## 4. Voice Browser

### 4.1 Voice list source

Voices are fetched live from each provider's API on panel load. Results
are cached in the browser for the session (no re-fetch on tab switch).

| Provider | API endpoint | What is fetched |
|---|---|---|
| Google Chirp 3 HD | `GET /v1/voices` (Cloud TTS) | All Chirp3-HD voices |
| Cartesia | `GET /voices` (api.cartesia.ai) | Shared public voice library |
| ElevenLabs | `GET /v1/voices` (api.elevenlabs.io) | Premade voices only |

All three calls are proxied through the Voxtera backend — API keys never
reach the browser.

### 4.2 Deduplication — Google Chirp 3 HD

The Google Cloud TTS API returns one entry per locale variant
(`en-US-Chirp3-HD-Kore`, `tr-TR-Chirp3-HD-Kore`, `de-DE-Chirp3-HD-Kore`,
…). The same 8 voice names repeat across ~31 locales, producing hundreds
of entries.

**Deduplication rule:** Group by voice name. Display each voice name once.
The locale prefix is resolved at synthesis time based on the guest's
detected language. The operator picks `Kore` — the system calls
`tr-TR-Chirp3-HD-Kore` when the guest speaks Turkish, `en-US-Chirp3-HD-Kore`
when the guest speaks English, and so on.

### 4.3 Cartesia scope

The Cartesia public voice library contains approximately 200–250 voices
and is actively growing (94 new voices were added in early 2026 alone
across 17 locales). Fetching and displaying all of them is impractical
and many are optimised for gaming, audiobooks, or character work — wrong
tone for a hotel concierge context.

**Curation approach:** Fetch the full public library via `GET /voices`,
then filter to a manually maintained allowlist of voices that have been
vetted for hospitality and professional conversational use. Target: ~25–35
voices. The allowlist is a config file maintained by Voxtera, not hardcoded
in the UI.

The allowlist must be reviewed whenever Cartesia publishes a major voice
library update.

### 4.4 ElevenLabs scope

ElevenLabs has 10,000+ voices in the shared library. In v1, fetch only
`category: premade` voices (the curated default set, ~30 voices). The
full shared library is out of scope.

### 4.5 Voice card data model

Each card displays:

| Field | Source | Example |
|---|---|---|
| Display name | API `name` field, deduplicated | Kore |
| Provider label | Derived from source API | (Google) |
| Gender | API field | Female |
| Tone tags | API labels or manually curated | Warm · Clear · Professional |
| Language coverage | Computed from API data | 31 languages |
| Preview button | Triggers live synthesis | ▶ |
| Selected state | Compared against saved config | ✓ highlighted border |

### 4.6 Naming collision handling

If two voices from different providers share the same display name (e.g.
both have a voice called "Aria"), disambiguate by appending the provider
in brackets: `Aria (Google)` vs `Aria (ElevenLabs)`. The provider label
is always visible in the card regardless, so collisions are always clear.

### 4.7 Internal voice identifier

Internally each voice is referenced by a stable composite key:
`{provider_slug}:{voice_id}`.

Examples:
- `google:Kore`
- `cartesia:f786b574-daa5-4673-aa0c-cbe3e8534c02`
- `elevenlabs:21m00Tcm4TlvDq8ikWAM`

This key is never shown to the user. The display name + provider label
is all the user sees.

---

## 5. Live Preview

### 5.1 Preview text input

A single shared text input sits above the voice card grid. The user types
once; any card's ▶ button synthesises that text in that card's voice.

- **Default text:** *"Good evening. How can I help you today?"*
- **Max characters:** 200 (keeps preview cost negligible; matches the
  Voxtera target of <200 chars per average bot response)
- **Character counter:** shown next to the input, turns red at 180+

### 5.2 Preview request flow

The preview endpoint streams audio bytes back to the browser as they
arrive from the provider — no intermediate file, no download URL. This
matches the real-time pipeline behaviour already in Pipecat and avoids
a perceived delay before audio starts.

```
Browser → POST /api/tts/preview
            { voice_key: "google:Kore", text: "...", property_id: "..." }
          → Backend resolves provider from voice_key
          → Backend calls provider TTS API (API key never leaves server)
          → Backend pipes audio chunks back as they arrive
            (Content-Type: audio/mpeg, Transfer-Encoding: chunked)
          → Browser decodes stream via Web Audio API and begins
            playback as soon as the first chunk arrives
```

**FastAPI implementation note:** Use `StreamingResponse` with
`media_type="audio/mpeg"`. Each provider streams differently:
- Google: `synthesize_speech()` returns bytes synchronously — wrap in
  a single-chunk generator.
- Cartesia: native streaming via WebSocket or SSE — pipe chunks directly.
- ElevenLabs: streaming endpoint at `/v1/text-to-speech/{voice_id}/stream`
  — pipe the response body directly.

### 5.3 Preview states

| State | Card UI |
|---|---|
| Idle | ▶ button enabled |
| Loading | ▶ button shows spinner, disabled |
| Playing | ▶ becomes ■ (stop); clicking stops playback |
| Error | ▶ button shows ✕ briefly, resets after 2s |

Only one voice can preview at a time. Starting a new preview stops any
currently playing audio.

### 5.4 Rate limiting

Preview endpoint: max 10 requests per minute per user session. If exceeded,
show inline message: *"Too many previews — wait a moment."* This prevents
accidental API cost spikes during onboarding sessions.

---

## 6. Parameter Drawer

### 6.1 Behaviour

Selecting any voice card slides a drawer in from the right edge of the
page. The card grid remains fully visible and scrollable behind it —
the user can dismiss the drawer, select a different card, and the drawer
updates to that voice without closing. The drawer never blocks the grid.

Drawer width: ~380px on desktop. On mobile, full-screen sheet from bottom.

### 6.2 Drawer anatomy

```
┌─────────────────────────────────┐
│  Kore  (Google)          ✕      │  ← voice name + close
│  Female · 31 languages          │  ← metadata strip
├─────────────────────────────────┤
│  PARAMETERS                     │
│                                 │
│  [Google has no configurable    │
│   parameters for Chirp 3 HD.    │
│   The voice sounds exactly as   │
│   previewed.]                   │
│                                 │  ← empty state for Google
├─────────────────────────────────┤
│  PREVIEW                        │
│  ┌─────────────────────────┐    │
│  │ Good evening. How can…  │    │  ← mirrors shared input
│  └─────────────────────────┘    │
│  [▶ Preview]                    │
├─────────────────────────────────┤
│  [Reset to defaults]            │
│  [Reset to saved]               │
│                      [Save]     │
└─────────────────────────────────┘
```

For providers with parameters (Cartesia, ElevenLabs), the parameters
section fills with the controls defined in Section 6.3.

### 6.3 Parameter controls per provider

#### Google Chirp 3 HD

No configurable parameters. Show informational message:
*"Chirp 3 HD voices have no adjustable parameters — what you hear in
the preview is exactly what your guests will hear."*

#### Cartesia Sonic-3

| Parameter | Control | Range | Default | Label shown to user |
|---|---|---|---|---|
| `speed` | Slider | Slowest → Fastest (5 steps) | Normal | Speaking speed |
| `emotion` | Multi-select chips | 6 emotions × 5 levels | None | Emotional tone |
| `volume` | Slider | -50% → +50% | 0 | Volume adjustment |
| `pronunciation_dict_id` | Text input (ID) | UUID string | Empty | Pronunciation dictionary ID |

**Emotion chip UX:** Show 6 emotion chips (Neutral, Positive, Excited,
Sad, Angry, Curious). Tapping a chip cycles through intensity levels:
off → Low → Medium → High → off. Active chips show the level label.
Multiple emotions can be active simultaneously.

**Pronunciation dict:** Small text field, labelled *"Custom pronunciation
dictionary ID (optional)"*. Staff-facing in practice — operators will
leave this blank. No validation in v1 beyond UUID format check.

#### ElevenLabs Flash v2.5

| Parameter | Control | Range | Default | Label shown to user |
|---|---|---|---|---|
| `stability` | Slider | 0 – 100% | 71% | Consistency |
| `similarity_boost` | Slider | 0 – 100% | 75% | Voice clarity |
| `style` | Slider | 0 – 100% | 0% | Style intensity |
| `use_speaker_boost` | Toggle | on / off | on | Speaker boost |
| `speed` | Slider | 0.7× – 1.2× | 1.0× | Speaking speed |

**Helper text under each ElevenLabs slider:**
- Consistency: *"Higher = more stable, lower = more expressive"*
- Voice clarity: *"How closely the voice matches the original character"*
- Style intensity: *"Keep at 0 for professional use"*
- Speaker boost: *"Improves clarity — slight latency increase"*

### 6.4 Auto-preview behaviour

When the user stops moving any slider or changes any control, a 1-second
debounce timer starts. After 1 second of inactivity, a preview
synthesises automatically using the current parameter values and the
shared preview text. The ▶ Preview button in the drawer triggers the same
synthesis immediately on click, bypassing the debounce.

Auto-preview fires only if a preview text string is present. If the
preview input is empty, auto-preview is suppressed and the user sees a
hint: *"Enter preview text above to hear changes automatically."*

Only one synthesis can be in flight at a time — starting a new one
(auto or manual) cancels any currently playing audio.

### 6.5 Reset buttons

Two reset options appear at the bottom of the drawer above the Save button:

| Button | Action |
|---|---|
| Reset to defaults | Restores all parameters to the provider's documented defaults (hardcoded in frontend). Does not save — user must still click Save. |
| Reset to saved | Restores all parameters to the last saved values for this property. Does not save — used to undo unsaved edits. |

Both buttons are always visible. "Reset to saved" is disabled (greyed)
if the current drawer state already matches the saved state.

### 6.6 Unsaved changes guard

If the user clicks ✕ or selects a different voice card while the drawer
has unsaved parameter changes, show a confirmation prompt:
*"You have unsaved changes to [Voice name]. Discard them?"*
with options: **Discard** · **Keep editing**.

---

## 7. Filters

Filter bar appears above the card grid.

## 8. Filters

Filter bar appears above the card grid.

| Filter | Options |
|---|---|
| Provider | All (default) · Google · Cartesia · ElevenLabs |
| Gender | All (default) · Female · Male · Neutral |

Filters are additive (AND). Selected filters persist for the session.
Resetting to All is one click. No search bar in v1 — the list is short
enough (~50–70 voices) to browse visually.

---

## 9. Save & Persistence

### 9.1 Save button behaviour

- Lives inside the parameter drawer, not on the main page.
- Disabled (greyed) when drawer state matches the currently saved config.
- Enabled when voice or any parameter differs from saved state.
- On click: POST to backend, then show success toast and close drawer.
- On failure: show error toast inside drawer, do not close or clear state.

### 9.2 Preview endpoint contract

```
POST /api/tts/preview

Request body:
{
  "voice_key":    "cartesia:f786b574-daa5-4673-aa0c-cbe3e8534c02",
  "text":         "Good evening. How can I help you today?",
  "property_id":  "hotel_casa_lisboa",
  "params": {
    // Provider-specific — only fields relevant to the voice's provider
    // Google:     {}  (no params)
    // Cartesia:   { "speed": 0.2, "emotion": ["positivity:high"] }
    // ElevenLabs: { "stability": 0.71, "similarity_boost": 0.75,
    //               "style": 0.0, "use_speaker_boost": true, "speed": 1.0 }
  }
}

Response: audio/mpeg stream (chunked)
Response 429: rate limit exceeded
Response 422: invalid params
```

### 9.3 Save endpoint contract

```
POST /api/voice/config

Request body:
{
  "property_id":  "hotel_casa_lisboa",
  "voice_key":    "cartesia:f786b574-daa5-4673-aa0c-cbe3e8534c02",
  "display_name": "Jameson",
  "provider":     "cartesia",
  "model":        "sonic-3",
  "params": {
    "speed":   0.2,
    "emotion": ["positivity:high", "curiosity:low"]
  }
}

Response 200:
{
  "property_id": "hotel_casa_lisboa",
  "voice_key":   "cartesia:f786b574-daa5-4673-aa0c-cbe3e8534c02",
  "updated_at":  "2026-06-08T14:23:00Z"
}

Response 422: validation error
Response 404: property not found
```

### 9.4 Config object stored per property

Parameters are stored alongside the voice key. The Pipecat pipeline reads
the full config at call initialisation and passes params directly to the
provider SDK.

```json
{
  "property_id":  "hotel_casa_lisboa",
  "voice_key":    "cartesia:f786b574-daa5-4673-aa0c-cbe3e8534c02",
  "display_name": "Jameson (Cartesia)",
  "provider":     "cartesia",
  "model":        "sonic-3",
  "params": {
    "speed":   0.2,
    "emotion": ["positivity:high", "curiosity:low"]
  },
  "updated_at":  "2026-06-08T14:23:00Z",
  "updated_by":  "user_id_or_staff_id"
}
```

Google config example (no params):
```json
{
  "property_id":  "hotel_casa_lisboa",
  "voice_key":    "google:Kore",
  "display_name": "Kore (Google)",
  "provider":     "google",
  "model":        "chirp3-hd",
  "params":       {},
  "updated_at":   "2026-06-08T14:23:00Z",
  "updated_by":   "user_id_or_staff_id"
}
```

### 9.5 Rollback

If an operator selects a voice that performs poorly after go-live, they
revert by returning to this panel, selecting the previous voice, and
saving. There is no automatic rollback and no Voxtera staff intervention
required.

To support this, the backend stores the **last two voice configs** per
property (current + previous), including their full params objects.
The panel surfaces the previous voice with a *"Previously used"* label
so the operator can restore it — and its exact parameters — in one click.

---

## 10. Loading & Error States

| Scenario | UI behaviour |
|---|---|
| API fetch in progress | Skeleton cards shown, filter bar disabled |
| One provider API fails | That provider's voices omitted; inline warning: *"Cartesia voices unavailable — check back later."* Other providers load normally. |
| All providers fail | Empty state with retry button |
| Preview API unreachable | Error toast inside drawer: *"Preview unavailable. Check your connection."* |
| Auto-preview debounce in flight | Subtle spinner on ▶ button, no blocking UI |
| Save fails | Error toast inside drawer: *"Couldn't save. Try again."* Drawer stays open. |
| Drawer closed with unsaved changes | Confirmation prompt before discard |

---

## 11. Out of Scope — v1

These are explicitly deferred. Do not build them now.

- Per-language voice override (one voice per language code)
- Voice cloning or custom voice upload
- ElevenLabs full shared library (10,000+ voices)
- A/B testing two voices across guest sessions
- Admin analytics (which voice is most selected across properties)
- Multi-property bulk voice update
- Cartesia `pronunciation_dict_id` UI (field present but manual text entry only)

---

## 12. Decisions Log

| # | Question | Decision |
|---|---|---|
| 1 | Cartesia scope — full library or curated? | Manually maintained allowlist of ~25–35 voices vetted for hospitality tone. Full library fetched, then filtered. |
| 2 | Preview — stream or download URL? | Stream. `StreamingResponse` from FastAPI, Web Audio API on the client. Playback begins on first chunk. |
| 3 | Dashboard IA — tab or standalone page? | Standalone page at `/admin/voice/`. Entry point is a card on the admin dashboard index. |
| 4 | Operator access before or after onboarding? | After onboarding only. Panel is staff-only until property is marked live. |
| 5 | Rollback process? | Self-serve via this panel. Backend stores current + previous voice config including full params. Panel surfaces previous voice with one-click restore. |
| 6 | Where do parameters appear? | Right-side drawer. Slides in on voice select. Card grid remains visible behind it. |
| 7 | Who can see parameters? | All users — no role gating on parameter controls. |
| 8 | How does preview work in the drawer? | Auto-preview 1s after slider stop + manual ▶ button. Both use current param values. |
| 9 | What does Reset do? | Two separate buttons: Reset to provider defaults and Reset to last saved. Neither saves automatically. |

---

*Voxtera · Voice Configuration Panel PRD v0.3 · June 2026 · Draft*