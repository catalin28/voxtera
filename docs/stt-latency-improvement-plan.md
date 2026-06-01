# STT Latency Improvement Plan

- **Date:** 2026-05-30
- **Status:** Recommendation document
- **Scope:** PSTN / on-demand Voxtera calls with STT enabled
- **Current provider focus:** Gladia Solaria-1 + Silero VAD + PSTN speech-timeout turn stop
- **Based on traces:** `voxtera-trace-2026-05-30T00-32-37-651Z.json`, `voxtera-trace-2026-05-30T00-49-50-725Z.json`

---

## TL;DR

STT is still the largest fixed cost on normal PSTN turns.

The traces show that most of the remaining delay happens **after the caller stops speaking** and before the final transcript reaches the rest of the pipeline.

The highest-value STT improvements for the current codebase are:

1. **Make PSTN `user_speech_timeout` configurable and tune it down carefully** instead of leaving it hardcoded at `0.4s`.
2. **Split the VAD stop threshold by transport** so PSTN can use a slightly more aggressive silence window than browser mic sessions.
3. **Add finer STT-stage tracing** so VAD stop, provider finalization, and local post-provider overhead are measured separately.
4. **Test Gladia with a constrained language set** when a deployment does not actually need open 99-language auto-detect.
5. **Verify the Gladia region is close to the bot host** so WebSocket RTT is not adding avoidable delay.
6. **Prefer overlap over brute-force compression** when direct STT gains flatten out: speculative downstream work is safer than over-aggressive endpointing.

---

## Important constraint

There is still STT headroom, but this is not the same kind of optimization surface as RAG.

For RAG, a cache or earlier prefetch can remove visible latency almost entirely for some turns.

For STT, the direct gains are usually smaller and riskier because they come from deciding earlier that the user has finished speaking. That means the failure mode is worse:

- cut off a natural pause
- split one sentence into two turns
- trigger the bot before the guest is actually done

So the right STT plan is:

1. tune conservatively
2. isolate PSTN-specific knobs from browser-mic knobs
3. measure after every step

---

## What the traces show

### Current normal PSTN turn

From the newer trace, the fast path looked roughly like this:

- `stt`: `721-741ms`
- `stt_to_llm`: `487-509ms`
- `llm_ttft`: `563ms`
- `tts_ttft`: `296ms`

### What matters most on the STT side

Gladia already reports a provider-specific tail metric:

- `user_stopped_to_final_ms = 607ms`
- `user_stopped_to_final_ms = 679ms`

The Voxtera `stt` stage is slightly larger:

- `721ms`
- `741ms`

That implies:

- the majority of STT latency is provider finalization after the user stops speaking
- the remaining `~60-115ms` is local frame transit and downstream handling after Gladia has already finalized

### What this means

The biggest remaining STT lever is not model cold start and not generic Python overhead.

The real levers are:

- how early Voxtera decides the caller is done
- how quickly Gladia finalizes once audio has stopped
- how much of the remaining post-STT work can be overlapped with interim transcripts

---

## Current STT path in code

The current PSTN path is:

1. Silero VAD decides speech has stopped
2. PSTN turn stop uses `SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.4)`
3. Gladia receives the turn audio and emits the final transcript
4. `TranscriptStageTimer` stamps the `transcript` anchor and emits the `stt` stage
5. the rest of the pipeline begins

### Main files involved

- `src/voxtera/pipeline.py`
- `src/voxtera/stt.py`
- `src/voxtera/config.py`
- `src/voxtera/observability.py`
- `src/voxtera/trace.py`
- `docs/voice-pipeline-config.md`

---

## Recommendation 1 — Make PSTN `user_speech_timeout` configurable

### Why this helps

For PSTN, the turn-stop strategy is currently hardcoded here:

```python
SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.4)
```

That means Voxtera always waits `400ms` after detected silence before declaring the turn done.

For phone calls, that may be slightly conservative.

Reducing it carefully can shave visible latency without touching the provider itself.

### Where to modify

#### `src/voxtera/config.py`

Add a new setting such as:

- `pstn_user_speech_timeout: float = 0.4`

and load it from an environment variable like:

- `PSTN_USER_SPEECH_TIMEOUT`

#### `src/voxtera/pipeline.py`

Replace the hardcoded PSTN value with the setting.

Current shape:

```python
SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.4)
```

Target shape:

