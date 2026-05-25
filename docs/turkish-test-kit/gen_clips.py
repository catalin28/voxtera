"""Regenerate the Turkish test clips for the Voxtera test kit.

Usage:
    pip install edge-tts          # one-time
    python3 gen_clips.py          # writes mp3s into ./clips/

edge-tts (Microsoft neural Turkish voices) -> mp3, then ffmpeg pads
0.3 s leading + 1.0 s trailing silence so Silero VAD reliably picks up
turn start/end when the clip is played into the bot's mic.

Edit the UTTERANCES list to change wording or add clips. Requires ffmpeg
on PATH.
"""

import asyncio
import subprocess
import tempfile
from pathlib import Path

import edge_tts

EMEL = "tr-TR-EmelNeural"  # female
AHMET = "tr-TR-AhmetNeural"  # male

# id, filename slug, voice, Turkish text
UTTERANCES = [
    (1, "hotel_checkin", EMEL, "Merhaba, otele en erken saat kaçta giriş yapabilirim?"),
    (2, "hotel_breakfast", AHMET, "Kahvaltı saat kaçta başlıyor ve nerede servis ediliyor?"),
    (3, "attractions_nearby", EMEL, "Otele yürüme mesafesinde görülecek yerler nereler?"),
    (4, "museum_hours", AHMET, "Yakındaki müze bugün saat kaça kadar açık?"),
    (
        5,
        "transport_airport",
        EMEL,
        "Havaalanına gitmek için en iyi yol nedir, taksi yaklaşık ne kadar tutar?",
    ),
    (6, "transport_transit", AHMET, "Şehir merkezine toplu taşımayla nasıl giderim?"),
    (
        7,
        "dining_recommend",
        EMEL,
        "Bu akşam için yakında iyi bir yerel restoran önerebilir misiniz?",
    ),
    (8, "dining_dietary", AHMET, "Yakında vejetaryen seçenekleri olan bir restoran var mı?"),
    (9, "safety_night", EMEL, "Şehir merkezinde gece yürümek güvenli mi?"),
    (10, "safety_emergency", AHMET, "Acil bir durumda hangi numarayı aramalıyım?"),
    (11, "events_langlock", EMEL, "Bu hafta sonu şehirde hangi festival veya event var?"),
    (12, "out_of_scope", AHMET, "Bana Paris'e bir uçuş rezervasyonu yapabilir misin?"),
]

OUT = Path(__file__).parent / "clips"
OUT.mkdir(parents=True, exist_ok=True)


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        for num, slug, voice, text in UTTERANCES:
            raw = Path(tmp) / f"{slug}.mp3"
            final = OUT / f"{num:02d}_{slug}.mp3"
            await edge_tts.Communicate(text, voice).save(str(raw))
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    str(raw),
                    "-af",
                    "adelay=300:all=1,apad=pad_dur=1.0",
                    "-codec:a",
                    "libmp3lame",
                    "-q:a",
                    "2",
                    str(final),
                ],
                check=True,
            )
            print(f"wrote {final.name}")
    print("DONE")


if __name__ == "__main__":
    asyncio.run(main())
