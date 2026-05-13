"""Multilingual startup greetings for Voxtera.

Hardcoded so the bot can speak before any LLM round-trip: faster, deterministic,
no token cost. Once the user speaks, Whisper detects their language and Claude
replies in kind — see ``src/voxtera/prompts/system_prompt.py``.

Resolution order in :func:`resolve_greeting`:

    1. Explicit preference (e.g. ``"fr"``)
    2. System locale (``locale.getdefaultlocale()``)
    3. English fallback

Add a language: drop a new ``"xx": "..."`` entry into ``GREETINGS``. The TTS
service handles the speech naturally — no per-language voice config needed at
this stage (that's VOX-E4 with Chirp 3 HD).
"""

from __future__ import annotations

import locale

GREETINGS: dict[str, str] = {
    "en": "Hello, I'm Voxtera, your travel assistant. Ask me anything about your trip.",
    "fr": (
        "Bonjour, je suis Voxtera, votre assistant de voyage. "
        "N'hésitez pas à me poser toutes vos questions sur votre séjour."
    ),
    "es": (
        "Hola, soy Voxtera, tu asistente de viajes. " "Pregúntame lo que quieras sobre tu viaje."
    ),
    "it": (
        "Ciao, sono Voxtera, il tuo assistente di viaggio. "
        "Chiedimi qualunque cosa sul tuo viaggio."
    ),
    "de": (
        "Hallo, ich bin Voxtera, Ihr Reiseassistent. "
        "Fragen Sie mich alles, was Sie über Ihre Reise wissen möchten."
    ),
    "pt": (
        "Olá, eu sou Voxtera, o seu assistente de viagens. "
        "Pergunte-me qualquer coisa sobre a sua viagem."
    ),
    "nl": (
        "Hallo, ik ben Voxtera, je reisassistent. " "Stel me alles wat je wilt weten over je reis."
    ),
    "ja": "こんにちは、旅行アシスタントのVoxteraです。旅行について何でもお聞きください。",
    "zh": "你好，我是您的旅行助手 Voxtera。关于您的旅行，请随时向我提问。",
    "ko": "안녕하세요, 저는 여행 도우미 Voxtera입니다. 여행에 관해 무엇이든 물어보세요.",
    "ar": "مرحباً، أنا فوكستيرا، مساعدك في السفر. اسألني أي شيء عن رحلتك.",
    "ru": (
        "Здравствуйте, я Voxtera, ваш помощник по путешествиям. "
        "Спрашивайте меня о чём угодно, связанном с вашей поездкой."
    ),
    "az": (
        "Salam, mən Voxtera, sizin səyahət köməkçinizəm. "
        "Səyahətiniz haqqında istədiyiniz hər şeyi soruşa bilərsiniz."
    ),
    "tr": (
        "Merhaba, ben Voxtera, seyahat asistanınızım. "
        "Yolculuğunuz hakkında her şeyi bana sorabilirsiniz."
    ),
    "ro": (
        "Bună, sunt Voxtera, asistentul tău de călătorie. " "Întreabă-mă orice despre călătoria ta."
    ),
    "hy": (
        "Բարև, ես Վոքստերան եմ՝ ձեր ճանապարհորդական օգնականը։ "
        "Հարցրեք ինձ որևէ բան ձեր ճանապարհորդության մասին։"
    ),
    "hi": ("नमस्ते, मैं वोक्सटेरा हूँ, आपका यात्रा सहायक। " "अपनी यात्रा के बारे में मुझसे कुछ भी पूछें।"),
    "pl": (
        "Cześć, jestem Voxtera, twoim asystentem podróży. "
        "Zapytaj mnie o wszystko, co dotyczy twojej podróży."
    ),
    "bg": (
        "Здравейте, аз съм Voxtera, вашият помощник за пътуване. "
        "Питайте ме всичко за вашето пътуване."
    ),
    "cs": (
        "Ahoj, jsem Voxtera, váš cestovní asistent. " "Zeptejte se mě na cokoli ohledně vaší cesty."
    ),
    "da": ("Hej, jeg er Voxtera, din rejseassistent. " "Spørg mig om hvad som helst om din rejse."),
    "el": (
        "Γεια σας, είμαι ο Voxtera, ο ταξιδιωτικός σας βοηθός. "
        "Ρωτήστε με οτιδήποτε για το ταξίδι σας."
    ),
    "fi": ("Hei, olen Voxtera, matkustusavustajasi. " "Kysy minulta mitä tahansa matkastasi."),
    "he": ("שלום, אני Voxtera, עוזר הנסיעות שלך. " "שאל אותי כל דבר על הטיול שלך."),
    "hu": (
        "Szia, én vagyok Voxtera, az utazási asszisztensed. " "Kérdezz tőlem bármit az utazásodról."
    ),
    "id": (
        "Halo, saya Voxtera, asisten perjalanan Anda. " "Tanyakan apa saja tentang perjalanan Anda."
    ),
    "no": ("Hei, jeg er Voxtera, din reiseassistent. " "Spør meg om hva som helst om reisen din."),
    "sv": ("Hej, jag är Voxtera, din reseassistent. " "Fråga mig vad som helst om din resa."),
    "th": ("สวัสดี ฉันคือ Voxtera ผู้ช่วยการเดินทางของคุณ " "ถามฉันได้ทุกอย่างเกี่ยวกับการเดินทางของคุณ"),
    "uk": (
        "Привіт, я Voxtera, ваш помічник у подорожах. " "Запитайте мене будь-що про вашу подорож."
    ),
    "vi": (
        "Xin chào, tôi là Voxtera, trợ lý du lịch của bạn. "
        "Hãy hỏi tôi bất cứ điều gì về chuyến đi của bạn."
    ),
}

DEFAULT_LANGUAGE = "en"


def _detect_system_language() -> str | None:
    """Return a 2-letter language code from the OS locale, or None on failure."""
    try:
        loc, _ = locale.getlocale()
    except (ValueError, locale.Error):
        loc = None

    if not loc:
        # getlocale() can be None or empty when LANG isn't set; fall back.
        try:
            loc = locale.getdefaultlocale()[0]
        except (ValueError, IndexError):
            loc = None

    if not loc:
        return None

    # Locale strings look like "en_US", "fr_FR", "ja_JP". We want the prefix.
    return loc.split("_", 1)[0].lower()


def resolve_greeting(preference: str = "auto") -> tuple[str, str]:
    """Pick a greeting and return ``(language_code, text)``.

    Args:
        preference: ``"auto"`` to detect from the system locale, or an explicit
            language code like ``"fr"``. Unknown codes fall back to English.

    Returns:
        A ``(code, text)`` tuple — ``code`` is the language we chose (useful for
        logging), ``text`` is the greeting to speak.
    """
    pref = (preference or "auto").lower().strip()

    if pref == "auto":
        detected = _detect_system_language()
        if detected and detected in GREETINGS:
            return detected, GREETINGS[detected]
        return DEFAULT_LANGUAGE, GREETINGS[DEFAULT_LANGUAGE]

    if pref in GREETINGS:
        return pref, GREETINGS[pref]

    # Unknown explicit code — fall back to English but make it observable.
    return DEFAULT_LANGUAGE, GREETINGS[DEFAULT_LANGUAGE]
