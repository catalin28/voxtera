# Voxtera — Full System Data-Flow Diagram

Generated 2026-06-09 from the code in `src/`, `demo-hotel/`, and `scripts/`.

## 1. Full system diagram

```mermaid
flowchart TB

%% ───────────── ENTRY CHANNELS ─────────────
subgraph CHANNELS["Entry channels"]
    BROWSER["Browser voice widget<br/>(Daily WebRTC)"]
    CHATUI["Browser chat<br/>(HTTP, no Daily)"]
    PHONE["PSTN phone call<br/>(Daily dial-in)"]
    WAUSER["WhatsApp user<br/>(text + voice calls)"]
    CLI["Local CLI<br/>make run / voxtera"]
end

%% ───────────── LAUNCHER ─────────────
subgraph LAUNCHER["demo-hotel/serve.py — launcher + admin (:8080)"]
    START["POST /api/start-session<br/>spawns bot subprocess<br/>(VOXTERA_SESSION_ID, DIALIN_*)"]
    CHATEP["POST /api/chat<br/>RAG + OpenAI GPT + TTS → JSON"]
    WAWH["GET/POST /whatsapp/webhook<br/>HMAC verify, dedupe, ACK-fast"]
    BOTEVT["POST /api/bot-event<br/>→ SSE /trace.html"]
    ADMINEP["/api/admin/*<br/>sessions, calls, prompts,<br/>ES/Qdrant browser, eject"]
end

BROWSER -->|"join"| START
PHONE -->|"Daily phone gateway<br/>DIALIN_CALL_ID/DOMAIN<br/>(pstn_auth HMAC verified)"| START
CHATUI --> CHATEP
WAUSER -->|"Meta Cloud API webhook"| WAWH
START -->|"spawn + env"| BOT
CLI -->|"LocalAudioTransport"| BOT

%% ───────────── VOICE BOT PIPELINE ─────────────
subgraph BOT["Voice bot process — bot.py / pipeline.py (Pipecat)"]
    direction TB
    TIN["transport.input()<br/>Daily 16 kHz / Local mic"]
    PREP["RawInputRecorder → HighShelfPreEmphasis<br/>→ RNNoiseDenoiser (opt) → PlaybackLeakageGuard<br/>→ AudioLevelMonitor"]
    VAD["Silero VAD<br/>(stop_secs, PstnIdleWatcher on PSTN)"]
    STTR{"STTRouter + STTGates<br/>(app-message voxtera-stt)"}
    STT["Active STT branch<br/>Whisper-1 · Deepgram Nova-3 · Gladia Solaria-1 (lazy-connect)<br/>· Google STT V2 · ElevenLabs Scribe v2"]
    TNF["TranscriptionNoiseFilter<br/>+ confidence gating (stt_thresholds.json)"]
    LANGSW["AutoTTSLanguageSwitcher<br/>+ InstantAckFiller + LanguageSwitcher"]
    CTXU["context_aggregator.user()<br/>+ LLMRunGuard + BrowserTextInputController"]
    BRAIN{"BOT_BRAIN"}
    RAGINJ["RAGContextInjector<br/>top-k cosine, 5 s timeout"]
    LLM["AnthropicLLMService<br/>Claude (Haiku default), prompt caching,<br/>max_tokens 250, tools"]
    TAB["TravelAgentBrain<br/>POST /api/concierge/stream<br/>(NDJSON: text chunks + done result)"]
    CTXA["context_aggregator.assistant()"]
    TTSR{"TTSRouter + TTSGates<br/>(voxtera-tts-provider)"}
    TTS["Active TTS branch<br/>OpenAI tts-1 · Google Chirp 3 HD<br/>· Cartesia Sonic-3 · ElevenLabs Flash v2.5"]
    TOUT["transport.output()<br/>48 kHz WebRTC / 8 kHz PSTN / 24 kHz local"]
end

TIN --> PREP --> VAD --> STTR --> STT --> TNF --> LANGSW --> CTXU --> BRAIN
BRAIN -->|"hotel"| RAGINJ --> LLM
BRAIN -->|"travel_agent"| TAB
LLM --> CTXA
TAB --> CTXA
CTXA --> TTSR --> TTS --> TOUT
TOUT -->|"bot audio + transcripts<br/>(app-messages)"| BROWSER
TOUT --> PHONE
LANGSW -.->|"TTSUpdateSettingsFrame<br/>locale-Chirp3-HD-character"| TTS

%% ───────────── RAG (hotel brain) ─────────────
subgraph RAGSTORE["RAG layer (hotel brain)"]
    EMB["Embeddings: multilingual-e5<br/>ONNX in-proc or sidecar :9400"]
    SQLITE[("SQLite ChunksStore<br/>chunks(hotel_id, lang, category,<br/>text, embedding)")]
end
RAGINJ --> EMB --> SQLITE
CHATEP --> EMB

%% ───────────── CONCIERGE / CALL-CENTER ─────────────
TAB ==>|"utterance, region,<br/>session_id, brief"| CP
WAWH ==>|"session_id = wa_id"| CP

subgraph CP["ConciergePipeline — call_center/ (runs in serve.py loop)"]
    direction TB
    ESC["EscalationClassifier<br/>GPT-4.1-nano"]
    DEC["QueryDecomposer<br/>Claude Haiku → intent, query_type,<br/>region, filters"]
    SESS["SessionStore<br/>last_results, region, full transcript"]
    ROUTE{"Triage + SourceRouter"}
    RESOLVE["HotelResolver<br/>(name → hotel_id,<br/>0.82 strong-score gate)"]
    KBRET["HotelKBRetriever<br/>(SCOPED, hotel-filtered)"]
    BROAD["BroadHotelDiscovery"]
    COMP["CompoundAndDiscovery"]
    WEBR["WebRetriever<br/>(web search)"]
    CONV["_handle_converse<br/>(conversational, answers<br/>from transcript)"]
    RER["CrossEncoderReranker<br/>bge-reranker-v2-m3 (opt)"]
    REN["Render answer<br/>Claude Haiku, localized en/tr,<br/>brief mode for voice"]
end

ESC --> DEC --> SESS --> ROUTE
ROUTE --> RESOLVE & KBRET & BROAD & COMP & WEBR & CONV
RESOLVE & KBRET & BROAD & COMP --> RER --> REN
WEBR --> REN
CONV --> REN
REN -->|"NDJSON stream"| TAB
REN -->|"text reply"| WACLIENT["WhatsAppClient<br/>(Graph API send)"]
WACLIENT --> WAUSER
WAWH -->|"voice call event"| WACALL["run_call_bot<br/>(Pipecat WhatsApp transport)"] --> BOT

%% ───────────── DATA STORES ─────────────
subgraph STORES["Data stores"]
    REDIS[("Redis<br/>session memory, 30 min TTL<br/>(in-memory fallback)")]
    ES[("Elasticsearch<br/>hotels index, Turkish analyzer")]
    QD[("Qdrant<br/>hotel_kb collection,<br/>1024-dim e5-large")]
    MYSQL[("MySQL leads DB<br/>LeadsStore: calls, leads,<br/>Cal.com booking ids")]
end
SESS --> REDIS
RESOLVE --> ES
KBRET & BROAD & COMP --> QD
CP -.->|"website-concierge<br/>(leads API)"| MYSQL

%% ───────────── ACTIONS / TOOLS ─────────────
subgraph ACTIONS["Actions & LLM tools (actions/)"]
    ART["ActionsRuntime<br/>create_ticket tool"]
    TG["TelegramSink<br/>Bot API sendMessage<br/>+ inline buttons"]
    LISTN["Listener<br/>(staff button taps)"]
    EXTH["web_search · find_videos (YouTube)<br/>· find_reviews (Google Places)"]
end
LLM -->|"tool call"| ART --> TG --> STAFF["Hotel staff<br/>Telegram channel"]
STAFF --> LISTN --> ART
LLM -->|"tool call"| EXTH

%% ───────────── OBSERVABILITY ─────────────
subgraph OBS["Observability & persistence"]
    TRACE["TraceBus (ring 5000)<br/>→ TraceForwarder"]
    TUNE["TuneServer 127.0.0.1:port<br/>/knobs /tune /speak"]
    REC["logs/calls/&lt;sid&gt;/<br/>record.json + recording.wav<br/>+ stage_*.wav (debug)"]
    CONVLOG["~/.voxtera/logs/<br/>conversations-*.jsonl<br/>(query, RAG chunks, latencies)"]
end
BOT --> TRACE --> BOTEVT --> DASH["trace.html / admin<br/>live dashboards"]
BOT --> REC
BOT --> CONVLOG
ADMINEP --> TUNE
ADMINEP --> REC

%% ───────────── OFFLINE INGESTION ─────────────
subgraph INGEST["Offline ingestion (scripts/)"]
    SCRAPE["scrape_parafly_hotels.py<br/>(Playwright)"]
    SEED["data/seed/hotels.json<br/>+ PDFs / Markdown KB"]
    PIPE2["loaders (PyMuPDF+OCR, text)<br/>→ chunker (tiktoken, 400/600 tok)<br/>→ e5 embeddings"]
    ING["ingest_product_kb.py → SQLite<br/>ingest_hotels.py → ES + Qdrant"]
end
SCRAPE --> SEED --> PIPE2 --> ING
ING --> SQLITE
ING --> ES
ING --> QD
```

