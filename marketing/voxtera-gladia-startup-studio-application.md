# Voxtera — Gladia Startup Studio Application

Prepared 2026-05-22. Copy-paste-ready answers for the Gladia Startup Studio
application form: https://share-eu1.hsforms.com/2REBVE0JJTUKdHbWxUvQ_nAfe6wg

---

## Eligibility snapshot

| Criterion | Gladia requirement | Voxtera | Status |
|---|---|---|---|
| Geography | US or EU-based (FAQ: teams worldwide welcome) | Canada | OK — apply, see "Timing & notes" |
| Stage | Pre-seed to Series A, <$20M raised or bootstrapped | Pre-revenue, building MVP, no outside funding | OK |
| Team size | 2–25 people, ideally a technical founder | 2–3 people, technical founder | OK |
| Use case | Building with real-time speech-to-text | Live multilingual voice agent | Strong fit |
| Gladia plan | Must NOT currently hold a paid plan | On Gladia free trial only | OK |

We aim for the **first prize** (a full year of transcription — up to 150 parallel
streams, 2,500 hours/month), but every tier directly helps the pilot rollout.

---

## Application answers

### Company / product name
Voxtera

### Website
[voxtera.ai — confirm your live URL, or leave blank if the site isn't public yet]

### Contact
[Your name] · [best contact email] · Founder

> Tip: use a company-domain email if you have one; it reads more credibly than a
> generic Gmail on a startup application.

### One-line description
Voxtera is a real-time multilingual voice agent for the tourism industry — a
traveler speaks in any language, and the agent instantly detects it and answers
back in that same language.

### What we're building
Voxtera is a voice concierge for travelers. A guest just starts talking — no menu,
no "press 1 for English," no language pre-selection. Voxtera detects the language
from the first utterance and holds the entire conversation in that language, with
natural turn-taking and interruption handling.

It answers the questions a traveler actually asks on the ground: hotels and
check-in, nearby attractions, transport and directions, dining, safety, cultural
etiquette, and local events. It's built to deploy three ways from one core: an
embeddable web widget, a mobile SDK, and a phone line (via Twilio), so a hotel or
destination can offer it on whatever channel their guests already use.

The agent persona is a warm, polished concierge — friendly and human, not a
robotic IVR.

### Who it's for
Two audiences. The **buyers** are hotels, resorts, and destination/tourism
operators who serve international guests and can't realistically staff a
multilingual front desk around the clock. The **end users** are travelers — often
speaking a language no local staff member knows — who need fast, reliable answers
in their own language. Our go-to-market starts with a founding cohort of pilot
hotels.

### Where real-time speech-to-text fits
Speech-to-text is the foundation of the entire product, not a feature bolted on.
Voxtera's core promise — zero language pre-selection, instant detection, and a
consistent same-language conversation — is only possible with real-time,
truly multilingual transcription.

We run Gladia's **Solaria-1** model for real-time STT. We chose it specifically
because it transcribes and auto-detects across the full long tail of languages a
global tourism product has to cover — the alternatives we evaluated topped out at
roughly ten languages, which simply doesn't work for travelers. Solaria-1's
sub-300ms latency is what keeps conversations feeling natural and makes
barge-in / interruption handling possible. In short: Gladia is the component that
makes Voxtera's central feature real.

Pipeline: Gladia Solaria-1 (real-time STT) → Claude (LLM, tourism reasoning + RAG)
→ Google Chirp 3 HD (TTS), orchestrated with Pipecat, Silero VAD for turn-taking,
and Daily.co / Twilio for transport.

### Current status / prototype
In active development. We have a working end-to-end voice loop and a live demo
running through Telegram that shows real-time multilingual conversation with
Gladia Solaria-1 wired in. We're pre-revenue with no paying customers yet; the
immediate focus is launching a founding cohort of pilot hotels and a
destination-specific knowledge layer (RAG) so the agent gives accurate,
local answers.

### Path to scale
Our pilot campaign puts Voxtera in front of hotels as a founding cohort. Each
hotel deployment generates concurrent guest calls across the web widget and phone
line, so usage scales with the number of properties live. A full year of
real-time transcription would cover the entire pilot cohort and early commercial
rollout without STT cost being the constraint on how fast we can onboard hotels —
letting a 2–3 person team put every hour into the product. Beta access to new
real-time features and the quarterly engineering sessions would directly help us
tune latency and widen language coverage as we grow.

### Team
2–3 people, with a technical founder leading the build. Canada-based.

### Demo / assets to share
- Live demo link: **[paste the Telegram demo link here]**
- Telegram screenshots: attach the screenshots showing a multilingual conversation
- Code repository: github.com/pokemonnode34-byte/voxtera (private — offer access on request)

---

## Timing & notes

- **Deadline:** the Startup Studio page advertises an application deadline of
  **November 26th** with winners announced by January — that's the 2025 cycle,
  which has now closed. The application form is still live, and Gladia states the
  Startup Studio "will evolve into a permanent program." Recommended: submit the
  form anyway to get on their radar, and optionally ping them on Discord
  (discord.com/invite/UUd79ckzz9) to ask when the next cohort opens.
- **Canada:** the eligibility box names "US and EU-based" startups, but Gladia's
  own FAQ explicitly says the Studio is "open to teams worldwide." No need to
  hide the location — apply normally.
- **No paid plan:** eligibility requires that you do NOT currently hold a paid
  Gladia plan. Voxtera is on the free trial, so this is fine — just don't upgrade
  to a paid plan before applying.

## Before you submit — fill these in
1. Website URL (or remove the field)
2. Your name and contact email
3. The Telegram demo link
4. Attach the Telegram screenshots
