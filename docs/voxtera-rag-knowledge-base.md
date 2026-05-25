# Voxtera — RAG Knowledge Base
# Optimized for vector store ingestion and retrieval-augmented generation

> This document is the single source of truth for the Voxtera product chat assistant.
> Each section is a self-contained chunk designed for semantic retrieval.
> Questions are answered from multiple angles: hotel buyers, travelers, technical
> evaluators, and industry partners.

---

## CHUNK: What is Voxtera?

Voxtera is a real-time multilingual voice agent built for the tourism and hospitality industry. It lets a traveler speak naturally into a phone, web browser, or app — in any language — and receive an instant spoken reply in that same language. There is no language menu, no "press 1 for English," and no manual language selection. The guest simply starts talking.

Voxtera acts as an always-on AI concierge. It handles the questions guests actually ask on the ground: hotel services, check-in, dining, spa bookings, room issues, local recommendations, transportation, and cultural tips. It speaks with a warm, polished voice — not a robotic IVR.

---

## CHUNK: The core promise — zero language friction

The defining feature of Voxtera is language-first design. The moment a guest starts speaking, Voxtera automatically detects the language from the very first utterance and holds the entire conversation in that language, consistently, from the first word to the last.

This is fundamentally different from existing hotel phone trees or chatbots that ask the user to choose a language upfront. For international travelers — especially those who are jet-lagged, anxious, or in an unfamiliar environment — not having to navigate a menu in a foreign language is a significant relief.

---

## CHUNK: What languages does Voxtera support?

Voxtera supports 99+ languages through its speech-to-text layer (OpenAI Whisper or Gladia Solaria-1). For the AI voice output (text-to-speech), Voxtera uses Google Chirp 3 HD which covers 75+ languages, automatically switching the voice locale to match the detected language of the guest.

Languages with confirmed production support include: English, French, Spanish, Italian, German, Portuguese, Dutch, Japanese, Chinese (Mandarin), Korean, Arabic, Russian, Turkish, Romanian, Armenian, and many more. Unlike competing solutions that top out at 10–14 languages, Voxtera is built to cover the full long tail of international tourism — the languages of the guests, not just the languages of the hotel.

---

## CHUNK: How does Voxtera understand speech so accurately?

Voxtera uses state-of-the-art automatic speech recognition (ASR/STT). In production, Voxtera runs either OpenAI Whisper (whisper-1) or Gladia Solaria-1. Both provide:

- Automatic language detection without user input
- High accuracy on accented speech, common in international tourism contexts
- Low latency: approximately 700 milliseconds from the Toronto deployment
- Support for the complete range of languages a global tourism deployment requires

Voxtera also uses Silero VAD (Voice Activity Detection) to handle natural conversation flow: the guest can start speaking, pause, or even interrupt the bot mid-sentence, and the system responds correctly in all cases.

---

## CHUNK: How natural does the conversation feel?

Voxtera is designed to feel like a conversation with a real concierge, not a robot. Several engineering choices contribute to this:

- **Interruption handling**: The guest can talk over the bot at any point. The bot stops speaking and listens immediately.
- **Turn detection**: After the guest finishes speaking, the bot begins processing within 0.8 seconds — a natural human-scale pause.
- **Voice quality**: The production voice is Google Chirp 3 HD, which delivers high-definition, natural-sounding speech in each language.
- **Persona**: The system prompt is tuned to produce warm, concise, concierge-appropriate replies — not technical, robotic, or overly formal language.
- **Total latency**: End-to-end from when the guest finishes speaking to when the bot starts speaking is approximately 1.5–3 seconds, depending on query complexity.

---

## CHUNK: What can the Voxtera voice agent actually do?

Voxtera can answer questions, provide information, and take actions on behalf of the guest. Here is a breakdown:

### Information and answers (from hotel knowledge base)
- Hotel hours: breakfast, lunch, dinner, bar, spa, pool, fitness, reception
- Room features: Wi-Fi password, thermostat, TV, safe, lighting, phone instructions
- Dining options: menu items, special dishes, dietary accommodations (vegan, gluten-free, halal, kosher)
- Spa and wellness: available treatments, prices, booking procedures, thermal area rules
- Hotel policies: check-in/check-out times, cancellation policy, pet policy, smoking policy
- Local recommendations: nearby restaurants, museums, attractions, transport, day trips
- Frequently asked questions: babysitting, luggage storage, parking, currency exchange

