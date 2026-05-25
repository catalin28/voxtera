# Voxtera — Turkish Test Kit

A self-service kit for rehearsing a Turkish-language demo when you don't
speak Turkish. It contains 12 pre-recorded Turkish tourist questions you
play into the web widget's microphone, plus everything you need to judge
whether the bot answered correctly.

Surface: **web widget**, audio fed in via **BlackHole**.

---

## 1. Pre-flight config check (done — 2026-05-22)

Verified against `.env`, `config/languages.json`, and `src/voxtera/`:

- **STT — Gladia Solaria-1:** `GLADIA_LANGUAGES=en,ro,tr,fr,ru,hy`. Turkish
  (`tr`) is in the candidate list, so Solaria-1 will detect Turkish from the
  first utterance. No change needed.
- **TTS:** the active provider at startup is **OpenAI `tts-1`, voice `nova`**
  (`TTS_PROVIDER` is unset, so it falls back to the `openai` default). `tts-1`
  is multilingual and *will* speak Turkish, but `nova` is a generic English
  voice — Turkish output will sound lightly accented.
  **For the client demo, switch the TTS provider to Google Chirp 3 HD in the
  widget's provider selector.** `config/languages.json` confirms `tr` →
  Chirp 3 HD `tr-TR` is supported, and `AutoTTSLanguageSwitcher` swaps the
  voice to `tr-TR-Chirp3-HD-…` automatically when Turkish is detected — so
  Turkish replies get a native-sounding voice.
- **`GLADIA_CODE_SWITCHING=false`:** Gladia picks one language per utterance.
  Clip 11 deliberately drops the English word "event" into a Turkish
  sentence to confirm a stray loanword doesn't flip detection away from
  Turkish.

---

## 2. The 12 clips

Files are in `clips/`, generated with Microsoft neural Turkish voices
(`tr-TR-EmelNeural` female, `tr-TR-AhmetNeural` male — alternating), each
padded with 0.3 s of lead and 1.0 s of trailing silence so Silero VAD
reliably detects turn start and end.

| # | File | Category | Turkish (what is said) | English meaning | A correct answer should… |
|---|------|----------|------------------------|-----------------|--------------------------|
| 1 | `01_hotel_checkin.mp3` | Hotel | Merhaba, otele en erken saat kaçta giriş yapabilirim? | Hi, what's the earliest I can check into the hotel? | Give a check-in time, in Turkish |
| 2 | `02_hotel_breakfast.mp3` | Hotel | Kahvaltı saat kaçta başlıyor ve nerede servis ediliyor? | What time does breakfast start and where is it served? | Give a time **and** a location |
| 3 | `03_attractions_nearby.mp3` | Attractions | Otele yürüme mesafesinde görülecek yerler nereler? | What sights are within walking distance of the hotel? | Name specific nearby attractions |
| 4 | `04_museum_hours.mp3` | Attractions | Yakındaki müze bugün saat kaça kadar açık? | Until what time is the nearby museum open today? | Give a closing time (or ask which museum) |
| 5 | `05_transport_airport.mp3` | Transport | Havaalanına gitmek için en iyi yol nedir, taksi yaklaşık ne kadar tutar? | Best way to the airport, and roughly how much is a taxi? | Give transport options + a rough taxi fare |
| 6 | `06_transport_transit.mp3` | Transport | Şehir merkezine toplu taşımayla nasıl giderim? | How do I get to the city center by public transport? | Give concrete transit directions |
| 7 | `07_dining_recommend.mp3` | Dining | Bu akşam için yakında iyi bir yerel restoran önerebilir misiniz? | Can you recommend a good local restaurant nearby for tonight? | Recommend a specific place or cuisine |
| 8 | `08_dining_dietary.mp3` | Dining | Yakında vejetaryen seçenekleri olan bir restoran var mı? | Is there a restaurant nearby with vegetarian options? | Address the vegetarian request directly |
| 9 | `09_safety_night.mp3` | Safety | Şehir merkezinde gece yürümek güvenli mi? | Is it safe to walk in the city center at night? | Give a safety assessment + practical advice |
| 10 | `10_safety_emergency.mp3` | Safety | Acil bir durumda hangi numarayı aramalıyım? | What number should I call in an emergency? | Give an emergency number (**112** in Turkey) |
| 11 | `11_events_langlock.mp3` | Events / **language-lock test** | Bu hafta sonu şehirde hangi festival veya event var? | What festival or event is on in the city this weekend? | Answer **in Turkish** about weekend events — must NOT switch to English because of the word "event" |
| 12 | `12_out_of_scope.mp3` | **Out-of-scope test** | Bana Paris'e bir uçuş rezervasyonu yapabilir misin? | Can you book me a flight to Paris? | Politely say it can't book flights and redirect — stay in Turkish, do not invent a booking |

Clips 11 and 12 are stress tests: 11 checks the bot stays locked to Turkish,
12 checks it fails gracefully instead of hallucinating.

---

## 3. Playback setup (web widget + BlackHole)

The bot hears whatever the browser's selected microphone hears. The trick is
to make the browser's "microphone" be the audio of an mp3 file.

**One-time setup**

1. Confirm **BlackHole 2ch** is installed (`/Applications/Utilities` →
   *Audio MIDI Setup* lists it).
2. Install **VLC** if you don't have it — it lets you pick an output device
   per app, which QuickTime does not.

**Each test run**

1. In VLC: menu **Audio → Audio Device → BlackHole 2ch**. Now VLC plays into
   the virtual cable instead of your speakers.
2. Open the Voxtera web widget. When the browser asks for microphone access,
   pick **BlackHole 2ch** as the mic. (Your Mac already defaults Chrome's mic
   to BlackHole — just confirm it in the site's mic selector.)
3. Leave the browser's *audio output* on your normal speakers/headphones so
   you can still hear the bot's replies.
4. Play a clip in VLC. The bot "hears" it as if you spoke. Wait for the bot
   to finish its full reply, then play the next clip — don't queue them
   back-to-back.

> Optional: to also hear the clips yourself, create a *Multi-Output Device*
> (BlackHole 2ch + your speakers) in Audio MIDI Setup and point VLC at that.

---

## 4. Recommended test flow

1. **Cold-detection check (most important).** Open a fresh session, play
   **only clip 01**, and confirm the bot detects Turkish and replies in
   Turkish. If this fails, stop and fix detection before anything else.
2. **Full run.** In one continuous session, play clips **01 → 12 in order**,
   waiting for each reply. This mirrors a real conversation and verifies the
   bot *stays* in Turkish across turns (the language-lock test at clip 11
   only means something mid-conversation).
3. **Capture the transcript.** Save the conversation transcript shown in the
   widget, or grab the session log from the project's `logs/` folder.
4. **Spot-check live.** Use the last column of the table above — you don't
   need Turkish to see whether clip 2 got a time, clip 10 got "112", or
   clip 12 was declined gracefully.

---

## 5. Next step — transcript review

Hand the captured transcript back to Claude. It will read the Turkish and
flag wrong answers, unnatural phrasing, wrong register, or any moment the bot
drifted out of Turkish. For the actual client presentation, also have one
native Turkish speaker glance over it — the combination is solid.

---

## Regenerating the clips

The generator script `gen_clips.py` sits next to this file. To change wording
or add clips, edit its `UTTERANCES` list and run:

```
pip install edge-tts
python3 gen_clips.py
```

It writes the mp3s into `clips/` (ffmpeg must be on PATH).