```python
SpeechTimeoutUserTurnStopStrategy(
    user_speech_timeout=settings.pstn_user_speech_timeout,
)
```

### Conservative tuning order

Test in this order:

1. `0.40`
2. `0.35`
3. `0.30`

Do not jump directly to `0.20`.

### Expected impact

Low to medium. This is the cleanest direct PSTN STT latency improvement in the current code.

### Risk

If pushed too far, short intra-sentence pauses will be interpreted as turn endings.

---

## Recommendation 2 — Split VAD stop tuning by transport

### Why this helps

The current `vad_stop_secs` is global and defaults to `0.3s`.

That value was tuned as a reasonable balance for general microphone conditions. PSTN is a narrower, more controlled audio path and may tolerate a slightly more aggressive stop window.

The problem is that a single global knob forces one tradeoff across very different channels:

- browser laptop mics
- headset mics
- PSTN telephony audio

### Where to modify

#### `src/voxtera/config.py`

Add a PSTN-specific override such as:

- `pstn_vad_stop_secs: float = 0.3`

with an environment variable like:

- `PSTN_VAD_STOP_SECS`

#### `src/voxtera/pipeline.py`

Select the VAD stop window based on transport type when building `VADParams`.

Current shape:

```python
VADParams(
    stop_secs=settings.vad_stop_secs,
    ...
)
```

Target shape:

```python
stop_secs = settings.pstn_vad_stop_secs if _is_pstn else settings.vad_stop_secs
```

### Conservative tuning order

Test in this order for PSTN only:

1. `0.30`
2. `0.28`
3. `0.25`

### Expected impact

Low to medium. This can reduce the time before the provider is allowed to finalize, but it must be tested on real calls.

### Risk

Too aggressive a VAD stop threshold causes chatter and split turns.

---

## Recommendation 3 — Add finer STT tracing inside the STT slice

### Why this helps

The trace already gives two useful signals:

- provider tail: `user_stopped_to_final_ms`
- overall STT stage: `stt`

That is enough to know STT is still large, but not enough to know exactly how much is:

- VAD / local turn-stop delay before the provider really sees the end of the utterance
- provider finalization time
- local post-provider handling after the final transcript arrives

Without that split, further STT tuning becomes guesswork.

### Where to modify

#### `src/voxtera/stt.py`

Extend the existing Gladia lifecycle instrumentation to emit more explicit stage-style numbers, not only lifecycle payloads.

Suggested additions:

- `stt_provider_tail`
- `stt_first_audio_to_final`

#### `src/voxtera/observability.py`

Add a stage for the local post-provider part if you want an explicit split between:

- provider final transcript time
- final `TranscriptionFrame` observed by `TranscriptStageTimer`

#### `src/voxtera/trace.py`

No schema rewrite is required. Existing stage / lifecycle emit support is already sufficient.

### Expected impact

Low direct latency impact. High tuning value.

### Important note

This should be done before any aggressive STT experiments if the current `stt` stage stays dominant after the latest pipeline changes.

---

## Recommendation 4 — Test constrained Gladia language sets for known deployments

### Why this helps

When `gladia_languages` is empty, the current builder sends Gladia into open auto-detect across all supported languages.

That is the broadest and safest multilingual behavior, but it is not always the best operational mode for a real hotel deployment.

If a hotel actually expects a narrower language set, constraining Gladia may improve transcript stability and may also help finalization consistency.

### Important caveat

This is more clearly an **accuracy and stability** optimization than a guaranteed latency win.

It belongs in the STT plan because poor transcript stability often turns into effective latency problems:

- retries
- empty turns
- follow-up clarification turns

### Where to modify

#### `src/voxtera/config.py`

Use the existing settings:

- `gladia_languages`
- `gladia_code_switching`

#### `src/voxtera/stt.py`

The Gladia builder already supports:

- empty list = open auto-detect across all languages
- explicit list + `code_switching=False` = detect one language per utterance from that list
- explicit list + `code_switching=True` = constrained code-switching mode

### Recommended behavior

For production hotels with a known traffic mix, test:

1. top 5-15 expected languages
2. `code_switching=False` first
3. only enable `code_switching=True` if real calls actually need it

### Expected impact

Low to medium. More likely to improve transcript quality than produce a dramatic raw latency reduction.

---

## Recommendation 5 — Verify Gladia region against bot geography