### Actions the bot can take (via tool calls to hotel systems)
- Create maintenance and housekeeping tickets (e.g., "the AC is not working in room 412")
- Report lost and found items
- Request room service or additional amenities
- Log guest issues for follow-up by staff
- In future: book spa appointments, restaurant reservations, local tours (integration with PMS/booking APIs)

---

## CHUNK: How does the hotel's specific information get into the bot?

Voxtera uses a Retrieval-Augmented Generation (RAG) layer. This means the bot has access to the actual documents of each specific hotel — the real menu, the actual spa prices, the current policies, the real hours — not just general knowledge from the AI model.

The hotel provides its content (menus, service guides, troubleshooting docs, FAQs) as structured text documents. These documents are:

1. Split into small chunks (100–300 word segments)
2. Converted into vector embeddings (numerical representations of meaning)
3. Stored in a vector database with the hotel's unique identifier
4. Retrieved in real time when a guest asks a relevant question

This means that if a guest asks "what's on the dinner menu tonight?", the bot retrieves the actual hotel menu and answers with the real dishes — not a generic answer. The hotel can update its content (menu changes, policy updates) without redeploying the bot.

---

## CHUNK: How quickly does the hotel's content get updated in the bot?

Content updates (new menu items, changed policies, updated spa prices) are applied by uploading the revised documents to the hotel's content folder. The content is re-indexed, and the bot starts using the updated information on the next conversation. There is no need to retrain a model or redeploy the software. Most updates take effect within minutes.

---

## CHUNK: What deployment channels does Voxtera support?

Voxtera can be deployed on three channels from a single backend:

1. **Web widget**: A JavaScript embed added to the hotel website. Guests click a button and speak directly in their browser. No app download required.
2. **Phone line (Twilio)**: Guests call a local phone number. The call is handled by Voxtera. This is the most accessible channel — no smartphone, no app, just a phone number printed on the room card or welcome booklet.
3. **Mobile SDK**: An embedded component for hotel mobile apps (future roadmap).

A hotel can deploy one, two, or all three channels from the same Voxtera account. The bot knowledge, language configuration, and actions are shared across all channels.

---

## CHUNK: What types of hotels or properties is Voxtera designed for?

Voxtera is designed for hotels, resorts, and destination tourism operators that serve international guests — particularly properties where multiple languages are spoken by the guest population.

**Best-fit properties:**
- 4-star and 5-star hotels with a significant share of international guests
- Hotels in major tourist destinations (Paris, Rome, Istanbul, Tokyo, Dubai, etc.)
- Resorts in holiday destinations where guests may not speak the local language
- Boutique properties without the budget to staff a 24-hour multilingual front desk
- Hotel groups managing multiple properties who want a consistent, scalable guest voice experience

**Less ideal fit:**
- Small domestic inns serving only local guests in a single language
- Properties with no internet connectivity for the bot's backend

---

## CHUNK: How does Voxtera help hotel staff?

Voxtera is a complement to, not a replacement for, hotel staff. It handles the high-volume, repetitive, often-after-hours questions that currently drain staff time:

- "What time does breakfast start?" (asked dozens of times a day)
- "What's the Wi-Fi password?" (same)
- "Can I get extra pillows?"
- "The TV isn't working"
- "What's nearby to eat tonight?"

When the guest reports a real issue (e.g., broken AC, plumbing problem), Voxtera creates a structured ticket in the hotel's workflow and notifies the right team member — immediately, in the staff's language, regardless of what language the guest spoke.

This lets staff focus on the interactions that actually require a human touch: difficult complaints, VIP needs, situations requiring judgment or empathy.

---

## CHUNK: Is Voxtera available 24 hours a day?

Yes. Voxtera runs as a cloud-hosted service, available 24 hours a day, 7 days a week. There is no shift schedule, no sick leave, and no language barrier. A guest arriving at 3 AM who speaks only Japanese can ask Voxtera for early check-in help, a spare toothbrush, or directions to the nearest pharmacy, and receive an accurate answer in Japanese — immediately.

