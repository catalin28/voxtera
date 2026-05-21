"""Multilingual instant-acknowledgment fillers for Voxtera.

A "filler" is a short, natural backchannel — "One moment.", "Let me check
that." — spoken the instant the guest stops talking, while the LLM is still
generating the real answer. It masks the unavoidable STT->LLM->TTS latency
gap with human-style speech instead of dead silence. See
:class:`voxtera.controllers.InstantAckFiller` for the processor that plays
them.

Why hardcoded (like ``GREETINGS``): the filler must start within ~100 ms of
the guest finishing — there is no time for an LLM round-trip, and it would be
wasteful to spend tokens on a phrase the caller hears dozens of times.

Language selection: the processor reads the language the STT detected for the
current utterance (``TranscriptionFrame.language``) and looks it up here. The
filler is therefore always in the same language the answer will come back in.
A language with no entry below plays *no* filler — a wrong-language
acknowledgment is worse than silence.

Design rules for entries:

* Keep each phrase SHORT — roughly 0.6-1.3 s of speech. Long fillers risk
  still playing when the real answer is ready, which would delay it.
* Give every language a VARIED pool (>= 4 phrases). The processor never
  repeats the same phrase twice in a row, so variety stops the filler from
  sounding like a robotic tic.
* Phrases must be natural spoken register for a hotel concierge — neutral,
  warm, and safe to say before literally any guest question.

Add a language: drop a new ``"xx": [...]`` list into ``FILLERS``. Only add a
language the TTS provider can actually pronounce well.
"""

from __future__ import annotations

# Per-language filler pools. Keys are 2-letter ISO 639-1 codes (matching the
# primary subtag the STT language detector emits).
FILLERS: dict[str, list[str]] = {
    "en": [
        "One moment.",
        "Let me check that.",
        "Sure, let me see.",
        "Okay, just a second.",
        "Let me look into that.",
        "Right, let me find that for you.",
    ],
    "fr": [
        "Un instant.",
        "Je vérifie ça.",
        "Bien sûr, voyons voir.",
        "D'accord, une seconde.",
        "Laissez-moi regarder ça.",
        "Très bien, je trouve ça pour vous.",
    ],
    "es": [
        "Un momento.",
        "Déjeme ver eso.",
        "Claro, un segundo.",
        "Vale, lo compruebo.",
        "Permítame revisarlo.",
        "Muy bien, lo busco enseguida.",
    ],
    "it": [
        "Un momento.",
        "Lascia che controlli.",
        "Certo, vediamo.",
        "Va bene, un secondo.",
        "Faccio subito un controllo.",
        "Perfetto, lo cerco per lei.",
    ],
    "de": [
        "Einen Moment.",
        "Lassen Sie mich kurz nachsehen.",
        "Klar, einen Augenblick.",
        "Gut, eine Sekunde.",
        "Ich schaue das eben nach.",
        "Alles klar, ich finde das für Sie.",
    ],
    "pt": [
        "Um momento.",
        "Deixe-me verificar.",
        "Claro, um segundo.",
        "Está bem, vou ver.",
        "Vou já confirmar isso.",
        "Certo, já lhe procuro isso.",
    ],
    "nl": [
        "Een moment.",
        "Laat me even kijken.",
        "Zeker, één seconde.",
        "Goed, ik zoek het op.",
        "Ik check het even.",
        "Prima, ik zoek dat voor u op.",
    ],
    "ru": [
        "Одну минуту.",
        "Сейчас проверю.",
        "Конечно, секунду.",
        "Хорошо, давайте посмотрим.",
        "Дайте мне взглянуть.",
        "Сейчас всё уточню для вас.",
    ],
    "ro": [
        "Un moment.",
        "Să verific.",
        "Sigur, o secundă.",
        "Bine, să mă uit.",
        "Verific imediat.",
        "Imediat caut asta pentru dumneavoastră.",
    ],
    "tr": [
        "Bir saniye.",
        "Hemen kontrol edeyim.",
        "Tabii, bir bakayım.",
        "Tamam, bir saniye.",
        "Şuna bir bakayım.",
        "Hemen sizin için buluyorum.",
    ],
    "pl": [
        "Chwileczkę.",
        "Już sprawdzam.",
        "Jasne, sekunda.",
        "Dobrze, zaraz zobaczę.",
        "Pozwól, że sprawdzę.",
        "Już to dla pana znajdę.",
    ],
    "hy": [
        "Մեկ վայրկյան։",
        "Հիմա ստուգեմ։",
        "Իհարկե, մի պահ։",
        "Լավ, թույլ տվեք նայեմ։",
        "Հիմա կճշտեմ։",
        "Հիմա կգտնեմ ձեզ համար։",
    ],
}

# Languages with a curated filler pool. Convenience export for callers that
# want to check support without touching the dict directly.
SUPPORTED_FILLER_LANGUAGES: frozenset[str] = frozenset(FILLERS)
