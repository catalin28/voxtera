# `tts_config.json` — Save Format Specification
**Voxtera · Voice Configuration Panel**
**Version:** 1.0 · June 2026
**Audience:** Frontend and backend engineers implementing the Save action

---

## What this document covers

When the operator clicks **Save** in the voice configuration panel, the
application writes a single JSON file to disk. This document defines
exactly what that file must contain — every field, every type, every
constraint — for all three providers.

---

## Rules that apply to every save

1. **Only the active voice is saved.** No inactive provider blocks.
2. **Language is never written to the file.** It is resolved at call time.
3. **All four top-level keys must always be present** — `_meta`,
   `active_voice`, `parameters`, `fallback_chain` — even if `parameters`
   has no values (Google edge case: still write an empty
   `custom_pronunciations` array).
4. **Write atomically.** Write to a `.tmp` file first, then rename to
   `tts_config.json`. Never write directly — a crash mid-write would
   corrupt the active config.
5. **`_meta.updated_at`** must be the UTC timestamp of the save action,
   not the time the user opened the panel.
6. **`fallback_chain`** must never contain the active provider.

---

## Top-level structure

```json
{
  "_meta":         { },
  "active_voice":  { },
  "parameters":    { },
  "fallback_chain": [ ]
}
```

---

## `_meta` — always identical shape

```json
"_meta": {
  "schema_version": "1.0",
  "description":    "Voxtera TTS configuration",
  "updated_at":     "2026-06-08T14:23:00Z",
  "updated_by":     "user_id_goes_here"
}
```

| Field | Type | Value |
|---|---|---|
| `schema_version` | string | Always `"1.0"` until schema changes |
| `description` | string | Fixed string — never user-editable |
| `updated_at` | string | UTC ISO 8601 timestamp of the save |
| `updated_by` | string | ID of the authenticated user who saved |

---

## `active_voice` — always identical shape

```json
"active_voice": {
  "voice_key":    "{provider}:{voice_id}",
  "display_name": "{Voice Name} ({Provider})",
  "provider":     "{provider_slug}",
  "model":        "{model_identifier}"
}
```

| Field | Type | Example | Rule |
|---|---|---|---|
| `voice_key` | string | `"cartesia:f786b574-daa5-4673-aa0c-cbe3e8534c02"` | Format: `{provider_slug}:{native_voice_id}` — split on first `:` to extract ID |
| `display_name` | string | `"Jameson (Cartesia)"` | Human label only — not used by Pipecat |
| `provider` | string | `"cartesia"` | One of `"google"` · `"cartesia"` · `"elevenlabs"` |
| `model` | string | `"sonic-3"` | See valid values below |

**Valid `model` values:**

| `provider` | `model` |
|---|---|
| `"google"` | `"chirp3-hd"` |
| `"cartesia"` | `"sonic-3"` |
| `"elevenlabs"` | `"eleven_flash_v2_5"` or `"eleven_turbo_v2_5"` |

---

## `parameters` — shape depends on `active_voice.provider`

### When provider is `"google"`

```json
"parameters": {
  "speaking_rate":   1.0,
  "volume_gain_db":  0,
  "effects_profile": "TELEPHONY_CLASS_APPLICATION",
  "pause_style":     "short",
  "custom_pronunciations": [
    {
      "phrase":            "Rixos",
      "phonetic_encoding": "PHONETIC_ENCODING_IPA",
      "pronunciation":     "ˈriksɔs"
    }
  ]
}
```

| Field | Type | Required | Valid values | Default |
|---|---|---|---|---|
| `speaking_rate` | float | ✅ | `0.25` – `2.0` | `1.0` |
| `volume_gain_db` | float | ✅ | `-96.0` – `16.0` | `0` |
| `effects_profile` | string | ✅ | See list below | `"TELEPHONY_CLASS_APPLICATION"` |
| `pause_style` | string | ✅ | `"short"` or `"long"` | `"short"` |
| `custom_pronunciations` | array | ✅ | Array of objects — may be empty `[]` | `[]` |

