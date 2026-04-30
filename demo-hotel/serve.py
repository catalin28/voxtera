"""Simple HTTP server for the demo frontend with TTS test and chat endpoints.

Serves static files for the demo page and exposes:

- ``POST /api/tts-test`` — real OpenAI / Google TTS so the browser can
  play the bot's greeting in the selected voice and language.
- ``POST /api/chat`` — full conversational endpoint that uses the Voxtera
  system prompt, RAG retrieval over hotel knowledge, and OpenAI GPT for
  the LLM response.  Returns JSON with ``text`` and optional base64 TTS
  audio so chat mode works entirely over HTTP (no Daily / daily-python).
"""

import asyncio
import base64
import contextlib
import http.server
import json
import socketserver
import sys
import uuid
from pathlib import Path

# Ensure the voxtera package is importable when running from demo-hotel/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from voxtera.prompts.greetings import GREETINGS  # noqa: E402
from voxtera.prompts.system_prompt import SYSTEM_PROMPT  # noqa: E402
from voxtera.actions import load_hotel_config, compose_system_prompt, build_openai_tools  # noqa: E402
from voxtera.actions.logging_sink import LoggingSink  # noqa: E402
from voxtera.actions.ticket import Category, Ticket  # noqa: E402

# ---------------------------------------------------------------------------
# Actions: OpenAI function-calling tool definition for create_ticket
# ---------------------------------------------------------------------------
_hotel_config = load_hotel_config("demo")
_ACTIONS_SYSTEM_PROMPT = compose_system_prompt(SYSTEM_PROMPT, _hotel_config)
_logging_sink = LoggingSink()

# ---------------------------------------------------------------------------
# Load tool definitions from one source (`voxtera.actions.tool`) and allow
# JSON no-code overrides from config/tools/*.json.
# ---------------------------------------------------------------------------
_TOOLS = build_openai_tools(_hotel_config)

# Language code → full name map for the LLM translation prompt.
_LANG_NAMES: dict[str, str] = {
    "en": "English", "fr": "French", "es": "Spanish", "de": "German",
    "it": "Italian", "pt": "Portuguese", "ro": "Romanian", "tr": "Turkish",
    "nl": "Dutch", "ja": "Japanese", "hi": "Hindi", "ru": "Russian",
    "ar": "Arabic", "zh": "Chinese", "ko": "Korean", "pl": "Polish",
    "bg": "Bulgarian", "cs": "Czech", "da": "Danish", "el": "Greek",
    "fi": "Finnish", "he": "Hebrew", "hu": "Hungarian", "id": "Indonesian",
    "no": "Norwegian", "sv": "Swedish", "th": "Thai", "uk": "Ukrainian",
    "vi": "Vietnamese", "az": "Azerbaijani",
}


def _translate_greeting(text: str, lang: str, model: str) -> str:
    """Use an OpenAI LLM to translate the greeting into the target language."""
    import os

    import openai

    client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    lang_name = _LANG_NAMES.get(lang, lang)
    response = client.chat.completions.create(
        model=model,
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"Translate the following greeting into {lang_name}. "
                "Return ONLY the translated text, nothing else.\n\n"
                f"{text}"
            ),
        }],
    )
    return response.choices[0].message.content.strip()


# ---------------------------------------------------------------------------
# RAG retriever (shared across requests, initialised lazily)
# ---------------------------------------------------------------------------
_retriever = None
_rag_ready = False


def _init_rag():
    """Initialise the RAG retriever once (thread-safe via GIL for first call)."""
    global _retriever, _rag_ready
    if _rag_ready:
        return
    try:
        from voxtera.rag.embeddings import embed_sync
        from voxtera.rag.retriever import Retriever
        from voxtera.rag.store import ChunksStore

        embed_sync(["warmup"])  # warm up embedding model

        default_db = str(Path.home() / ".voxtera" / "voxtera.db")
        import os
        db_path = Path(os.environ.get("VOXTERA_DB_PATH", default_db))
        if db_path.exists():
            store = ChunksStore(db_path)
            store.init_schema()
            _retriever = Retriever(store)
            print(f"[chat] RAG retriever ready (db={db_path})")
        else:
            print(f"[chat] RAG database not found at {db_path}, running without RAG")
    except Exception as exc:
        print(f"[chat] RAG init failed ({exc}), running without RAG")
    _rag_ready = True