## 2. One voice turn (sequence)

```mermaid
sequenceDiagram
    participant U as Guest (browser/PSTN)
    participant D as Daily WebRTC
    participant P as Pipecat pipeline
    participant S as STT (active branch)
    participant B as Brain (Claude / Concierge)
    participant T as TTS (active branch)

    U->>D: speech (16 kHz)
    D->>P: InputAudioRawFrame
    P->>P: pre-emphasis → denoise → leakage guard → Silero VAD
    P->>S: audio (gated to active branch)
    S-->>P: TranscriptionFrame(text, language)
    P->>P: noise filter → AutoTTSLanguageSwitcher (Chirp3 locale) → InstantAck filler
    P->>B: LLMContext (+ RAG chunks injected from SQLite)
    alt BOT_BRAIN=travel_agent
        B->>B: POST /api/concierge/stream → decompose → route → ES/Qdrant/Redis → render
    end
    B-->>P: LLMTextFrames (streamed, ≤250 tokens)
    P->>T: TTSSpeakFrame (gated to active branch)
    T-->>D: TTSAudioRawFrame (48 kHz)
    D-->>U: bot voice
    Note over P: TraceBus → launcher dashboard<br/>record.json + wav persisted per turn
```

## 3. Key facts behind the arrows

- **Two brains** (`BOT_BRAIN`): `hotel` = in-process RAG (SQLite + e5) + Claude with tools; `travel_agent` = delegates each utterance to the ConciergePipeline over streaming NDJSON, same downstream frames so TTS/tracing are unchanged.
- **STT/TTS hot-swap**: in Daily mode all providers run as parallel gated branches; browser app-messages (`voxtera-stt`, `voxtera-tts-provider`) flip gates; Gladia lazy-connects to dodge the 1-session free-tier cap.
- **Language flow**: STT auto-detects → `AutoTTSLanguageSwitcher` rewrites the Chirp 3 HD voice id (`{locale}-Chirp3-HD-{character}`) mid-call; filler acks are language-matched.
- **WhatsApp text** shares the concierge with voice: `wa_id` is the session id, so Redis memory and region persist per contact. WhatsApp **calls** spawn a Pipecat call bot.
- **ConciergePipeline retrieval paths**: SCOPED (hotel KB), BROAD discovery, COMPOUND (multi-constraint), RESOLVE (name→hotel, 0.82 strong-score gate), WEB, and `conversational` (`_handle_converse`, answers from transcript, no retrieval).
- **LLM split**: escalation = GPT-4.1-nano; decompose/render/converse = Claude Haiku; voice bot = Claude (Haiku default) with prompt caching, 250-token cap.
- **Persistence**: atomic `record.json` + stereo WAV per call (`logs/calls/<sid>/`), JSONL conversation audit (`~/.voxtera/logs/`), Redis sessions (30 min TTL), MySQL leads (website-concierge).
- **Observability**: in-proc TraceBus → HTTP forward to `serve.py /api/bot-event` → SSE `trace.html`; per-bot TuneServer for live knob edits and injected speech.
