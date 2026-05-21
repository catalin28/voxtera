"""Multilingual startup greetings for Voxtera — the hotel voice concierge.

Hardcoded so the bot can speak before any LLM round-trip: faster, deterministic,
no token cost. Once the guest speaks, Whisper detects their language and Claude
replies in kind — see ``src/voxtera/prompts/system_prompt.py``.

Two catalogs:

* ``GREETINGS`` — one time-neutral concierge greeting per language. This is the
  safe default: used at bot boot (before the browser connects) and whenever the
  guest's local time is unknown (phone line, Telegram, an older widget).
* ``TIMED_GREETINGS`` — morning / afternoon / evening variants per language.
  Used when the browser reports the guest's timezone via the ``voxtera-timezone``
  app-message; :class:`~voxtera.controllers.GreetingController` computes the
  daypart and picks the matching variant.

Why the browser's timezone and not the server clock: the bot runs on a server
whose clock is UTC and tells us nothing about the guest's local time. The widget
knows it (``Intl.DateTimeFormat().resolvedOptions().timeZone``) and sends it.

Resolution order in :func:`resolve_greeting`:

    1. Explicit preference (e.g. ``"fr"``)
    2. System locale (``locale.getlocale()``)
    3. English fallback

Add a language: add a ``"xx": "..."`` entry to ``GREETINGS`` and a matching
``"xx": {...}`` entry to ``TIMED_GREETINGS``. A language present in ``GREETINGS``
but missing from ``TIMED_GREETINGS`` simply never gets a time-of-day greeting —
it falls back to the neutral one, which is always safe.

Where a language does not lexically distinguish a daypart (French has no
separate "good afternoon"; Korean and Hindi barely daypart greetings at all),
the variants intentionally repeat — that is correct usage, not an oversight.
"""

from __future__ import annotations

import locale
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_LANGUAGE = "en"

# Daypart bucket keys used throughout (TIMED_GREETINGS, GreetingController).
DAYPARTS = ("morning", "afternoon", "evening")