def _rag_context(query: str, hotel_id: str = "demo") -> str:
    """Retrieve RAG chunks for a query and return formatted context string."""
    if _retriever is None:
        return ""
    try:
        loop = asyncio.new_event_loop()
        results = loop.run_until_complete(
            _retriever.retrieve(hotel_id=hotel_id, query=query)
        )
        loop.close()
        if not results:
            return ""
        excerpts = "\n\n".join(f"[{r.doc_id}] {r.text}" for r in results)
        return (
            "Here are relevant excerpts from the hotel's information. Use them when "
            "answering, but only if they're relevant to the user's most recent "
            "question. If they don't answer that question, ignore them.\n\n"
            + excerpts
        )
    except Exception as exc:
        print(f"[rag] retrieval error: {exc}")
        return ""


# Initialise RAG at startup so the first chat request is fast.
_init_rag()

# ---------------------------------------------------------------------------
# Chat sessions — simple in-memory conversation history keyed by session id
# ---------------------------------------------------------------------------
_sessions: dict[str, list[dict[str, str]]] = {}


def _handle_tool_call(tool_call, session_id: str) -> str:
    """Dispatch tool calls by function name."""
    name = tool_call.function.name
    args = json.loads(tool_call.function.arguments)
    print(f"[actions] LLM called {name} with: {args}")

    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        return json.dumps({"status": "error", "reason": f"Unknown tool: {name}"})

    return handler(args, session_id)


def _handle_create_ticket(args: dict, session_id: str) -> str:
    """Execute a create_ticket tool call via LoggingSink and return result JSON."""
    import asyncio

    try:
        category = Category(args["category"])
    except (ValueError, KeyError):
        return json.dumps({"status": "rejected", "reason": f"Invalid category: {args.get('category')}"})

    ticket = Ticket(
        category=category,
        summary=args.get("summary", ""),
        room_number=args.get("room_number", ""),
        original_quote=args.get("original_quote", ""),
        language_detected=args.get("language_detected", ""),
    )

    loop = asyncio.new_event_loop()
    ok = loop.run_until_complete(_logging_sink.send(ticket))
    loop.close()

    if ok:
        return json.dumps({"status": "filed", "category": category.value, "session_id": session_id})
    return json.dumps({"status": "failed"})


# Tool execution registry for the HTTP OpenAI function-calling path.
_TOOL_HANDLERS = {
    "create_ticket": _handle_create_ticket,
}


def _chat_completion(session_id: str, user_text: str, model: str, language: str) -> str:
    """Run one chat turn: RAG retrieval → OpenAI chat completion → reply text."""
    import os
    import openai

    if session_id not in _sessions:
        _sessions[session_id] = [{"role": "system", "content": _ACTIONS_SYSTEM_PROMPT}]

    messages = _sessions[session_id]

    # Inject RAG context before the user message.
    rag_ctx = _rag_context(user_text)
    if rag_ctx:
        messages.append({"role": "system", "content": rag_ctx})

    messages.append({"role": "user", "content": user_text})

    client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    response = client.chat.completions.create(
        model=model,
        max_tokens=512,
        messages=messages,
        tools=_TOOLS or None,
        tool_choice="auto" if _TOOLS else None,
    )

    msg = response.choices[0].message

    # Handle tool calls: execute the function, feed result back, get final reply.
    if msg.tool_calls:
        # Append the assistant message with tool_calls.
        messages.append(msg.model_dump())
        for tc in msg.tool_calls:
            result = _handle_tool_call(tc, session_id)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })
        # Second LLM call to get the final spoken reply.
        response2 = client.chat.completions.create(
            model=model,
            max_tokens=512,
            messages=messages,
            tools=_TOOLS or None,
            tool_choice="auto" if _TOOLS else None,
        )
        reply = response2.choices[0].message.content or ""
        messages.append({"role": "assistant", "content": reply})
    else:
        reply = msg.content or ""
        messages.append({"role": "assistant", "content": reply})

    # Keep history bounded (system + last 40 turns).
    if len(messages) > 42:
        _sessions[session_id] = [messages[0]] + messages[-40:]

    return reply.strip()


