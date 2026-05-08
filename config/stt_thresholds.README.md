# STT Confidence Thresholds

`stt_thresholds.json` controls per-language confidence filtering for the
Whisper STT path. Low-confidence transcriptions are dropped before they
reach the LLM, which suppresses Whisper substitution hallucinations like
"the water is not running" → "the White House" without biasing toward any
single language.

## Schema

```json
{
  "default":   { "avg_logprob_min": -1.0, "no_speech_prob_max": 0.7 },
  "<lang>":    { "avg_logprob_min": -0.7, "no_speech_prob_max": 0.6 },
  ...
}
```

- **Keys** — `default`, plus any ISO 639-1 short code (`en`, `fr`, `ro`)
  or full English name (`english`, `french`, `romanian`). Both forms map
  to the same canonical entry.
- **`avg_logprob_min`** — drop the transcription if any segment scores
  *worse* (more negative) than this. Whisper's per-token log-probability
  average. Cleaner audio = closer to 0. Hallucinations typically score
  below −1.0.
- **`no_speech_prob_max`** — drop the transcription if any segment scores
  *worse* (higher) than this. Whisper's non-speech estimate per segment.

## Lookup chain

The loader resolves a transcription's language in this order:

1. Whisper-returned language → canonical ISO 639-1 short code.
2. Look up the canonical code in this JSON.
3. If not found, fall back to the `default` entry.
4. If `default` is missing or the JSON is malformed, fall back to the
   hardcoded values in `voxtera.stt_thresholds._HARDCODED_*`
   (`avg_logprob_min = -1.0`, `no_speech_prob_max = 0.7`).

## Tuning guidance

Lean lenient. For a demo, a false positive (dropping legitimate speech →
the bot ignores the user) is much more damaging than a false negative
(one hallucinated reply, recoverable by repeating).

Per-language baselines for clean speech:

| Language     | Clean baseline | Suggested `avg_logprob_min` |
| ------------ | -------------- | --------------------------- |
| English      | −0.2 to −0.4   | −0.7                        |
| French       | −0.2 to −0.4   | −0.7                        |
| Russian      | −0.3 to −0.5   | −0.7                        |
| Romanian     | −0.4 to −0.6   | −0.8                        |
| Turkish      | −0.5 to −0.8   | −1.0                        |
| Arabic (MSA) | −0.4 to −0.6   | −1.0                        |
| Azerbaijani  | −0.6 to −0.9   | −1.1                        |

These are starting points. Test in your demo room with your demo speakers
and adjust based on what shows up in the logs.

## Diagnosing

Watch for these log lines:

```
[stt] dropped low-confidence transcription (lang='russian', avg_logprob=-1.20 threshold=-0.70, ...)
```

If you're seeing legitimate speech being dropped, raise the threshold
(e.g. `-0.7` → `-0.9`) for that language and reload. If too many weird
transcriptions are getting through, lower the threshold.

## Reload at runtime

`STTThresholds.reload()` re-reads the file. The `_MultilingualWhisperSTT`
service exposes `reload_thresholds()` for this. Wire to a Daily app
message or admin endpoint if you want to retune live during a demo.

## Path

By default the bot loads from `config/stt_thresholds.json` (relative to
the working directory when the bot starts). Override with the
`STT_THRESHOLDS_PATH` environment variable. Set `STT_THRESHOLDS_PATH=`
(empty) to disable the file load entirely; the hardcoded fallback is then
used uniformly.