# Time-neutral concierge greeting per language. Always safe to fall back to.
GREETINGS: dict[str, str] = {
    "en": (
        "Hello, and a very warm welcome. "
        "It's a pleasure to have you with us — I'm your concierge. "
        "How may I help you?"
    ),
    "fr": (
        "Bonjour et bienvenue. C'est un véritable plaisir de vous accueillir — "
        "je suis votre concierge. Comment puis-je vous aider ?"
    ),
    "es": (
        "Hola y le damos la bienvenida. Es un placer tenerle con nosotros. "
        "Soy su conserje. ¿En qué puedo ayudarle?"
    ),
    "it": (
        "Salve e Le diamo il benvenuto. È un piacere averla con noi. "
        "Sono il Suo concierge. Come posso aiutarla?"
    ),
    "de": (
        "Hallo und herzlich willkommen. Es ist uns eine Freude, Sie bei uns "
        "zu begrüßen. Ich bin Ihr Concierge. Wie kann ich Ihnen helfen?"
    ),
    "pt": (
        "Olá e damos-lhe as boas-vindas. É um grande prazer que esteja "
        "connosco. Sou o seu concierge. Como posso ajudar?"
    ),
    "nl": (
        "Hallo en hartelijk welkom. Wat fijn dat u er bent — "
        "ik ben uw conciërge. Hoe kan ik u helpen?"
    ),
    "ja": (
        "ようこそお越しくださいました。お会いできて光栄です。"
        "わたくし、コンシェルジュでございます。ご用件をお伺いいたします。"
    ),
    "zh": "您好，热烈欢迎您。很高兴为您服务，我是您的专属礼宾。请问有什么可以帮您？",
    "ko": (
        "안녕하세요, 진심으로 환영합니다. 모시게 되어 기쁩니다. "
        "저는 고객님의 컨시어지입니다. 무엇을 도와드릴까요?"
    ),
    "ar": ("أهلاً وسهلاً بك. يسعدنا وجودك معنا. " "أنا الكونسيرج الخاص بك. كيف يمكنني مساعدتك؟"),
    "ru": (
        "Здравствуйте и добро пожаловать. Мы рады видеть вас. "
        "Я ваш консьерж. Чем я могу вам помочь?"
    ),
    "az": (
        "Salam və xoş gəlmisiniz. Sizi aramızda görməyə şadıq. "
        "Mən sizin konsyerjinizəm. Sizə necə kömək edə bilərəm?"
    ),
    "tr": (
        "Merhaba ve hoş geldiniz. Sizi aramızda görmek bir mutluluk. "
        "Ben sizin konsiyerjinizim. Size nasıl yardımcı olabilirim?"
    ),
    "ro": (
        "Bună ziua și bine ați venit. Ne face plăcere să vă avem alături. "
        "Sunt concierge-ul dumneavoastră. Cu ce vă pot ajuta?"
    ),
    "hy": (
        "Բարև Ձեզ և բարի գալուստ։ Ուրախ ենք Ձեզ մեզ մոտ տեսնել։ "
        "Ես Ձեր կոնսիերժն եմ։ Ինչո՞վ կարող եմ օգնել Ձեզ։"
    ),
    "hi": (
        "नमस्ते और हार्दिक स्वागत है। आपका हमारे यहाँ आना हमारे लिए खुशी की बात है। "
        "मैं आपका कॉन्सियर्ज हूँ। मैं आपकी कैसे सहायता करूँ?"
    ),
    "pl": (
        "Dzień dobry i serdecznie witamy. Cieszymy się, że są Państwo z nami. "
        "Jestem Państwa konsjerżem. W czym mogę pomóc?"
    ),
    "bg": (
        "Здравейте и добре дошли. За нас е удоволствие да сте при нас. "
        "Аз съм вашият консиерж. С какво мога да ви помогна?"
    ),
    "cs": (
        "Dobrý den a vítejte. Je nám potěšením, že jste u nás. "
        "Jsem váš concierge. Jak vám mohu pomoci?"
    ),
    "da": (
        "Hej og hjertelig velkommen. Det glæder os at have dig hos os. "
        "Jeg er din concierge. Hvordan kan jeg hjælpe dig?"
    ),
    "el": (
        "Γεια σας και καλώς ορίσατε. Χαιρόμαστε που είστε μαζί μας. "
        "Είμαι ο κονσιέρζ σας. Πώς μπορώ να σας βοηθήσω;"
    ),
    "fi": (
        "Hei ja tervetuloa. On ilo saada teidät vieraaksemme. "
        "Olen conciergenne. Kuinka voin auttaa teitä?"
    ),
    "he": ("שלום וברוכים הבאים. שמחים לארח אתכם. " "אני הקונסיירז' שלכם. כיצד אוכל לעזור לכם?"),
    "hu": (
        "Üdvözöljük! Örömünkre szolgál, hogy nálunk van. "
        "Én vagyok az Ön concierge-e. Miben segíthetek?"
    ),
    "id": (
        "Halo dan selamat datang. Kami senang Anda berada di sini. "
        "Saya concierge pribadi Anda. Ada yang bisa saya bantu?"
    ),
    "no": (
        "Hei og hjertelig velkommen. Det gleder oss å ha deg her. "
        "Jeg er din concierge. Hvordan kan jeg hjelpe deg?"
    ),
    "sv": (
        "Hej och hjärtligt välkommen. Det glädjer oss att ha dig hos oss. "
        "Jag är din concierge. Hur kan jag hjälpa dig?"
    ),
    "th": ("สวัสดี ยินดีต้อนรับ เรายินดีมากที่คุณมาพัก " "คอนเซียร์จส่วนตัวของคุณพร้อมให้บริการ มีอะไรให้ช่วยไหม"),
    "uk": (
        "Вітаю і ласкаво просимо. Ми раді вітати вас у нас. "
        "Я ваш консьєрж. Чим я можу вам допомогти?"
    ),
    "vi": (
        "Xin chào và chào mừng quý khách. "
        "Chúng tôi rất hân hạnh được đón tiếp quý khách. "
        "Tôi là nhân viên lễ tân riêng của quý khách. Tôi có thể giúp gì cho quý khách?"
    ),
}