`effects_profile` valid values:
```
"TELEPHONY_CLASS_APPLICATION"
"HEADPHONE_CLASS_DEVICE"
"SMALL_BLUETOOTH_SPEAKER_CLASS_DEVICE"
"WEARABLE_CLASS_DEVICE"
"HANDSET_CLASS_DEVICE"
"LARGE_HOME_ENTERTAINMENT_CLASS_DEVICE"
"LARGE_AUTOMOTIVE_CLASS_DEVICE"
```

Each object in `custom_pronunciations`:

| Field | Type | Valid values |
|---|---|---|
| `phrase` | string | The exact word to override |
| `phonetic_encoding` | string | `"PHONETIC_ENCODING_IPA"` or `"PHONETIC_ENCODING_X_SAMPA"` |
| `pronunciation` | string | Phonetic string in the chosen encoding |

---

### When provider is `"cartesia"`

```json
"parameters": {
  "speed":                 "normal",
  "volume":                1.0,
  "emotion":               ["content:medium", "curious:low"],
  "pronunciation_dict_id": null
}
```

| Field | Type | Required | Valid values | Default |
|---|---|---|---|---|
| `speed` | string or float | ✅ | Enum: `"slowest"` `"slow"` `"normal"` `"fast"` `"fastest"` — or float `0.6`–`1.5` | `"normal"` |
| `volume` | float | ✅ | `0.5` – `2.0` | `1.0` |
| `emotion` | array | ✅ | Array of `"emotion_name:level"` strings — may be empty `[]` | `["content:medium", "curious:low"]` |
| `pronunciation_dict_id` | string or null | ✅ | UUID string or `null` | `null` |

`emotion` format: `"{emotion_name}:{level}"`

Valid levels: `lowest` · `low` · `medium` · `high` · `highest`

Common emotion names: `neutral` · `angry` · `excited` · `content` · `sad` ·
`scared` · `happy` · `curious` · `calm` · `confident` · `apologetic`
(full list of 60+ in TTS Parameters Reference Section 2)

---

### When provider is `"elevenlabs"`

```json
"parameters": {
  "stability":                0.65,
  "similarity_boost":         0.75,
  "style":                    0.0,
  "use_speaker_boost":        true,
  "speed":                    1.0,
  "apply_text_normalization": "auto",
  "pronunciation_dictionary_locators": []
}
```

| Field | Type | Required | Valid values | Default |
|---|---|---|---|---|
| `stability` | float | ✅ | `0.0` – `1.0` | `0.65` |
| `similarity_boost` | float | ✅ | `0.0` – `1.0` | `0.75` |
| `style` | float | ✅ | `0.0` – `1.0` | `0.0` |
| `use_speaker_boost` | boolean | ✅ | `true` or `false` | `true` |
| `speed` | float | ✅ | `0.7` – `1.2` | `1.0` |
| `apply_text_normalization` | string | ✅ | `"auto"` · `"on"` · `"off"` | `"auto"` |
| `pronunciation_dictionary_locators` | array | ✅ | Array of `{ "dictionary_id": "...", "version_id": "..." }` — may be empty `[]` | `[]` |

---

## `fallback_chain` — always an array

```json
"fallback_chain": ["cartesia", "google", "elevenlabs"]
```

| Rule | Detail |
|---|---|
| Type | Array of strings |
| Valid values | `"google"` · `"cartesia"` · `"elevenlabs"` |
| Must not contain | The value of `active_voice.provider` |
| Minimum length | 1 |
| Recommended order | Put the provider with widest language coverage first |

**Correct fallback chains per active provider:**

| Active provider | `fallback_chain` |
|---|---|
| `"google"` | `["cartesia", "elevenlabs"]` |
| `"cartesia"` | `["google", "elevenlabs"]` |
| `"elevenlabs"` | `["cartesia", "google"]` |