### Why this helps

`gladia_region` is currently configurable and defaults to `eu-west`.

That is not automatically optimal for every deployment. If the bot host is far from the selected Gladia region, WebSocket round-trip time adds avoidable delay to the STT path.

### Where to modify

#### `src/voxtera/config.py`

The knob already exists:

- `gladia_region`

#### `.env`

Test the appropriate region for the bot host, for example:

- `GLADIA_REGION=eu-west`
- `GLADIA_REGION=us-west`

### Expected impact

Low, but easy to test and low-risk.

### Important note

This is especially worth checking when the bot host is in North America and the STT region is pinned to Europe.

---

## Recommendation 6 — Prefer overlap once direct STT gains flatten out

### Why this matters

Even after tuning, the provider still needs time to finalize.

There is a point where pushing STT harder yields only small savings while increasing the risk of cutoffs.

At that point, the better latency move is to overlap downstream work with interim transcripts instead of trying to eliminate the STT tail itself.

### Where this connects to other work

This is not a pure STT-file change. It crosses into the RAG and pre-LLM path.

Candidate surfaces:

- `src/voxtera/pipeline.py`
- future speculative prefetch logic for RAG or action classification

### Expected impact

High perceived-latency impact, even if the raw STT provider time stays similar.

---

## What is unlikely to help first

These are not the first STT moves to make:

- enabling Gladia server-side VAD while Silero VAD is already active
- custom vocabulary as a latency tactic
- Gladia import / startup optimization as a per-turn latency tactic
- switching STT providers before exhausting the current PSTN turn-stop and VAD tuning path

Why:

- duplicate endpointing layers usually add complexity before they add speed
- custom vocabulary mainly affects recognition quality
- import and connection work matter for startup, not for the steady-state turn path

---

## Suggested implementation order

1. **Make `user_speech_timeout` configurable for PSTN**
2. **Make `vad_stop_secs` split by transport**
3. **Run controlled A/B traces on PSTN**
4. **Add finer STT-stage tracing if the results are ambiguous**
5. **Test constrained Gladia language sets where the deployment supports it**
6. **Benchmark Gladia region choices**
7. **Only then consider deeper provider or overlap experiments**

This order gives the best ratio of engineering effort to likely latency gain.

---

## Concrete file map

### Immediate change surfaces

- `src/voxtera/config.py`
  - add PSTN-specific STT tuning knobs
  - expose them via environment variables

- `src/voxtera/pipeline.py`
  - replace hardcoded PSTN speech timeout
  - select PSTN-specific VAD stop timing when appropriate

- `src/voxtera/stt.py`
  - extend provider-tail instrumentation
  - test alternative Gladia language / region settings

### Existing support already in place

- `src/voxtera/observability.py`
  - already emits the overall `stt` stage

- `src/voxtera/trace.py`
  - already supports per-turn shared anchors and stage/lifecycle events

- `docs/voice-pipeline-config.md`
  - already documents the existing voice-pipeline latency decisions

---

## Conservative PSTN test matrix

Use the same prompts and the same call path for every trial.

Suggested sequence:

1. baseline
   - `PSTN_USER_SPEECH_TIMEOUT=0.40`
   - `PSTN_VAD_STOP_SECS=0.30`

2. lighter speech-timeout reduction
   - `PSTN_USER_SPEECH_TIMEOUT=0.35`
   - `PSTN_VAD_STOP_SECS=0.30`

3. lighter VAD reduction
   - `PSTN_USER_SPEECH_TIMEOUT=0.35`
   - `PSTN_VAD_STOP_SECS=0.28`

4. more aggressive trial
   - `PSTN_USER_SPEECH_TIMEOUT=0.30`
   - `PSTN_VAD_STOP_SECS=0.25`

Stop at the first configuration that causes noticeable:

- mid-sentence cutoffs
- split turns
- premature bot replies

---

## Success criteria

After implementing the first two recommendations, the next traces should prove:

1. the normal-turn `stt` stage is lower than the current `721-741ms`
2. `user_stopped_to_final_ms` improves or, at minimum, end-to-end latency improves without transcript quality regression
3. there is no spike in empty turns, cut-off turns, or split-utterance turns
4. PSTN calls still feel natural on callers with brief pauses inside sentences

If those criteria are not met, stop pushing STT harder and shift effort toward overlap and downstream speculative work instead.