# Morning / afternoon / evening variants. Same body as the neutral greeting,
# only the opening time-of-day phrase changes.
TIMED_GREETINGS: dict[str, dict[str, str]] = {
    "en": {
        "morning": (
            "Good morning, and a very warm welcome. "
            "It's a pleasure to have you with us — I'm your concierge. "
            "How may I help you?"
        ),
        "afternoon": (
            "Good afternoon, and a very warm welcome. "
            "It's a pleasure to have you with us — I'm your concierge. "
            "How may I help you?"
        ),
        "evening": (
            "Good evening, and a very warm welcome. "
            "It's a pleasure to have you with us — I'm your concierge. "
            "How may I help you?"
        ),
    },
    "fr": {
        # French has no distinct "good afternoon" — "Bonjour" covers the day.
        "morning": (
            "Bonjour et bienvenue. C'est un véritable plaisir de vous accueillir — "
            "je suis votre concierge. Comment puis-je vous aider ?"
        ),
        "afternoon": (
            "Bonjour et bienvenue. C'est un véritable plaisir de vous accueillir — "
            "je suis votre concierge. Comment puis-je vous aider ?"
        ),
        "evening": (
            "Bonsoir et bienvenue. C'est un véritable plaisir de vous accueillir — "
            "je suis votre concierge. Comment puis-je vous aider ?"
        ),
    },
    "es": {
        "morning": (
            "Buenos días y le damos la bienvenida. Es un placer tenerle con "
            "nosotros. Soy su conserje. ¿En qué puedo ayudarle?"
        ),
        "afternoon": (
            "Buenas tardes y le damos la bienvenida. Es un placer tenerle con "
            "nosotros. Soy su conserje. ¿En qué puedo ayudarle?"
        ),
        "evening": (
            "Buenas noches y le damos la bienvenida. Es un placer tenerle con "
            "nosotros. Soy su conserje. ¿En qué puedo ayudarle?"
        ),
    },
    "it": {
        "morning": (
            "Buongiorno e Le diamo il benvenuto. È un piacere averla con noi. "
            "Sono il Suo concierge. Come posso aiutarla?"
        ),
        "afternoon": (
            "Buon pomeriggio e Le diamo il benvenuto. È un piacere averla con noi. "
            "Sono il Suo concierge. Come posso aiutarla?"
        ),
        "evening": (
            "Buonasera e Le diamo il benvenuto. È un piacere averla con noi. "
            "Sono il Suo concierge. Come posso aiutarla?"
        ),
    },
    "de": {
        "morning": (
            "Guten Morgen und herzlich willkommen. Es ist uns eine Freude, Sie "
            "bei uns zu begrüßen. Ich bin Ihr Concierge. Wie kann ich Ihnen helfen?"
        ),
        "afternoon": (
            "Guten Tag und herzlich willkommen. Es ist uns eine Freude, Sie "
            "bei uns zu begrüßen. Ich bin Ihr Concierge. Wie kann ich Ihnen helfen?"
        ),
        "evening": (
            "Guten Abend und herzlich willkommen. Es ist uns eine Freude, Sie "
            "bei uns zu begrüßen. Ich bin Ihr Concierge. Wie kann ich Ihnen helfen?"
        ),
    },
    "pt": {
        "morning": (
            "Bom dia e damos-lhe as boas-vindas. É um grande prazer que esteja "
            "connosco. Sou o seu concierge. Como posso ajudar?"
        ),
        "afternoon": (
            "Boa tarde e damos-lhe as boas-vindas. É um grande prazer que esteja "
            "connosco. Sou o seu concierge. Como posso ajudar?"
        ),
        "evening": (
            "Boa noite e damos-lhe as boas-vindas. É um grande prazer que esteja "
            "connosco. Sou o seu concierge. Como posso ajudar?"
        ),
    },
    "nl": {
        "morning": (
            "Goedemorgen en hartelijk welkom. Wat fijn dat u er bent — "
            "ik ben uw conciërge. Hoe kan ik u helpen?"
        ),
        "afternoon": (
            "Goedemiddag en hartelijk welkom. Wat fijn dat u er bent — "
            "ik ben uw conciërge. Hoe kan ik u helpen?"
        ),
        "evening": (
            "Goedenavond en hartelijk welkom. Wat fijn dat u er bent — "
            "ik ben uw conciërge. Hoe kan ik u helpen?"
        ),
    },
    "ja": {
        "morning": (
            "おはようございます。ようこそお越しくださいました。お会いできて光栄です。"
            "わたくし、コンシェルジュでございます。ご用件をお伺いいたします。"
        ),
        "afternoon": (
            "こんにちは。ようこそお越しくださいました。お会いできて光栄です。"
            "わたくし、コンシェルジュでございます。ご用件をお伺いいたします。"
        ),
        "evening": (
            "こんばんは。ようこそお越しくださいました。お会いできて光栄です。"
            "わたくし、コンシェルジュでございます。ご用件をお伺いいたします。"
        ),
    },
    "zh": {
        "morning": "早上好，热烈欢迎您。很高兴为您服务，我是您的专属礼宾。请问有什么可以帮您？",
        "afternoon": "下午好，热烈欢迎您。很高兴为您服务，我是您的专属礼宾。请问有什么可以帮您？",
        "evening": "晚上好，热烈欢迎您。很高兴为您服务，我是您的专属礼宾。请问有什么可以帮您？",
    },
    "ko": {
        # Korean rarely dayparts greetings; "안녕하세요" is the natural default.
        "morning": (
            "좋은 아침입니다. 진심으로 환영합니다. 모시게 되어 기쁩니다. "
            "저는 고객님의 컨시어지입니다. 무엇을 도와드릴까요?"
        ),
        "afternoon": (
            "안녕하세요, 진심으로 환영합니다. 모시게 되어 기쁩니다. "
            "저는 고객님의 컨시어지입니다. 무엇을 도와드릴까요?"
        ),
        "evening": (
            "안녕하세요, 진심으로 환영합니다. 모시게 되어 기쁩니다. "
            "저는 고객님의 컨시어지입니다. 무엇을 도와드릴까요?"
        ),
    },
    "ar": {
        # Arabic "مساء الخير" covers both afternoon and evening.
        "morning": (
            "صباح الخير وأهلاً بك. يسعدنا وجودك معنا. " "أنا الكونسيرج الخاص بك. كيف يمكنني مساعدتك؟"
        ),
        "afternoon": (
            "مساء الخير وأهلاً بك. يسعدنا وجودك معنا. " "أنا الكونسيرج الخاص بك. كيف يمكنني مساعدتك؟"
        ),
        "evening": (
            "مساء الخير وأهلاً بك. يسعدنا وجودك معنا. " "أنا الكونسيرج الخاص بك. كيف يمكنني مساعدتك؟"
        ),
    },
    "ru": {
        "morning": (
            "Доброе утро и добро пожаловать. Мы рады видеть вас. "
            "Я ваш консьерж. Чем я могу вам помочь?"
        ),
        "afternoon": (
            "Добрый день и добро пожаловать. Мы рады видеть вас. "
            "Я ваш консьерж. Чем я могу вам помочь?"
        ),
        "evening": (
            "Добрый вечер и добро пожаловать. Мы рады видеть вас. "
            "Я ваш консьерж. Чем я могу вам помочь?"
        ),
    },
    "az": {
        "morning": (
            "Sabahınız xeyir və xoş gəlmisiniz. Sizi aramızda görməyə şadıq. "
            "Mən sizin konsyerjinizəm. Sizə necə kömək edə bilərəm?"
        ),
        "afternoon": (
            "Günortanız xeyir və xoş gəlmisiniz. Sizi aramızda görməyə şadıq. "
            "Mən sizin konsyerjinizəm. Sizə necə kömək edə bilərəm?"
        ),
        "evening": (
            "Axşamınız xeyir və xoş gəlmisiniz. Sizi aramızda görməyə şadıq. "
            "Mən sizin konsyerjinizəm. Sizə necə kömək edə bilərəm?"
        ),
    },
    "tr": {
        "morning": (
            "Günaydın ve hoş geldiniz. Sizi aramızda görmek bir mutluluk. "
            "Ben sizin konsiyerjinizim. Size nasıl yardımcı olabilirim?"
        ),
        "afternoon": (
            "İyi günler ve hoş geldiniz. Sizi aramızda görmek bir mutluluk. "
            "Ben sizin konsiyerjinizim. Size nasıl yardımcı olabilirim?"
        ),
        "evening": (
            "İyi akşamlar ve hoş geldiniz. Sizi aramızda görmek bir mutluluk. "
            "Ben sizin konsiyerjinizim. Size nasıl yardımcı olabilirim?"
        ),
    },
    "ro": {
        "morning": (
            "Bună dimineața și bine ați venit. Ne face plăcere să vă avem "
            "alături. Sunt concierge-ul dumneavoastră. Cu ce vă pot ajuta?"
        ),
        "afternoon": (
            "Bună ziua și bine ați venit. Ne face plăcere să vă avem "
            "alături. Sunt concierge-ul dumneavoastră. Cu ce vă pot ajuta?"
        ),
        "evening": (
            "Bună seara și bine ați venit. Ne face plăcere să vă avem "
            "alături. Sunt concierge-ul dumneavoastră. Cu ce vă pot ajuta?"
        ),
    },
    "hy": {
        "morning": (
            "Բարի լույս և բարի գալուստ։ Ուրախ ենք Ձեզ մեզ մոտ տեսնել։ "
            "Ես Ձեր կոնսիերժն եմ։ Ինչո՞վ կարող եմ օգնել Ձեզ։"
        ),
        "afternoon": (
            "Բարի օր և բարի գալուստ։ Ուրախ ենք Ձեզ մեզ մոտ տեսնել։ "
            "Ես Ձեր կոնսիերժն եմ։ Ինչո՞վ կարող եմ օգնել Ձեզ։"
        ),
        "evening": (
            "Բարի երեկո և բարի գալուստ։ Ուրախ ենք Ձեզ մեզ մոտ տեսնել։ "
            "Ես Ձեր կոնսիերժն եմ։ Ինչո՞վ կարող եմ օգնել Ձեզ։"
        ),
    },
    "hi": {
        # Hindi barely dayparts greetings; "नमस्ते" is the natural default.
        "morning": (
            "सुप्रभात और हार्दिक स्वागत है। आपका हमारे यहाँ आना हमारे लिए खुशी की बात है। "
            "मैं आपका कॉन्सियर्ज हूँ। मैं आपकी कैसे सहायता करूँ?"
        ),
        "afternoon": (
            "नमस्ते और हार्दिक स्वागत है। आपका हमारे यहाँ आना हमारे लिए खुशी की बात है। "
            "मैं आपका कॉन्सियर्ज हूँ। मैं आपकी कैसे सहायता करूँ?"
        ),
        "evening": (
            "शुभ संध्या और हार्दिक स्वागत है। आपका हमारे यहाँ आना हमारे लिए खुशी की बात है। "
            "मैं आपका कॉन्सियर्ज हूँ। मैं आपकी कैसे सहायता करूँ?"
        ),
    },
    "pl": {
        # Polish "Dzień dobry" covers morning and afternoon.
        "morning": (
            "Dzień dobry i serdecznie witamy. Cieszymy się, że są Państwo z "
            "nami. Jestem Państwa konsjerżem. W czym mogę pomóc?"
        ),
        "afternoon": (
            "Dzień dobry i serdecznie witamy. Cieszymy się, że są Państwo z "
            "nami. Jestem Państwa konsjerżem. W czym mogę pomóc?"
        ),
        "evening": (
            "Dobry wieczór i serdecznie witamy. Cieszymy się, że są Państwo z "
            "nami. Jestem Państwa konsjerżem. W czym mogę pomóc?"
        ),
    },
    "bg": {
        "morning": (
            "Добро утро и добре дошли. За нас е удоволствие да сте при нас. "
            "Аз съм вашият консиерж. С какво мога да ви помогна?"
        ),
        "afternoon": (
            "Добър ден и добре дошли. За нас е удоволствие да сте при нас. "
            "Аз съм вашият консиерж. С какво мога да ви помогна?"
        ),
        "evening": (
            "Добър вечер и добре дошли. За нас е удоволствие да сте при нас. "
            "Аз съм вашият консиерж. С какво мога да ви помогна?"
        ),
    },
    "cs": {
        "morning": (
            "Dobré ráno a vítejte. Je nám potěšením, že jste u nás. "
            "Jsem váš concierge. Jak vám mohu pomoci?"
        ),
        "afternoon": (
            "Dobrý den a vítejte. Je nám potěšením, že jste u nás. "
            "Jsem váš concierge. Jak vám mohu pomoci?"
        ),
        "evening": (
            "Dobrý večer a vítejte. Je nám potěšením, že jste u nás. "
            "Jsem váš concierge. Jak vám mohu pomoci?"
        ),
    },
    "da": {
        "morning": (
            "Godmorgen og hjertelig velkommen. Det glæder os at have dig hos "
            "os. Jeg er din concierge. Hvordan kan jeg hjælpe dig?"
        ),
        "afternoon": (
            "God eftermiddag og hjertelig velkommen. Det glæder os at have dig "
            "hos os. Jeg er din concierge. Hvordan kan jeg hjælpe dig?"
        ),
        "evening": (
            "Godaften og hjertelig velkommen. Det glæder os at have dig hos "
            "os. Jeg er din concierge. Hvordan kan jeg hjælpe dig?"
        ),
    },
    "el": {
        "morning": (
            "Καλημέρα και καλώς ορίσατε. Χαιρόμαστε που είστε μαζί μας. "
            "Είμαι ο κονσιέρζ σας. Πώς μπορώ να σας βοηθήσω;"
        ),
        "afternoon": (
            "Καλό απόγευμα και καλώς ορίσατε. Χαιρόμαστε που είστε μαζί μας. "
            "Είμαι ο κονσιέρζ σας. Πώς μπορώ να σας βοηθήσω;"
        ),
        "evening": (
            "Καλησπέρα και καλώς ορίσατε. Χαιρόμαστε που είστε μαζί μας. "
            "Είμαι ο κονσιέρζ σας. Πώς μπορώ να σας βοηθήσω;"
        ),
    },
    "fi": {
        "morning": (
            "Hyvää huomenta ja tervetuloa. On ilo saada teidät vieraaksemme. "
            "Olen conciergenne. Kuinka voin auttaa teitä?"
        ),
        "afternoon": (
            "Hyvää päivää ja tervetuloa. On ilo saada teidät vieraaksemme. "
            "Olen conciergenne. Kuinka voin auttaa teitä?"
        ),
        "evening": (
            "Hyvää iltaa ja tervetuloa. On ilo saada teidät vieraaksemme. "
            "Olen conciergenne. Kuinka voin auttaa teitä?"
        ),
    },
    "he": {
        "morning": (
            "בוקר טוב וברוכים הבאים. שמחים לארח אתכם. " "אני הקונסיירז' שלכם. כיצד אוכל לעזור לכם?"
        ),
        "afternoon": (
            "צהריים טובים וברוכים הבאים. שמחים לארח אתכם. "
            "אני הקונסיירז' שלכם. כיצד אוכל לעזור לכם?"
        ),
        "evening": (
            "ערב טוב וברוכים הבאים. שמחים לארח אתכם. " "אני הקונסיירז' שלכם. כיצד אוכל לעזור לכם?"
        ),
    },
    "hu": {
        "morning": (
            "Jó reggelt és sok szeretettel üdvözöljük. Örömünkre szolgál, hogy "
            "nálunk van. Én vagyok az Ön concierge-e. Miben segíthetek?"
        ),
        "afternoon": (
            "Jó napot és sok szeretettel üdvözöljük. Örömünkre szolgál, hogy "
            "nálunk van. Én vagyok az Ön concierge-e. Miben segíthetek?"
        ),
        "evening": (
            "Jó estét és sok szeretettel üdvözöljük. Örömünkre szolgál, hogy "
            "nálunk van. Én vagyok az Ön concierge-e. Miben segíthetek?"
        ),
    },
    "id": {
        "morning": (
            "Selamat pagi dan selamat datang. Kami senang Anda berada di sini. "
            "Saya concierge pribadi Anda. Ada yang bisa saya bantu?"
        ),
        "afternoon": (
            "Selamat siang dan selamat datang. Kami senang Anda berada di sini. "
            "Saya concierge pribadi Anda. Ada yang bisa saya bantu?"
        ),
        "evening": (
            "Selamat malam dan selamat datang. Kami senang Anda berada di sini. "
            "Saya concierge pribadi Anda. Ada yang bisa saya bantu?"
        ),
    },
    "no": {
        "morning": (
            "God morgen og hjertelig velkommen. Det gleder oss å ha deg her. "
            "Jeg er din concierge. Hvordan kan jeg hjelpe deg?"
        ),
        "afternoon": (
            "God dag og hjertelig velkommen. Det gleder oss å ha deg her. "
            "Jeg er din concierge. Hvordan kan jeg hjelpe deg?"
        ),
        "evening": (
            "God kveld og hjertelig velkommen. Det gleder oss å ha deg her. "
            "Jeg er din concierge. Hvordan kan jeg hjelpe deg?"
        ),
    },
    "sv": {
        "morning": (
            "God morgon och hjärtligt välkommen. Det glädjer oss att ha dig hos "
            "oss. Jag är din concierge. Hur kan jag hjälpa dig?"
        ),
        "afternoon": (
            "God dag och hjärtligt välkommen. Det glädjer oss att ha dig hos "
            "oss. Jag är din concierge. Hur kan jag hjälpa dig?"
        ),
        "evening": (
            "God kväll och hjärtligt välkommen. Det glädjer oss att ha dig hos "
            "oss. Jag är din concierge. Hur kan jag hjälpa dig?"
        ),
    },
    "th": {
        "morning": (
            "อรุณสวัสดิ์ ยินดีต้อนรับ เรายินดีมากที่คุณมาพัก " "คอนเซียร์จส่วนตัวของคุณพร้อมให้บริการ มีอะไรให้ช่วยไหม"
        ),
        "afternoon": (
            "สวัสดีตอนบ่าย ยินดีต้อนรับ เรายินดีมากที่คุณมาพัก " "คอนเซียร์จส่วนตัวของคุณพร้อมให้บริการ มีอะไรให้ช่วยไหม"
        ),
        "evening": (
            "สวัสดีตอนเย็น ยินดีต้อนรับ เรายินดีมากที่คุณมาพัก " "คอนเซียร์จส่วนตัวของคุณพร้อมให้บริการ มีอะไรให้ช่วยไหม"
        ),
    },
    "uk": {
        "morning": (
            "Доброго ранку і ласкаво просимо. Ми раді вітати вас у нас. "
            "Я ваш консьєрж. Чим я можу вам допомогти?"
        ),
        "afternoon": (
            "Доброго дня і ласкаво просимо. Ми раді вітати вас у нас. "
            "Я ваш консьєрж. Чим я можу вам допомогти?"
        ),
        "evening": (
            "Доброго вечора і ласкаво просимо. Ми раді вітати вас у нас. "
            "Я ваш консьєрж. Чим я можу вам допомогти?"
        ),
    },
    "vi": {
        "morning": (
            "Chào buổi sáng và chào mừng quý khách. Chúng tôi rất hân hạnh được "
            "đón tiếp quý khách. Tôi là nhân viên lễ tân riêng của quý khách. "
            "Tôi có thể giúp gì cho quý khách?"
        ),
        "afternoon": (
            "Chào buổi chiều và chào mừng quý khách. Chúng tôi rất hân hạnh được "
            "đón tiếp quý khách. Tôi là nhân viên lễ tân riêng của quý khách. "
            "Tôi có thể giúp gì cho quý khách?"
        ),
        "evening": (
            "Chào buổi tối và chào mừng quý khách. Chúng tôi rất hân hạnh được "
            "đón tiếp quý khách. Tôi là nhân viên lễ tân riêng của quý khách. "
            "Tôi có thể giúp gì cho quý khách?"
        ),
    },
}