---

## CHUNK: What happens when the bot doesn't know the answer?

Voxtera is designed to fail gracefully. If a question falls outside the hotel's knowledge base and outside the AI's general knowledge:

1. The bot gives an honest response: "I don't have specific information on that, but I'd recommend calling the front desk directly."
2. The bot offers to connect the guest to a human staff member if the channel supports it.
3. The failure is logged, allowing the hotel to identify gaps in their knowledge base and fill them.

Voxtera does not make up answers or hallucinate facts. The RAG layer grounds every response in the actual hotel content provided.

---

## CHUNK: How does Voxtera handle noisy environments?

Voxtera includes RNNoise integration, a neural noise suppressor that cleans up microphone input before it reaches the speech recognition engine. This is designed for the real conditions a voice agent encounters: lobby background noise, pool-area echoes, HVAC hum in conference rooms, or the ambient sound of a busy bar.

RNNoise removes broadband background noise while preserving the natural quality and intelligibility of the guest's voice. This means speech recognition accuracy is maintained even when the guest is not in a quiet room.

---

## CHUNK: What is the latency / speed of Voxtera responses?

Voxtera's voice pipeline is engineered for real-time conversation. Measured from production deployments:

- **STT (speech-to-text) latency**: ~700 ms (Whisper, short conversational queries)
- **LLM (AI reasoning) latency**: ~350–500 ms (Claude Haiku 4.5, with prompt caching)
- **TTS (text-to-speech) first audio byte**: ~230 ms (Google Chirp 3 HD via gRPC streaming)
- **Total end-to-end**: approximately 1.5–3 seconds (95th percentile for typical concierge queries)

This is within the range that feels natural in human conversation. For comparison, calling a hotel's front desk often involves longer hold times.

---

## CHUNK: What AI models power Voxtera?

Voxtera is built on best-in-class AI models at each stage of the pipeline:

| Stage | Model | Why |
|---|---|---|
| Speech recognition | OpenAI Whisper or Gladia Solaria-1 | 99+ language auto-detection, high accuracy, low latency |
| Language understanding & generation | Anthropic Claude Haiku 4.5 | Fast (350–500 ms), multilingual, excellent for concierge Q&A |
| Voice output | Google Chirp 3 HD | 75+ languages, 230 ms first audio, high-definition natural voice |
| Noise suppression | RNNoise (neural) | Real-time microphone denoising |
| Voice activity detection | Silero VAD | Accurate speech/silence boundary detection |
| Orchestration | Pipecat | Real-time AI voice pipeline framework |

---

## CHUNK: Is Voxtera secure? What happens to guest data?

Security and privacy are built into Voxtera's design:

- All API keys and credentials are stored in server environment variables, never in code
- Conversation audio is processed in real time and not stored beyond the current session unless explicitly configured
- Multi-tenant architecture ensures complete data isolation: Hotel A's knowledge base and guest conversations are never accessible to Hotel B
- Communication uses encrypted channels (HTTPS, gRPC with TLS)
- Guest personal data (room number, booking details) is only accessed via authenticated tool calls to the hotel's own PMS — Voxtera does not store or cache this data

---

## CHUNK: How does Voxtera handle multiple hotels (multi-tenant)?

Voxtera is architected as a multi-tenant platform. Each hotel has:

- Its own isolated knowledge base (content documents, FAQs, policies, menus)
- Its own hotel configuration (name, language of staff, supported guest languages, action categories)
- Its own ticket/action routing (Telegram channel, webhook endpoint, or email)
- Complete data isolation — no cross-hotel data leakage

This means a single Voxtera deployment can power dozens or hundreds of hotel properties simultaneously, each with its own identity, content, and workflow.

---

## CHUNK: What does integration with existing hotel systems look like?

Voxtera integrates with hotel operations through two mechanisms:

1. **Structured ticket creation**: When a guest reports an issue or makes a request, Voxtera creates a structured ticket (category, summary, original guest quote in their language, room number, priority) and delivers it to the hotel's chosen endpoint — Telegram, Slack, email, or webhook to the hotel's PMS/ticketing system.