def _tts_openai(text: str, voice: str) -> bytes:
    """Generate speech via OpenAI tts-1 and return raw MP3 bytes."""
    import os

    from openai import OpenAI

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    response = client.audio.speech.create(model="tts-1", voice=voice, input=text)
    return response.content


def _tts_google(text: str, voice: str, language: str) -> bytes:
    """Generate speech via Google Chirp 3 HD and return raw MP3 bytes."""
    import os

    from google.cloud import texttospeech

    os.environ.setdefault(
        "GOOGLE_APPLICATION_CREDENTIALS",
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""),
    )
    client = texttospeech.TextToSpeechClient()
    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice_params = texttospeech.VoiceSelectionParams(
        language_code=language if "-" in language else f"{language}-US",
        name=voice,
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
    )
    response = client.synthesize_speech(
        input=synthesis_input, voice=voice_params, audio_config=audio_config
    )
    return response.audio_content


class DemoHandler(http.server.SimpleHTTPRequestHandler):
    """Serves static files + the /api/tts-test endpoint."""

    def handle_one_request(self):
        with contextlib.suppress(ConnectionResetError):
            super().handle_one_request()

    def log_message(self, format, *args):  # noqa: A002
        msg = format % args
        sys.stderr.write(f"{self.address_string()} - - [{self.log_date_time_string()}] {msg}\n")

    def do_POST(self):  # noqa: N802
        if self.path == "/api/tts-test":
            return self._handle_tts_test()
        if self.path == "/api/chat":
            return self._handle_chat()
        self.send_error(404)

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _handle_tts_test(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            provider = body.get("provider", "openai")
            voice = body.get("voice", "nova")
            lang = body.get("language", "en")
            model = body.get("model", "gpt-4o-mini")
            if lang == "multi":
                lang = "en"

            # Use pre-built greeting if available, otherwise translate via LLM.
            text = GREETINGS.get(lang)
            if not text:
                base = GREETINGS["en"]
                text = _translate_greeting(base, lang, model)

            if provider == "google":
                audio = _tts_google(text, voice, lang)
            else:
                audio = _tts_openai(text, voice)

            self.send_response(200)
            self._cors_headers()
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(audio)))
            self.end_headers()
            self.wfile.write(audio)
        except Exception as exc:
            error_msg = json.dumps({"error": str(exc)}).encode()
            self.send_response(500)
            self._cors_headers()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(error_msg)))
            self.end_headers()
            self.wfile.write(error_msg)

    def _handle_chat(self):
        """POST /api/chat — LLM chat with RAG + TTS audio response."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            text = (body.get("text") or "").strip()
            session_id = body.get("session_id") or str(uuid.uuid4())
            model = body.get("model") or "gpt-4o-mini"
            language = body.get("language") or "en"
            tts_provider = body.get("tts_provider") or "openai"
            voice = body.get("voice") or "nova"

            if not text:
                resp = json.dumps({"error": "text is required"}).encode()
                self.send_response(400)
                self._cors_headers()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
                return

            # LLM chat with RAG context injection.
            reply = _chat_completion(session_id, text, model, language)

            # Generate TTS audio for the reply.
            audio_b64 = ""
            try:
                if tts_provider == "google":
                    audio = _tts_google(reply, voice, language)
                else:
                    audio = _tts_openai(reply, voice)
                audio_b64 = base64.b64encode(audio).decode("ascii")
            except Exception as tts_exc:
                print(f"[chat] TTS failed ({tts_exc}), returning text only")

            resp = json.dumps({
                "text": reply,
                "audio": audio_b64,
                "session_id": session_id,
            }).encode()
            self.send_response(200)
            self._cors_headers()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        except Exception as exc:
            error_msg = json.dumps({"error": str(exc)}).encode()
            self.send_response(500)
            self._cors_headers()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(error_msg)))
            self.end_headers()
            self.wfile.write(error_msg)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    with socketserver.ThreadingTCPServer(("", port), DemoHandler) as httpd:
        httpd.allow_reuse_address = True
        print(f"Serving demo on http://localhost:{port}/demo.html")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