def daypart_for_hour(hour: int) -> str:
    """Map a 24-hour clock hour (0-23) to a daypart key.

    Boundaries: 05:00-11:59 morning, 12:00-17:59 afternoon, 18:00-04:59 evening.
    """
    if 5 <= hour <= 11:
        return "morning"
    if 12 <= hour <= 17:
        return "afternoon"
    return "evening"


def daypart_for_timezone(tz: str | None) -> str | None:
    """Return the current daypart in IANA timezone ``tz`` (e.g. ``"Europe/Paris"``).

    Returns ``None`` when ``tz`` is missing, empty, or not a recognised IANA
    name — callers should then fall back to the time-neutral greeting.
    """
    if not tz or not isinstance(tz, str):
        return None
    try:
        now = datetime.now(ZoneInfo(tz))
    except (ZoneInfoNotFoundError, ValueError, OSError):
        # Unknown/malformed timezone — degrade to time-neutral.
        return None
    return daypart_for_hour(now.hour)


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


def resolve_greeting(preference: str = "auto", *, daypart: str | None = None) -> tuple[str, str]:
    """Pick a greeting and return ``(language_code, text)``.

    Args:
        preference: ``"auto"`` to detect from the system locale, or an explicit
            language code like ``"fr"``. Unknown codes fall back to English.
        daypart: optional ``"morning"`` / ``"afternoon"`` / ``"evening"``. When
            given and a timed greeting exists for the chosen language, the
            time-of-day variant is returned; otherwise the time-neutral one is.
            Boot-time callers leave this ``None`` (the browser hasn't reported
            the guest's timezone yet) — see ``GreetingController``.

    Returns:
        A ``(code, text)`` tuple — ``code`` is the language chosen (useful for
        logging), ``text`` is the greeting to speak.
    """
    pref = (preference or "auto").lower().strip()

    if pref == "auto":
        detected = _detect_system_language()
        code = detected if (detected and detected in GREETINGS) else DEFAULT_LANGUAGE
    elif pref in GREETINGS:
        code = pref
    else:
        # Unknown explicit code — fall back to English but stay observable.
        code = DEFAULT_LANGUAGE

    if daypart:
        timed = TIMED_GREETINGS.get(code)
        if timed and daypart in timed:
            return code, timed[daypart]

    return code, GREETINGS[code]