2. **CRM and PMS API integration (roadmap)**: Future tool calls will connect directly to Property Management Systems for live data: current room availability, bookings, loyalty program status, maintenance logs.

No wholesale system replacement is required. Voxtera plugs into existing hotel operations and augments them.

---

## CHUNK: How does a hotel get started with Voxtera?

The onboarding process is designed to be fast:

1. **Content collection**: The hotel provides its existing documents — menu PDFs, welcome guide, spa brochure, FAQ sheet, policy document. These don't need to be reformatted; Voxtera ingests standard formats.
2. **Configuration**: A hotel configuration file is set up (hotel name, languages, ticket routing, staff notification channel).
3. **Knowledge base build**: Content is chunked, embedded, and indexed into the hotel's vector store.
4. **Deployment**: The web widget embed code is added to the hotel's website, or the phone number is configured via Twilio.
5. **Live in days**: Most hotels can be live within 2–5 business days of content delivery.

---

## CHUNK: Can guests book services through Voxtera?

Currently, Voxtera can log and route service requests (maintenance, housekeeping, concierge requests). Direct live booking (spa appointments, restaurant reservations, room upgrades) requires integration with the hotel's booking/PMS API, which is on the near-term roadmap.

When booking integration is live, a guest will be able to say "I'd like to book a couples massage for tomorrow at 4 PM" and Voxtera will check availability, confirm with the guest, and create the booking — all in the guest's own language.

---

## CHUNK: How does Voxtera compare to a traditional IVR phone system?

| Feature | Traditional IVR | Voxtera |
|---|---|---|
| Languages | Usually 2–4 (pre-recorded) | 99+ (automatic detection) |
| Input method | Press keypad digits | Natural spoken conversation |
| Language selection | Guest must choose upfront | Automatic, no selection needed |
| Knowledge depth | Static pre-recorded messages | Dynamic, hotel-specific knowledge base |
| Can take action | Limited (transfer to agent only) | Creates tickets, requests, bookings |
| Available 24/7 | Yes | Yes |
| Update content | Re-record audio, costly | Edit a document, live in minutes |
| Naturalness | Robotic | Natural human-like voice |

---

## CHUNK: How does Voxtera compare to a chatbot widget?

| Feature | Standard chatbot | Voxtera |
|---|---|---|
| Input method | Type text | Speak naturally (or type — hybrid mode supported) |
| Languages | Often English only or limited | 99+ auto-detected |
| Response format | Text bubbles | Spoken voice reply |
| Accessibility | Requires literacy in that language | Voice-first, accessible to all |
| Actions | Usually informational only | Creates tickets, routes requests |
| Hotel-specific knowledge | Usually generic FAQ | Full RAG layer on hotel content |

Voxtera also supports a text/hybrid input mode, so guests who prefer typing (quiet environments, hearing impairment, personal preference) can use the same bot via text while still hearing spoken replies.

---

## CHUNK: What is the business model for hotels?

Voxtera operates as a Software-as-a-Service (SaaS) subscription for hotels. Pricing is based on the number of properties and conversation volume. Hotels in the founding cohort (early design partners) receive favorable pricing in exchange for product feedback and case study participation.

Voxtera is seeking its first cohort of pilot hotels to validate the product in real guest environments. Hotels interested in becoming design partners receive dedicated onboarding support, direct access to the founding team, and input into the product roadmap.

---

## CHUNK: Is Voxtera a startup? What is the current stage?

Yes. Voxtera is a pre-revenue startup in active development. As of May 2026:

- A working end-to-end voice pipeline is live
- A demo environment (Grand Hôtel Lumière) is fully functional
- The RAG layer for hotel-specific knowledge is built and tested
- The action-taking layer (ticket creation) is built and tested
- The team is seeking the founding cohort of pilot hotel partners

The founding team is technical. Voxtera was built by engineers who understand the hotel industry's multilingual guest challenge from the ground up.

---

## CHUNK: What is the demo hotel — Grand Hôtel Lumière?

