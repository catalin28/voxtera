# Voxtera — Phone Testing Setup (Pattern A: PIN Dial-In)

A complete, copy-pasteable guide to wire your Pipecat bot to a real phone number using Daily.co's PSTN dial-in with PIN entry. **Goal: dial a phone number from your mobile, hear your Voxtera bot answer.**

> **Use this for:** local testing, internal demos, founder demos to one or two hotel prospects.
> **Don't use this for:** real hotel customers. PIN entry is bad UX for guests. Pattern B (pinless + webhook) is the production path — covered in a separate document.

---

## Table of contents

1. [What you'll build](#1-what-youll-build)
2. [Prerequisites](#2-prerequisites)
3. [Step 1 — Confirm your Daily account is dial-in ready](#step-1--confirm-your-daily-account-is-dial-in-ready)
4. [Step 2 — Buy a phone number](#step-2--buy-a-phone-number)
5. [Step 3 — Create a dial-in enabled room](#step-3--create-a-dial-in-enabled-room)
6. [Step 4 — Bind the phone number to the room](#step-4--bind-the-phone-number-to-the-room)
7. [Step 5 — Update your Pipecat bot to use the room](#step-5--update-your-pipecat-bot-to-use-the-room)
8. [Step 6 — Tune VAD for phone audio](#step-6--tune-vad-for-phone-audio)
9. [Step 7 — End-to-end test](#step-7--end-to-end-test)
10. [Troubleshooting](#troubleshooting)
11. [Cleanup & cost control](#cleanup--cost-control)
12. [What's next](#whats-next)

---

## 1. What you'll build

```
+---------------+        +-----------+        +---------------------+
|  Your phone   |  PSTN  |  Daily.co |  WebRTC|  Pipecat bot        |
|  (any mobile) | -----> |  network  | -----> |  (your machine /    |
|               |        |  + PIN    |        |   DigitalOcean)     |
+---------------+        +-----------+        +---------------------+
                              |                         |
                              |   binds number → room   |
                              v                         |
                         +-----------+                  |
                         | Daily Room|<-----------------+
                         |  (dial-in |    bot already here, waiting
                         |   PIN'd)  |
                         +-----------+
```

You dial → Daily prompts for a PIN → you enter the PIN → you're inside a Daily room → bot greets you → conversation happens through the existing Whisper → Claude → Chirp 3 HD pipeline.

---

## 2. Prerequisites

Before you start, you should have:

- [ ] An active **Daily.co account** with a credit card on file (the phone-number API is pay-as-you-go and won't work on free-tier-only accounts)
- [ ] Your **Daily API key** — find it at https://dashboard.daily.co/developers
- [ ] Your **Daily domain name** — the prefix in your room URLs (e.g. if your rooms look like `https://voxtera.daily.co/foo`, your domain is `voxtera`)
- [ ] A working **Pipecat bot** that runs against a manually-created Daily room (your current demo setup is fine)
- [ ] Python 3.12+ and the existing project's `.env` file
- [ ] A **mobile phone** with a plan that allows outbound calls to the country where you'll buy the number (US/Canada is cheapest from most plans)
- [ ] Required env vars from your existing bot:
  - `DAILY_API_KEY`
  - `OPENAI_API_KEY` (for Whisper)
  - `ANTHROPIC_API_KEY` (for Claude)
  - `GOOGLE_APPLICATION_CREDENTIALS` (for Chirp 3 HD)

**Budget for this test:** ~$2 for the number rental (prorated for the first month) + ~$0.02 per minute of test calls. A full afternoon of testing costs under $5.

---

## Step 1 — Confirm your Daily account is dial-in ready

Before buying a number, sanity-check that your account can use the API.

### 1.1 — Check API connectivity

```bash
curl -X GET "https://api.daily.co/v1/" \
  -H "Authorization: Bearer $DAILY_API_KEY"
```

You should get a JSON response describing your domain config. If you get `401 Unauthorized`, your API key is wrong or expired.

### 1.2 — Confirm a payment method is on file

Open https://dashboard.daily.co/billing and verify you see a card listed. **Without this, the `/buy-phone-number` endpoint will fail.** Free-tier-only accounts get a `403`.

### 1.3 — Decide on a region

The buy-phone-number API today only provisions numbers in **US and Canada (+1)**. Pick whichever makes sense for testing. From Canada, a Canadian number is slightly cheaper to dial.

---

## Step 2 — Buy a phone number

### 2.1 — List available numbers

Pick an area code that makes sense for your test region. `415` (San Francisco), `212` (NYC), `416` (Toronto), `613` (Ottawa), etc.

```bash
curl -X GET "https://api.daily.co/v1/list-available-numbers?areacode=415" \
  -H "Authorization: Bearer $DAILY_API_KEY"
```

Response looks like:

```json
{
  "available_numbers": [
    "+14155551234",
    "+14155555678",
    ...
  ]
}
```

Copy one of the numbers — let's say `+14155551234`.

### 2.2 — Buy it

```bash
curl -X POST "https://api.daily.co/v1/buy-phone-number" \
  -H "Authorization: Bearer $DAILY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"number": "+14155551234"}'
```

Response includes an `id` you'll need later — save it:

```json
{
  "id": "abc123-def456-...",
  "number": "+14155551234",
  "status": "active"
}
```

### 2.3 — Save it to your `.env`

Add to your project's `.env`:

```bash
VOXTERA_TEST_PHONE_NUMBER=+14155551234
VOXTERA_TEST_PHONE_ID=abc123-def456-...
```

> **Tip:** if you don't care which number you get, you can omit the `number` field from the POST and Daily picks a random California number. Faster for testing.

### 2.4 — Verify ownership

```bash
curl -X GET "https://api.daily.co/v1/purchased-phone-numbers" \
  -H "Authorization: Bearer $DAILY_API_KEY"
```

You should see your number listed.

---

## Step 3 — Create a dial-in enabled room

### 3.1 — Why a dedicated room

For Pattern A, we use **one persistent room** that the number always routes to. The bot lives in this room. Anyone who dials in lands here.

This is fine for testing. In production (Pattern B), you'll create a fresh room per call.

### 3.2 — Create the room

```bash
curl -X POST "https://api.daily.co/v1/rooms" \
  -H "Authorization: Bearer $DAILY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "voxtera-phone-test",
    "properties": {
      "enable_dialin": true,
      "start_video_off": true,
      "exp": null
    }
  }'
```

Response — **note the `dialin_pin` value**, you'll need it to actually dial in:

```json
{
  "id": "...",
  "name": "voxtera-phone-test",
  "url": "https://YOUR-DOMAIN.daily.co/voxtera-phone-test",
  "config": {
    "enable_dialin": true,
    "dialin_pin": "12345678901",
    ...
  }
}
```

### 3.3 — Save it

Add to your `.env`:

```bash
VOXTERA_TEST_ROOM_NAME=voxtera-phone-test
VOXTERA_TEST_ROOM_URL=https://YOUR-DOMAIN.daily.co/voxtera-phone-test
VOXTERA_TEST_ROOM_PIN=12345678901
```

The PIN is **11 digits** by default. Yes, that's a lot to dial. It's a test setup, not customer-facing — bear with it.

> If you forget the PIN later, fetch it again:
> ```bash
> curl -X GET "https://api.daily.co/v1/rooms/voxtera-phone-test" \
>   -H "Authorization: Bearer $DAILY_API_KEY"
> ```

---

## Step 4 — Bind the phone number to the room

Buying a number and creating a room aren't enough — Daily doesn't know they're related yet. You need a `domainDialinConfig` that connects them.

### 4.1 — Create the binding

```bash
curl -X POST "https://api.daily.co/v1/domainDialinConfig" \
  -H "Authorization: Bearer $DAILY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "phone_number": "+14155551234",
    "room_name": "voxtera-phone-test",
    "type": "pin_dialin"
  }'
```

Response confirms the binding:

```json
{
  "id": "config-...",
  "phone_number": "+14155551234",
  "room_name": "voxtera-phone-test",
  "type": "pin_dialin"
}
```

### 4.2 — Verify the binding

```bash
curl -X GET "https://api.daily.co/v1/domainDialinConfig" \
  -H "Authorization: Bearer $DAILY_API_KEY"
```

You should see your number → room mapping in the response. **If this step is missed, dialing the number will just ring forever.** This is the single most common mistake.

---

## Step 5 — Update your Pipecat bot to use the room

### 5.1 — Update the room URL

Find wherever your existing bot creates the `DailyTransport`. Change the `room_url` to the test room you just created.

```python
# bot.py — minimal example
import os
import asyncio
from loguru import logger
from dotenv import load_dotenv

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineTask
from pipecat.pipeline.runner import PipelineRunner
from pipecat.transports.services.daily import DailyTransport, DailyParams
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.google.tts import GoogleTTSService
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext

load_dotenv()

ROOM_URL = os.environ["VOXTERA_TEST_ROOM_URL"]


async def run_bot() -> None:
    """Run the Voxtera bot in the phone-test room."""
    transport = DailyTransport(
        room_url=ROOM_URL,
        token=None,                       # no token needed for owned domain
        bot_name="Voxtera",
        params=DailyParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_enabled=True,
            vad_analyzer=SileroVADAnalyzer(),
            transcription_enabled=False,  # we do STT ourselves via Whisper
        ),
    )

    stt = OpenAISTTService(
        api_key=os.environ["OPENAI_API_KEY"],
        model="whisper-1",
    )

    llm = AnthropicLLMService(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        model="claude-sonnet-4-6",
    )

    tts = GoogleTTSService(
        credentials_path=os.environ["GOOGLE_APPLICATION_CREDENTIALS"],
        voice_id="en-US-Chirp3-HD-Charon",  # will switch per detected language
        sample_rate=24000,
    )

    context = OpenAILLMContext(messages=[{
        "role": "system",
        "content": (
            "You are Voxtera, a multilingual hotel concierge. "
            "Detect the caller's language from their first message and "
            "respond in the same language for the entire conversation. "
            "Keep responses warm, brief, under 30 words. "
            "If asked something you don't know, offer to connect them to the front desk."
        ),
    }])

    pipeline = Pipeline([
        transport.input(),
        stt,
        llm,
        tts,
        transport.output(),
    ])

    task = PipelineTask(pipeline)

    @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(transport, participant):
        logger.info(f"📞 Caller joined: {participant.get('id')}")
        # Greet the caller immediately. Language-agnostic greeting.
        await tts.say("Hello. How can I help you today?")

    @transport.event_handler("on_participant_left")
    async def on_participant_left(transport, participant, reason):
        logger.info(f"👋 Caller left: {reason}")
        await task.cancel()

    runner = PipelineRunner()
    logger.info(f"🤖 Bot joining {ROOM_URL}")
    await runner.run(task)


if __name__ == "__main__":
    asyncio.run(run_bot())
```

### 5.2 — Critical adjustments for phone audio

Three changes from typical web-WebRTC bot code:

1. **`transcription_enabled=False`** — let Whisper handle STT, not Daily's built-in transcription. Daily's transcription is fine but doesn't auto-detect 99 languages.
2. **Greet on first participant join** — the caller will be confused by silence after entering the PIN. The bot must speak first.
3. **End the pipeline cleanly on hangup** — without this, the bot stays in the room burning minutes after the caller leaves. The `on_participant_left` handler cancels the task.

---

## Step 6 — Tune VAD for phone audio

This is where most first-time tests go wrong. **Phone audio is fundamentally different from WebRTC audio**, and the defaults Pipecat ships with are tuned for the latter.

### 6.1 — Why phone audio is harder

| | WebRTC (your demo) | Phone (PSTN) |
|---|---|---|
| Sample rate | 48 kHz | 8 kHz (narrowband) |
| Codec | Opus (high quality) | μ-law / G.711 (compressed) |
| Background noise | Usually quiet | Often noisy (street, café, lobby) |
| Echo | Minimal (headphones) | Common (speakerphone, hands-free) |

This means: VAD may trigger on background noise, may cut off the caller mid-sentence, or may keep listening forever because it didn't detect the end of speech clearly.

### 6.2 — Recommended VAD tuning

Update your VAD analyzer:

```python
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams

vad_analyzer = SileroVADAnalyzer(
    params=VADParams(
        stop_secs=1.2,           # was 0.8 for WebRTC; bump for phone
        confidence=0.5,          # default 0.5, lower if cutting off mid-sentence
        start_secs=0.2,          # how long sustained voice before "speech started"
        min_volume=0.6,          # raise if background noise triggers VAD
    )
)
```

Three knobs to remember:

- **`stop_secs`** — how long silence must last to consider the caller done speaking. Too low → bot interrupts. Too high → awkward pauses.
- **`confidence`** — VAD's threshold for declaring "this is speech." Too high → misses quiet voices. Too low → background noise gets transcribed.
- **`min_volume`** — quiet phone environments need this low; noisy ones need it higher.

You will tune these by ear. Plan for 30 minutes of trial-and-error on real phone calls.

### 6.3 — A quick AB test

Run two test calls with different `stop_secs`:

1. `stop_secs=0.8` — speak naturally, see if bot interrupts you mid-thought
2. `stop_secs=1.5` — speak naturally, see if the bot's response feels slow

The right value is whichever feels closest to how a human concierge would respond. For most testers, **1.0–1.2 is the sweet spot for phone audio**.

---

## Step 7 — End-to-end test

Time to actually make this work.

### 7.1 — Pre-flight checklist

Before dialing:

- [ ] `.env` has all the required keys (Daily, OpenAI, Anthropic, Google creds)
- [ ] `domainDialinConfig` exists for your number → room mapping (verify with the GET from step 4.2)
- [ ] Bot script runs without errors in a terminal
- [ ] You have the room's PIN ready to type

### 7.2 — Start the bot first

```bash
cd /path/to/voxtera
python bot.py
```

Wait for the log line:

```
🤖 Bot joining https://YOUR-DOMAIN.daily.co/voxtera-phone-test
```

Then verify the bot is actually in the room — open the room URL in a browser tab. You should see a participant named "Voxtera" in the room. **If you don't see the bot in the room, do not dial yet.** Fix the bot first.

> **Order matters.** The bot must be in the room *before* the caller arrives. If the caller lands in an empty room, they hear nothing and assume the system is broken.

### 7.3 — Dial the number

1. Pick up your phone
2. Dial the number from `VOXTERA_TEST_PHONE_NUMBER` (e.g. `+14155551234`)
3. You'll hear a Daily voice prompt: *"Welcome. Please enter your PIN, then press pound."*
4. Type the 11-digit PIN from `VOXTERA_TEST_ROOM_PIN`, then `#`
5. You'll hear a confirmation tone and you're in
6. The bot should say: *"Hello. How can I help you today?"*
7. Speak. Try in different languages — *"Bonjour, où est la piscine?"*, *"¿Hay servicio de habitaciones?"*, *"今晩、レストランは開いていますか?"*
8. Verify the bot responds **in the language you spoke**

### 7.4 — What to watch in the logs

In your bot's terminal, you should see:

```
📞 Caller joined: abc123...
[STT] Transcribed (es-ES): ¿Hay servicio de habitaciones?
[LLM] Response: Sí, el servicio de habitaciones está disponible las 24 horas...
[TTS] Synthesizing 47 chars in es-ES with es-ES-Chirp3-HD-...
```

If any layer fails, the log will tell you which.

### 7.5 — End the test

Hang up your phone. The bot should log:

```
👋 Caller left: REASON_LEFT
```

…and the Python process should exit cleanly. If it doesn't, the `on_participant_left` handler isn't firing — check the event name spelling.

---

## Troubleshooting

The five failures you're statistically most likely to hit, in order of frequency:

### "I dial the number and it just rings forever"

**Cause:** the `domainDialinConfig` binding (step 4) wasn't created, or it points to a room name that doesn't exist.

**Fix:**
```bash
curl -X GET "https://api.daily.co/v1/domainDialinConfig" \
  -H "Authorization: Bearer $DAILY_API_KEY"
```
Verify your number is bound to your room name. If not, re-run step 4.

### "It says my PIN is invalid"

**Cause:** wrong PIN — usually you typed the room name, or copied a stale PIN from an older room.

**Fix:** re-fetch the current PIN:
```bash
curl -X GET "https://api.daily.co/v1/rooms/voxtera-phone-test" \
  -H "Authorization: Bearer $DAILY_API_KEY" | jq '.config.dialin_pin'
```

### "I'm in the room but the bot doesn't respond when I speak"

**Cause #1:** the bot isn't actually in the room. Open the room URL in a browser — do you see "Voxtera" as a participant?

**Cause #2:** VAD never triggers because phone audio is too quiet or too noisy. Lower `min_volume` to `0.4`, lower `confidence` to `0.4`, and try again.

**Cause #3:** STT/LLM/TTS API key is wrong or rate-limited. Check the bot's terminal — there should be a stack trace.

### "The bot interrupts me mid-sentence"

**Cause:** `stop_secs` is too low. Bump it from `0.8` to `1.2` or `1.5`.

### "The bot responds but the audio is robotic or choppy"

**Cause:** sample-rate mismatch between Chirp 3 HD output (24000 Hz) and what Daily expects for telephony (8000 Hz μ-law). Daily should resample automatically, but if you see issues, force the TTS output to 16000 Hz in the GoogleTTSService config and see if it improves.

### "The call ends but the bot keeps running"

**Cause:** `on_participant_left` event handler isn't firing or doesn't cancel the task. Check the event name and add a `task.cancel()` call.

---

## Cleanup & cost control

Don't leave test resources running. Phone numbers cost $2/month each, rooms are free but clutter the dashboard.

### Release the test number when you're done

```bash
curl -X DELETE "https://api.daily.co/v1/release-phone-number/$VOXTERA_TEST_PHONE_ID" \
  -H "Authorization: Bearer $DAILY_API_KEY"
```

> **Caution:** once released, you usually can't get the same number back. If this number was ever shared with anyone, hold it for at least 30 days before releasing.

### Delete the test room (optional)

```bash
curl -X DELETE "https://api.daily.co/v1/rooms/voxtera-phone-test" \
  -H "Authorization: Bearer $DAILY_API_KEY"
```

### Cost reminder

| Item | Cost |
|---|---|
| Phone number rental | ~$2/month per number |
| Inbound minutes (PSTN dial-in) | $0.018/min (Pipecat Cloud tier) or $0.025/min (Daily Bots tier) |
| Whisper STT (OpenAI) | $0.006/min |
| Claude Sonnet 4.6 | per-token, ~$0.01-0.03 per minute of conversation |
| Chirp 3 HD TTS | $30 per 1M chars (1M free/month) |

**A 5-minute test call costs roughly $0.15–$0.25 total.** Don't be afraid to make many test calls — this is the cheapest part of the project.

---

## What's next

Once Pattern A works reliably, you're ready for:

1. **Pattern B: Pinless dial-in via webhook** — production-grade routing without PIN entry. Each call gets a fresh room. Required before onboarding any real customer.
2. **Per-property routing** — map each Daily number to a hotel's knowledge base, voice, and PMS. The dialed number becomes the routing key.
3. **Bot worker pool** — pre-warmed Pipecat processes that join rooms in <500ms instead of cold-starting.
4. **Call lifecycle management** — room cleanup, orphan-call sweeper, escalation routing, post-call summaries to PMS.
5. **Daily.co international numbers** — email `help@daily.co` to inquire about provisioning EU numbers if you need them for European hotel customers.

Each of these is its own document. **Pattern A is the foundation — don't move on until you can reliably dial in, get a multilingual response, and hang up cleanly five times in a row.**

---

## Appendix: full bot.py reference

The minimum working bot for Pattern A. Copy this into your project:

```python
"""
bot.py — Voxtera Phone Test (Pattern A: PIN dial-in).
Prerequisites: VOXTERA_TEST_ROOM_URL set in .env, room already configured for dial-in,
phone number bound to the room via domainDialinConfig.
"""
import asyncio
import os
from loguru import logger
from dotenv import load_dotenv

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineTask
from pipecat.pipeline.runner import PipelineRunner
from pipecat.transports.services.daily import DailyTransport, DailyParams
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.google.tts import GoogleTTSService
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext

load_dotenv()

ROOM_URL = os.environ["VOXTERA_TEST_ROOM_URL"]
DEFAULT_VOICE = "en-US-Chirp3-HD-Charon"


async def run_bot() -> None:
    """Run the Voxtera bot in the phone-test room. Pattern A: PIN dial-in."""

    transport = DailyTransport(
        room_url=ROOM_URL,
        token=None,
        bot_name="Voxtera",
        params=DailyParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_enabled=True,
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(
                    stop_secs=1.2,
                    confidence=0.5,
                    start_secs=0.2,
                    min_volume=0.6,
                )
            ),
            transcription_enabled=False,
        ),
    )

    stt = OpenAISTTService(
        api_key=os.environ["OPENAI_API_KEY"],
        model="whisper-1",
    )
    llm = AnthropicLLMService(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        model="claude-sonnet-4-6",
    )
    tts = GoogleTTSService(
        credentials_path=os.environ["GOOGLE_APPLICATION_CREDENTIALS"],
        voice_id=DEFAULT_VOICE,
        sample_rate=24000,
    )

    context = OpenAILLMContext(messages=[{
        "role": "system",
        "content": (
            "You are Voxtera, a multilingual hotel concierge. "
            "Detect the caller's language from their first message and "
            "respond in the same language for the entire conversation. "
            "Keep responses warm, brief, under 30 words. "
            "If asked something you don't know, offer to connect them to the front desk."
        ),
    }])

    pipeline = Pipeline([
        transport.input(),
        stt,
        llm,
        tts,
        transport.output(),
    ])

    task = PipelineTask(pipeline)

    @transport.event_handler("on_first_participant_joined")
    async def _on_join(transport, participant):
        logger.info(f"📞 Caller joined: {participant.get('id')}")
        await tts.say("Hello. How can I help you today?")

    @transport.event_handler("on_participant_left")
    async def _on_leave(transport, participant, reason):
        logger.info(f"👋 Caller left: {reason}")
        await task.cancel()

    runner = PipelineRunner()
    logger.info(f"🤖 Bot joining {ROOM_URL}")

    try:
        await runner.run(task)
    except Exception:
        logger.exception("Bot crashed")
        raise


if __name__ == "__main__":
    asyncio.run(run_bot())
```

---

*Document version: 1.0 · Last updated: 2026-05-20 · Pattern A (PIN dial-in)*