---

## Validation before writing

Run these checks before writing the file. Return an error to the UI
if any check fails — never write an invalid file.

| Check | Condition |
|---|---|
| Provider valid | `active_voice.provider` is one of the three valid values |
| Model valid for provider | See model table above |
| Active provider not in fallback | `active_voice.provider` not in `fallback_chain` |
| Fallback non-empty | `fallback_chain.length >= 1` |
| No language field | `parameters` must not contain `language` or `language_code` |
| `speaking_rate` in range | Google only: `0.25 ≤ value ≤ 2.0` |
| `volume_gain_db` in range | Google only: `-96.0 ≤ value ≤ 16.0` |
| `cartesia.speed` valid | Float `0.6–1.5` or valid enum string |
| `cartesia.volume` in range | `0.5 ≤ value ≤ 2.0` |
| `elevenlabs.stability` in range | `0.0 ≤ value ≤ 1.0` |
| `elevenlabs.similarity_boost` in range | `0.0 ≤ value ≤ 1.0` |
| `elevenlabs.style` in range | `0.0 ≤ value ≤ 1.0` |
| `elevenlabs.speed` in range | `0.7 ≤ value ≤ 1.2` |

---

## Complete save output — one example per provider

### Google Chirp 3 HD

```json
{
  "_meta": {
    "schema_version": "1.0",
    "description":    "Voxtera TTS configuration",
    "updated_at":     "2026-06-08T14:23:00Z",
    "updated_by":     "staff_user_id"
  },
  "active_voice": {
    "voice_key":    "google:Kore",
    "display_name": "Kore (Google)",
    "provider":     "google",
    "model":        "chirp3-hd"
  },
  "parameters": {
    "speaking_rate":   1.0,
    "volume_gain_db":  0,
    "effects_profile": "TELEPHONY_CLASS_APPLICATION",
    "pause_style":     "short",
    "custom_pronunciations": [
      {
        "phrase":            "Rixos",
        "phonetic_encoding": "PHONETIC_ENCODING_IPA",
        "pronunciation":     "ˈriksɔs"
      }
    ]
  },
  "fallback_chain": ["cartesia", "elevenlabs"]
}
```

### Cartesia Sonic-3

```json
{
  "_meta": {
    "schema_version": "1.0",
    "description":    "Voxtera TTS configuration",
    "updated_at":     "2026-06-08T14:23:00Z",
    "updated_by":     "staff_user_id"
  },
  "active_voice": {
    "voice_key":    "cartesia:f786b574-daa5-4673-aa0c-cbe3e8534c02",
    "display_name": "Jameson (Cartesia)",
    "provider":     "cartesia",
    "model":        "sonic-3"
  },
  "parameters": {
    "speed":                 "normal",
    "volume":                1.0,
    "emotion":               ["content:medium", "curious:low"],
    "pronunciation_dict_id": null
  },
  "fallback_chain": ["google", "elevenlabs"]
}
```

### ElevenLabs Flash v2.5

```json
{
  "_meta": {
    "schema_version": "1.0",
    "description":    "Voxtera TTS configuration",
    "updated_at":     "2026-06-08T14:23:00Z",
    "updated_by":     "staff_user_id"
  },
  "active_voice": {
    "voice_key":    "elevenlabs:21m00Tcm4TlvDq8ikWAM",
    "display_name": "Rachel (ElevenLabs)",
    "provider":     "elevenlabs",
    "model":        "eleven_flash_v2_5"
  },
  "parameters": {
    "stability":                0.65,
    "similarity_boost":         0.75,
    "style":                    0.0,
    "use_speaker_boost":        true,
    "speed":                    1.0,
    "apply_text_normalization": "auto",
    "pronunciation_dictionary_locators": []
  },
  "fallback_chain": ["cartesia", "google"]
}
```

---

*Voxtera · `tts_config.json` Save Format Specification v1.0 · June 2026*