Grand Hôtel Lumière is the reference demo property used to showcase Voxtera's capabilities. It is a fictional 5-star hotel in Paris's 8th arrondissement (near the Champs-Élysées) with:

- Two restaurants (Le Mirador rooftop, La Petite Terrasse garden bistro)
- A lobby bar (Bar Lumière) with live jazz on Thursdays
- Spa Lumière with treatments, heated pool, sauna, hammam, and fitness studio
- A full-service concierge desk (Clefs d'Or)

The demo bot is fully loaded with the hotel's real menus, spa price list, policies, troubleshooting guide, and welcome information. Prospective hotel partners can speak with the demo bot to experience exactly how their own guests would interact with a deployed Voxtera instance.

---

## CHUNK: What kinds of questions can the demo bot answer?

The Voxtera demo bot (Grand Hôtel Lumière) can answer:

- "What time does breakfast start?" → 6:30 (weekends until 11:00)
- "Do you have vegan options?" → Yes, all restaurants accommodate vegan requests
- "How much is a couples massage?" → €290 for 60 minutes in the couples suite
- "What's the Wi-Fi password?" → Printed on the keycard sleeve
- "Can I check out late?" → Until 14:00 complimentary if available, until 18:00 at 50% surcharge
- "My TV isn't working" → Troubleshooting guide + option to log a maintenance ticket
- "Where should I have dinner nearby?" → Specific recommendations with descriptions
- "Is there babysitting available?" → Yes, €35/hour, 4-hour minimum, 24 hours notice needed
- "Can I bring my dog?" → Yes, under 10 kg, €40/night

All answers come from the actual hotel documents loaded into the RAG system.

---

## CHUNK: What languages did the demo bot successfully run in?

The Voxtera voice pipeline has been tested and confirmed working in:

- English
- French
- Spanish
- Italian
- German
- Japanese
- Turkish
- Romanian
- Portuguese
- Arabic

In each case, the bot detects the language automatically from the first utterance and maintains the conversation entirely in that language without any prompt or selection from the guest.

---

## CHUNK: How does Voxtera handle guest complaints and maintenance issues?

When a guest reports a problem — broken air conditioning, plumbing issue, noise complaint, missing items — Voxtera follows a structured resolution flow:

1. The bot first asks clarifying questions to understand the issue clearly
2. It provides any self-help troubleshooting steps from the hotel's knowledge base
3. If the problem requires staff involvement, it asks the guest for confirmation
4. On confirmation, it creates a structured ticket including: the category (maintenance, housekeeping, etc.), a brief English summary for staff, the guest's original quote in their language, the room number, and a priority level
5. The ticket is immediately routed to the designated staff channel (Telegram, Slack, email, or PMS webhook)
6. The bot confirms to the guest (in their language) that the issue has been logged and staff will respond

This happens in the guest's language — the staff receives the ticket in their language. No translation required on either end.

---

## CHUNK: What happens with the voice data / audio recordings?

Voxtera processes voice audio in real time and does not store conversation audio beyond the active session. The flow is:

1. Guest speaks → audio is sent to the STT service (OpenAI Whisper or Gladia) → transcribed to text
2. The text transcript is used to query the knowledge base and generate a reply
3. The reply is converted to audio by the TTS service
4. Audio is played back to the guest

Conversation transcripts can be optionally logged for quality assurance and knowledge base improvement, subject to the hotel's privacy policy and applicable data protection regulation (GDPR, etc.). Audio recordings are not retained by default.

---

## CHUNK: Can Voxtera handle technical jargon or specialized vocabulary?

Yes. Voxtera supports custom vocabulary configuration via a `stt_vocabulary.json` configuration file. Hotels can register:

- Property-specific terms (hotel names, restaurant names, suite names)
- Local place names and landmarks that the STT might otherwise mishear
- Staff names, department names
- Specialized menu items or service names

This ensures that "Lumière Club," "Le Mirador," and "Hammam Ritual" are transcribed correctly even if they don't appear in standard speech recognition training data.

---

## CHUNK: What does the Voxtera admin dashboard do?

The admin dashboard (in development) allows hotel managers to:

- Monitor active and recent voice sessions
- Review conversation transcripts
- See ticket creation activity
- Update the knowledge base content
- Configure bot behavior (language settings, action categories, notification routing)
- View usage metrics (total conversations, language distribution, resolution rate)

The admin panel gives the hotel full visibility into how the AI concierge is performing and what guests are asking most.

---

## CHUNK: Does Voxtera require any hardware installation?

No hardware installation is required. Voxtera is entirely software-based:

- **Web widget**: A two-line JavaScript snippet added to the hotel website by the web team
- **Phone line**: A phone number provisioned in Twilio (a cloud telephony provider) — no physical PBX hardware required
- **Backend**: Hosted on cloud servers (DigitalOcean / any cloud provider) — nothing installed on-premises

The entire deployment is managed remotely by the Voxtera team. The hotel's IT team involvement is minimal.

---

## CHUNK: Is Voxtera only for voice, or can guests also type?

Voxtera supports three input modes:

1. **Voice only**: Guest speaks, bot replies with audio. Classic voice concierge experience.
2. **Text only**: Guest types, bot replies with audio. Useful in quiet environments (libraries, late-night in shared spaces) or for guests who are hearing-impaired or simply prefer typing.
3. **Hybrid (default)**: Guest can speak or type on any given turn, mixing freely. The bot always replies with voice.

This flexibility makes Voxtera accessible to a wider range of guests and usage contexts.

---

## CHUNK: What technology stack does Voxtera use?

For technically curious evaluators:

- **Language**: Python 3.12+
- **Pipeline orchestration**: Pipecat (real-time AI voice pipeline framework)
- **STT**: OpenAI Whisper (whisper-1) or Gladia Solaria-1
- **LLM**: Anthropic Claude Haiku 4.5 (primary) / Claude Sonnet 4.6 (higher quality option)
- **TTS**: Google Chirp 3 HD (production primary) / OpenAI TTS (fallback)
- **VAD**: Silero Voice Activity Detection
- **Noise suppression**: RNNoise (neural denoiser)
- **Transport**: Daily.co WebRTC (web/app channels) / Twilio (phone channel)
- **Vector store**: SQLite (MVP) / pgvector or Qdrant (production scale)
- **Embeddings**: OpenAI text-embedding-3-small (multilingual)
- **Hosting**: DigitalOcean Droplet / Docker + Nginx

---

## CHUNK: What integrations are on the Voxtera roadmap?

Near-term roadmap integrations:

- **PMS (Property Management System)**: Direct API integration for live room status, guest profile lookup, booking data
- **Spa/restaurant booking systems**: Real-time availability check and booking creation
- **Loyalty programs**: Guest recognition and personalized responses based on membership tier
- **CRM webhooks**: Push conversation summaries and tickets to hotel CRM platforms
- **More ticketing platforms**: Slack, email, Jira Service Desk, Salesforce, in addition to existing Telegram support

---

## CHUNK: How does Voxtera protect against misuse?

Voxtera's AI layer is configured to:

- Stay in scope: answer only questions relevant to the hotel and guest needs; decline irrelevant or inappropriate requests
- Not generate harmful content
- Refer sensitive matters (medical emergencies, security incidents) to human staff immediately
- Never claim to be human when sincerely asked whether it's an AI
- Operate within the persona defined by the hotel (hotel name, tone, language)

The system prompt and content guardrails are configured per property and reviewed as part of the onboarding process.

---

## CHUNK: What is the competitive landscape for Voxtera?

Voxtera competes in the AI hotel assistant space. Its key differentiators against alternatives:

**vs. existing hotel IVR / phone trees**: Voxtera handles natural language in 99+ languages vs. pre-recorded multi-choice menus in 2–4 languages. No comparison for international properties.

**vs. English-only AI chatbots (Ada, Certainly, etc.)**: These are typically text-only or English-focused. Voxtera is voice-first and genuinely multilingual.

**vs. human front desk staff**: Not a replacement — a complement. Voxtera handles repetitive informational queries around the clock so staff can focus on high-value interactions.

**vs. Google/Alexa smart room speakers**: General consumer devices not configured for hospitality; no hotel-specific knowledge, no ticket routing, no multilingual design for international guests.

**vs. other hotel-specific AI vendors**: Most hospitality AI solutions are text-chat-first with limited or no voice, and support fewer than 20 languages. Voxtera's 99+ language voice capability is a meaningful gap.

---

## CHUNK: What is the ROI for a hotel deploying Voxtera?

Return on investment for hotels comes from several directions:

1. **Staff time savings**: The average hotel front desk receives dozens of identical informational questions per day. Automating these with Voxtera frees staff hours for higher-value work and reduces the cost of staffing overnight shifts for multilingual coverage.

2. **Guest satisfaction**: Faster responses, no hold times, and answers in the guest's own language measurably improve satisfaction scores (NPS, TripAdvisor, Google reviews).

3. **Revenue enablement**: A conversational bot that proactively mentions the spa, dinner reservations, or room upgrades can increase ancillary revenue per stay.

4. **Issue resolution speed**: Maintenance tickets created instantly in real time (vs. a guest remembering to mention an issue at checkout) mean problems are fixed faster, reducing complaints and compensation costs.

5. **Competitive differentiation**: For hotels competing for international guests, offering voice service in the guest's own language is a meaningful differentiator in property selection.

---

## CHUNK: Frequently asked questions from hotel decision-makers

**Q: Do I need to buy any hardware?**
A: No. Voxtera is entirely software. The web widget is a two-line embed; the phone line is a cloud number via Twilio. Nothing is installed on-premises.

**Q: How long does it take to get Voxtera live?**
A: Typically 2–5 business days after the hotel provides its content documents. The Voxtera team handles all technical setup.

**Q: What if a guest asks something the bot doesn't know?**
A: The bot responds honestly ("I don't have that information, please call the front desk") and can transfer the call or log a callback request. It never invents an answer.

**Q: Can I update the menu or policies myself?**
A: Yes. Upload the revised document and the bot's knowledge updates within minutes. No technical skills required.

**Q: Is our guest data safe?**
A: Yes. Audio is processed in real time and not stored. Conversation data is isolated per property. Voxtera complies with standard data protection requirements.

**Q: What languages do you support?**
A: 99+ languages via automatic detection. No additional configuration needed for a new language — the bot handles it automatically.

**Q: Does it work on the phone?**
A: Yes. Via Twilio integration, guests can call a regular phone number and speak to Voxtera exactly as they would a voice agent. No smartphone or app required.

**Q: Can it handle multiple guests at the same time?**
A: Yes. The cloud architecture scales to handle concurrent sessions across all channels simultaneously.

**Q: What does it cost?**
A: Pricing is based on property count and usage volume. Contact the Voxtera founding team for a pricing discussion and the current design-partner offer.

---

## CHUNK: Frequently asked questions from travelers (end users)

**Q: Do I have to press anything or choose a language?**
A: No. Just start speaking in your language and the bot will reply in that same language automatically.

**Q: Can I interrupt the bot if it's speaking?**
A: Yes. Start speaking at any time and the bot will stop and listen to you.

**Q: What if I want to type instead of speak?**
A: The bot supports text input. Type your question and the bot will reply with a spoken answer (and text if the interface shows it).

**Q: Is this a real person or a machine?**
A: Voxtera is an AI assistant. If you ask directly, it will always tell you it is an AI. For complex or sensitive matters, it will offer to connect you with a real staff member.

**Q: Can it book things for me?**
A: Currently it can log requests and create service tickets. Booking integration (spa, restaurant, room upgrades) is in active development.

**Q: What if my language is very uncommon?**
A: Voxtera supports 99+ languages. If the bot cannot understand or respond adequately in a very rare language, it will let you know and offer an alternative (e.g., suggest a more widely supported language or connect to a staff member).

---

## CHUNK: Contact and next steps

Voxtera is actively recruiting founding-cohort hotel design partners. If you are a hotel operator or hospitality group interested in:

- A live demo with the Grand Hôtel Lumière demo environment
- A pilot deployment at your property
- A discussion about pricing and partnership terms
- Technical integration questions

Contact the Voxtera founding team at **dan@voxtera.io** or visit **voxtera.ai**.

---

*Document version: May 2026. This knowledge base is maintained by the Voxtera team and updated with each product release.*
