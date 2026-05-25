"""Voxtera Test Runner — author a script of questions, generate voice clips,
and rehearse them one by one against the Voxtera bot.

The whole app is built around one "test script": an ordered list of
questions. You:
  1. Add questions (type English; pick a language/voice; translate or
     speak-as-typed). Each question keeps its own language.
  2. Generate audio for the whole script in one click.
  3. Rehearse — step through the script, playing each clip into the bot's
     mic (via BlackHole) and advancing when the bot has answered.

Scripts save to JSON so a good sequence is reusable.

Platform: macOS. Audio conversion/playback use the built-in `afconvert`
and `afplay` tools, so no ffmpeg is required.

Setup (run inside the project venv):
    uv pip install edge-tts          # or: pip install edge-tts
    # tkinter ships with python.org Python. With Homebrew Python:
    #   brew install python-tk@3.12

Run:
    python3 voice_test_lab.py

Translation uses ANTHROPIC_API_KEY (preferred) or OPENAI_API_KEY from the
project .env.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import subprocess
import threading
import tkinter as tk
import unicodedata
import uuid
import wave
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    import edge_tts

    EDGE_TTS_AVAILABLE = True
except ImportError:
    EDGE_TTS_AVAILABLE = False

# --- paths -----------------------------------------------------------------
APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent.parent  # tools/voice-test-lab -> repo root
CLIPS_DIR = APP_DIR / "clips"
SCRIPTS_DIR = APP_DIR / "scripts"
CLIPS_DIR.mkdir(parents=True, exist_ok=True)
SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

# Silence padding so Silero VAD reliably catches the start/end of the turn.
LEAD_SILENCE_S = 0.3
TRAIL_SILENCE_S = 1.0

# LLM models for translation (filename naming no longer needs an LLM).
ANTHROPIC_MODEL = "claude-sonnet-4-6"
OPENAI_MODEL = "gpt-4o-mini"

# locale -> friendly language name, for nicer dropdown labels
LOCALE_NAMES = {
    "en-US": "English (US)",
    "en-GB": "English (UK)",
    "en-AU": "English (Australia)",
    "ro-RO": "Romanian",
    "tr-TR": "Turkish",
    "fr-FR": "French",
    "de-DE": "German",
    "es-ES": "Spanish (Spain)",
    "es-MX": "Spanish (Mexico)",
    "it-IT": "Italian",
    "pt-BR": "Portuguese (Brazil)",
    "pt-PT": "Portuguese (Portugal)",
    "ru-RU": "Russian",
    "ar-SA": "Arabic",
    "ar-EG": "Arabic (Egypt)",
    "ja-JP": "Japanese",
    "ko-KR": "Korean",
    "zh-CN": "Chinese (Mandarin)",
    "zh-HK": "Chinese (Cantonese)",
    "nl-NL": "Dutch",
    "el-GR": "Greek",
    "hi-IN": "Hindi",
    "pl-PL": "Polish",
    "hy-AM": "Armenian",
    "uk-UA": "Ukrainian",
    "th-TH": "Thai",
    "vi-VN": "Vietnamese",
    "id-ID": "Indonesian",
    "sv-SE": "Swedish",
    "da-DK": "Danish",
    "nb-NO": "Norwegian",
    "fi-FI": "Finnish",
    "cs-CZ": "Czech",
    "hu-HU": "Hungarian",
    "he-IL": "Hebrew",
    "bg-BG": "Bulgarian",
}

# Used only if edge-tts cannot be reached at startup to list live voices.
FALLBACK_VOICES = [
    {"ShortName": "en-US-AriaNeural", "Locale": "en-US", "Gender": "Female"},
    {"ShortName": "en-US-GuyNeural", "Locale": "en-US", "Gender": "Male"},
    {"ShortName": "ro-RO-AlinaNeural", "Locale": "ro-RO", "Gender": "Female"},
    {"ShortName": "ro-RO-EmilNeural", "Locale": "ro-RO", "Gender": "Male"},
    {"ShortName": "tr-TR-EmelNeural", "Locale": "tr-TR", "Gender": "Female"},
    {"ShortName": "tr-TR-AhmetNeural", "Locale": "tr-TR", "Gender": "Male"},
    {"ShortName": "fr-FR-DeniseNeural", "Locale": "fr-FR", "Gender": "Female"},
    {"ShortName": "de-DE-KatjaNeural", "Locale": "de-DE", "Gender": "Female"},
    {"ShortName": "es-ES-ElviraNeural", "Locale": "es-ES", "Gender": "Female"},
    {"ShortName": "ru-RU-SvetlanaNeural", "Locale": "ru-RU", "Gender": "Female"},
]


# --- helpers ---------------------------------------------------------------
def new_qid() -> str:
    """Short unique id for a question (also used in its audio filename)."""
    return uuid.uuid4().hex[:8]


def load_api_keys() -> dict[str, str]:
    """Read ANTHROPIC_API_KEY / OPENAI_API_KEY from the project .env."""
    keys: dict[str, str] = {}
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            name = name.strip()
            value = value.strip().strip('"').strip("'")
            if name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY") and value:
                keys[name] = value
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        if os.environ.get(name):
            keys.setdefault(name, os.environ[name])
    return keys


def slugify(text: str, max_words: int = 5) -> str:
    """Best-effort ASCII snake_case slug for filenames."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-zA-Z0-9]+", ascii_text.lower())
    return "_".join(words[:max_words]) or "clip"


def translate_text(text: str, target_language: str, keys: dict[str, str]) -> str:
    """Translate `text` into `target_language` with an LLM.

    Raises RuntimeError if no API key is available or every call fails.
    """
    prompt = (
        f"Translate the text below into {target_language}. It will be spoken "
        "aloud as a voice clip, so use natural, conversational phrasing. "
        "Reply with ONLY the translation - no quotes, no notes.\n\n" + text
    )
    if keys.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic

            client = anthropic.Anthropic(api_key=keys["ANTHROPIC_API_KEY"])
            msg = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}],
            )
            out = (
                "".join(block.text for block in msg.content if block.type == "text")
                .strip()
                .strip('"')
                .strip("'")
            )
            if out:
                return out
        except Exception:
            pass
    if keys.get("OPENAI_API_KEY"):
        try:
            import openai

            client = openai.OpenAI(api_key=keys["OPENAI_API_KEY"])
            resp = client.chat.completions.create(
                model=OPENAI_MODEL,
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}],
            )
            out = (resp.choices[0].message.content or "").strip().strip('"').strip("'")
            if out:
                return out
        except Exception:
            pass
    raise RuntimeError("Translation needs ANTHROPIC_API_KEY or OPENAI_API_KEY in the project .env")


async def _edge_tts_to_mp3(text: str, voice: str, mp3_path: Path) -> None:
    await edge_tts.Communicate(text, voice).save(str(mp3_path))


def _mp3_to_wav(mp3_path: Path, wav_path: Path) -> None:
    """Convert mp3 -> 16-bit PCM WAV with macOS-native afconvert."""
    proc = subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", "LEI16", str(mp3_path), str(wav_path)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
        raise RuntimeError(f"afconvert failed: {detail}")


def _pad_wav(wav_path: Path, lead_s: float, trail_s: float) -> None:
    """Prepend/append digital silence so VAD reliably detects turn edges."""
    with wave.open(str(wav_path), "rb") as src:
        params = src.getparams()
        frames = src.readframes(src.getnframes())
    bytes_per_frame = params.sampwidth * params.nchannels
    lead = b"\x00" * (int(params.framerate * lead_s) * bytes_per_frame)
    trail = b"\x00" * (int(params.framerate * trail_s) * bytes_per_frame)
    with wave.open(str(wav_path), "wb") as dst:
        dst.setparams(params)
        dst.writeframes(lead + frames + trail)


def generate_clip(text: str, voice: str, out_dir: Path, filename: str) -> Path:
    """Synthesize `text` with `voice` into a padded WAV in `out_dir`."""
    stem = re.sub(r"[^\w.\-]+", "_", Path(filename).stem).strip("_") or "clip"
    wav_path = out_dir / f"{stem}.wav"
    mp3_tmp = out_dir / f"{stem}.tmp.mp3"
    try:
        asyncio.run(_edge_tts_to_mp3(text, voice, mp3_tmp))
        _mp3_to_wav(mp3_tmp, wav_path)
        _pad_wav(wav_path, LEAD_SILENCE_S, TRAIL_SILENCE_S)
    finally:
        if mp3_tmp.exists():
            with contextlib.suppress(OSError):
                mp3_tmp.unlink()
    return wav_path


def load_voices() -> dict[str, list[dict]]:
    """Group every edge-tts voice by locale (falls back to a small static set)."""
    voices: list[dict] = []
    if EDGE_TTS_AVAILABLE:
        try:
            voices = asyncio.run(edge_tts.list_voices())
        except Exception:
            voices = []
    if not voices:
        voices = FALLBACK_VOICES
    by_locale: dict[str, list[dict]] = {}
    for voice in voices:
        by_locale.setdefault(voice["Locale"], []).append(voice)
    for items in by_locale.values():
        items.sort(key=lambda d: d.get("Gender", ""))
    return by_locale


def voice_choices(
    voices_by_locale: dict[str, list[dict]], locale: str
) -> tuple[list[str], dict[str, str]]:
    """Return (display labels, label -> ShortName) for the voices of a locale."""
    labels: list[str] = []
    mapping: dict[str, str] = {}
    for voice in voices_by_locale.get(locale, []):
        short = voice["ShortName"]
        name = short.split("-")[-1].replace("Neural", "")
        label = f"{name}  ·  {voice.get('Gender', '?')}"
        labels.append(label)
        mapping[label] = short
    return labels, mapping


def _open_path(path: Path) -> None:
    """Reveal a file/folder in Finder."""
    with contextlib.suppress(OSError):
        subprocess.run(["open", str(path)], check=False)


# --- add/edit dialog -------------------------------------------------------
class QuestionDialog(tk.Toplevel):
    """Modal dialog to add or edit a single question. Result in `self.result`."""

    def __init__(
        self,
        parent: tk.Misc,
        voices_by_locale: dict,
        locale_labels: dict,
        api_keys: dict,
        question: dict | None = None,
        defaults: dict | None = None,
    ) -> None:
        super().__init__(parent)
        self.voices_by_locale = voices_by_locale
        self.locale_labels = locale_labels  # label -> locale
        self.api_keys = api_keys
        self.result: dict | None = None
        self._voice_map: dict[str, str] = {}
        self._existing_id = question.get("id") if question else None
        self._existing_audio = question.get("audio", "") if question else ""

        self.title("Edit question" if question else "Add question")
        self.transient(parent)
        self.resizable(False, False)

        defaults = defaults or {}
        q = question or {}
        init_locale = q.get("locale") or defaults.get("locale", "")
        init_voice = q.get("voice") or defaults.get("voice", "")
        init_mode = q.get("mode") or defaults.get("mode", "translate")

        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Question (English)").grid(
            row=0, column=0, sticky="nw", pady=4, padx=(0, 8)
        )
        self.english_text = tk.Text(frm, width=54, height=3, wrap="word")
        self.english_text.grid(row=0, column=1, sticky="ew", pady=4)
        self.english_text.insert("1.0", q.get("english", ""))

        ttk.Label(frm, text="Language").grid(row=1, column=0, sticky="w", pady=4)
        self.lang_var = tk.StringVar()
        self.lang_combo = ttk.Combobox(
            frm,
            textvariable=self.lang_var,
            state="readonly",
            values=list(self.locale_labels.keys()),
        )
        self.lang_combo.grid(row=1, column=1, sticky="ew", pady=4)
        self.lang_combo.bind("<<ComboboxSelected>>", self._on_lang_change)

        ttk.Label(frm, text="Voice").grid(row=2, column=0, sticky="w", pady=4)
        self.voice_var = tk.StringVar()
        self.voice_combo = ttk.Combobox(frm, textvariable=self.voice_var, state="readonly")
        self.voice_combo.grid(row=2, column=1, sticky="ew", pady=4)

        ttk.Label(frm, text="Mode").grid(row=3, column=0, sticky="nw", pady=4)
        self.mode_var = tk.StringVar(value=init_mode)
        mode_frame = ttk.Frame(frm)
        mode_frame.grid(row=3, column=1, sticky="w", pady=4)
        ttk.Radiobutton(
            mode_frame,
            text="Translate English into the language",
            value="translate",
            variable=self.mode_var,
            command=self._on_mode_change,
        ).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(
            mode_frame,
            text="Speak my text exactly as typed",
            value="as_typed",
            variable=self.mode_var,
            command=self._on_mode_change,
        ).grid(row=1, column=0, sticky="w")

        self.trans_label = ttk.Label(frm, text="Translation")
        self.trans_label.grid(row=4, column=0, sticky="nw", pady=4)
        self.trans_frame = ttk.Frame(frm)
        self.trans_frame.grid(row=4, column=1, sticky="ew", pady=4)
        self.trans_frame.columnconfigure(0, weight=1)
        self.translation_text = tk.Text(self.trans_frame, width=54, height=3, wrap="word")
        self.translation_text.grid(row=0, column=0, sticky="ew")
        self.translation_text.insert("1.0", q.get("translation", ""))
        self.translate_btn = ttk.Button(
            self.trans_frame, text="Translate", command=self._on_translate
        )
        self.translate_btn.grid(row=0, column=1, sticky="n", padx=(6, 0))

        self.status = tk.StringVar(
            value="Leave Translation blank — it is filled when you generate audio."
        )
        ttk.Label(frm, textvariable=self.status, foreground="#888").grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(6, 8)
        )

        btns = ttk.Frame(frm)
        btns.grid(row=6, column=0, columnspan=2, sticky="e")
        ttk.Button(btns, text="Cancel", command=self._cancel).grid(row=0, column=0, padx=4)
        ttk.Button(btns, text="OK", command=self._ok).grid(row=0, column=1)

        label_for = {loc: lbl for lbl, loc in self.locale_labels.items()}
        if init_locale in label_for:
            self.lang_var.set(label_for[init_locale])
        elif self.locale_labels:
            self.lang_var.set(next(iter(self.locale_labels)))
        self._refresh_voices(preferred=init_voice)
        self._on_mode_change()

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.grab_set()
        self.english_text.focus_set()

    def _on_lang_change(self, _event: object = None) -> None:
        self._refresh_voices()

    def _refresh_voices(self, preferred: str = "") -> None:
        locale = self.locale_labels.get(self.lang_var.get(), "")
        labels, mapping = voice_choices(self.voices_by_locale, locale)
        self._voice_map = mapping
        self.voice_combo["values"] = labels
        chosen = ""
        if preferred:
            for label, short in mapping.items():
                if short == preferred:
                    chosen = label
                    break
        if chosen:
            self.voice_var.set(chosen)
        elif labels:
            self.voice_combo.current(0)
        else:
            self.voice_var.set("")

    def _on_mode_change(self) -> None:
        if self.mode_var.get() == "translate":
            self.trans_label.grid()
            self.trans_frame.grid()
        else:
            self.trans_label.grid_remove()
            self.trans_frame.grid_remove()

    def _on_translate(self) -> None:
        text = self.english_text.get("1.0", "end").strip()
        if not text:
            self.status.set("Type the English text first.")
            return
        self.translate_btn.config(state="disabled")
        self.status.set("Translating...")
        threading.Thread(target=self._translate_worker, args=(text,), daemon=True).start()

    def _translate_worker(self, text: str) -> None:
        locale = self.locale_labels.get(self.lang_var.get(), "")
        language = LOCALE_NAMES.get(locale, locale)
        try:
            out = translate_text(text, language, self.api_keys)
            self.after(0, self._translate_done, out, None)
        except Exception as exc:
            self.after(0, self._translate_done, None, str(exc))

    def _translate_done(self, out: str | None, error: str | None) -> None:
        self.translate_btn.config(state="normal")
        if error or out is None:
            self.status.set(f"Translation error: {error}")
            return
        self.translation_text.delete("1.0", "end")
        self.translation_text.insert("1.0", out)
        self.status.set("Translated — review or edit it, then OK.")

    def _ok(self) -> None:
        english = self.english_text.get("1.0", "end").strip()
        if not english:
            self.status.set("Type the English question first.")
            return
        voice = self._voice_map.get(self.voice_var.get())
        if not voice:
            self.status.set("Pick a voice for this language.")
            return
        mode = self.mode_var.get()
        translation = self.translation_text.get("1.0", "end").strip() if mode == "translate" else ""
        self.result = {
            "id": self._existing_id or new_qid(),
            "english": english,
            "mode": mode,
            "locale": self.locale_labels.get(self.lang_var.get(), ""),
            "voice": voice,
            "translation": translation,
            "audio": self._existing_audio,
        }
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


# --- main window -----------------------------------------------------------
class VoxteraTestRunner(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Voxtera Test Runner")
        self.geometry("880x800")
        self.minsize(780, 680)

        self.api_keys = load_api_keys()
        self.voices_by_locale = load_voices()
        self.locale_labels = self._build_locale_labels()

        self.questions: list[dict] = []
        self.current_id: str | None = None
        self.played: set[str] = set()
        self.script_path: Path | None = None
        self.player_proc: subprocess.Popen | None = None
        self._def_voice_map: dict[str, str] = {}

        self._build_toolbar()
        self._build_defaults()
        self._build_rehearsal()  # packed to the bottom
        self._build_generate_bar()  # packed above rehearsal
        self._build_question_list()  # fills the middle

        self.bind("<space>", self._on_space)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh_tree()
        self._update_rehearsal()

    def _build_locale_labels(self) -> dict[str, str]:
        labels: dict[str, str] = {}
        for locale in self.voices_by_locale:
            name = LOCALE_NAMES.get(locale, locale)
            labels[f"{name}  ({locale})"] = locale
        return dict(sorted(labels.items()))

    # --- toolbar -----------------------------------------------------------
    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self, padding=(12, 10))
        bar.pack(fill="x")
        ttk.Label(bar, text="Script:").pack(side="left")
        self.script_name_var = tk.StringVar(value="untitled")
        ttk.Label(bar, textvariable=self.script_name_var, font=("TkDefaultFont", 13, "bold")).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(bar, text="Save", command=self.on_save).pack(side="right")
        ttk.Button(bar, text="Open", command=self.on_open).pack(side="right", padx=4)
        ttk.Button(bar, text="New", command=self.on_new).pack(side="right")
        ttk.Separator(self, orient="horizontal").pack(fill="x")

    # --- defaults bar ------------------------------------------------------
    def _build_defaults(self) -> None:
        bar = ttk.Frame(self, padding=(12, 8))
        bar.pack(fill="x")
        ttk.Label(bar, text="Defaults for new questions:", foreground="#888").pack(
            side="left", padx=(0, 8)
        )

        self.def_lang_var = tk.StringVar()
        def_lang = ttk.Combobox(
            bar,
            textvariable=self.def_lang_var,
            state="readonly",
            width=22,
            values=list(self.locale_labels.keys()),
        )
        def_lang.pack(side="left", padx=4)
        def_lang.bind("<<ComboboxSelected>>", lambda _e: self._refresh_def_voices())

        self.def_voice_var = tk.StringVar()
        self.def_voice_combo = ttk.Combobox(
            bar, textvariable=self.def_voice_var, state="readonly", width=20
        )
        self.def_voice_combo.pack(side="left", padx=4)

        self.def_mode_var = tk.StringVar(value="translate")
        ttk.Radiobutton(bar, text="Translate", value="translate", variable=self.def_mode_var).pack(
            side="left", padx=(8, 0)
        )
        ttk.Radiobutton(bar, text="As typed", value="as_typed", variable=self.def_mode_var).pack(
            side="left"
        )

        if self.locale_labels:
            default = next(
                (lbl for lbl, loc in self.locale_labels.items() if loc == "tr-TR"),
                next(iter(self.locale_labels)),
            )
            self.def_lang_var.set(default)
        self._refresh_def_voices()
        ttk.Separator(self, orient="horizontal").pack(fill="x")

    def _refresh_def_voices(self) -> None:
        locale = self.locale_labels.get(self.def_lang_var.get(), "")
        labels, mapping = voice_choices(self.voices_by_locale, locale)
        self._def_voice_map = mapping
        self.def_voice_combo["values"] = labels
        if labels:
            self.def_voice_combo.current(0)
        else:
            self.def_voice_var.set("")

    def _defaults(self) -> dict:
        return {
            "locale": self.locale_labels.get(self.def_lang_var.get(), ""),
            "voice": self._def_voice_map.get(self.def_voice_var.get(), ""),
            "mode": self.def_mode_var.get(),
        }

    # --- rehearsal panel (bottom) -----------------------------------------
    def _build_rehearsal(self) -> None:
        panel = ttk.LabelFrame(self, text="Rehearsal", padding=12)
        panel.pack(fill="x", side="bottom", padx=12, pady=(6, 12))

        self.rehearsal_count = ttk.Label(panel, text="", foreground="#888")
        self.rehearsal_count.pack(anchor="w")

        self.rehearsal_en = ttk.Label(
            panel, text="", font=("TkDefaultFont", 16), wraplength=780, justify="left"
        )
        self.rehearsal_en.pack(anchor="w", pady=(6, 2))
        self.rehearsal_tr = ttk.Label(
            panel,
            text="",
            font=("TkDefaultFont", 13),
            foreground="#888",
            wraplength=780,
            justify="left",
        )
        self.rehearsal_tr.pack(anchor="w", pady=(0, 8))

        controls = ttk.Frame(panel)
        controls.pack(fill="x")
        ttk.Button(controls, text="◀ Prev", command=self.on_prev).pack(side="left")
        ttk.Button(controls, text="▶ Play question", command=self.on_play).pack(side="left", padx=6)
        ttk.Button(controls, text="Played — next ▶", command=self.on_next).pack(side="left")
        ttk.Button(controls, text="↻ Restart rehearsal", command=self.on_restart).pack(side="right")

        self.rehearsal_status = ttk.Label(
            panel,
            text="Space plays the question; press Space again to advance to the next.",
            foreground="#888",
        )
        self.rehearsal_status.pack(anchor="w", pady=(8, 0))

    # --- generate bar ------------------------------------------------------
    def _build_generate_bar(self) -> None:
        bar = ttk.Frame(self, padding=(12, 8))
        bar.pack(fill="x", side="bottom")
        ttk.Separator(self, orient="horizontal").pack(fill="x", side="bottom")

        self.generate_btn = ttk.Button(
            bar, text="Generate audio for all questions", command=self.on_generate_all
        )
        self.generate_btn.pack(side="left")
        ttk.Button(bar, text="Open clips folder", command=lambda: _open_path(CLIPS_DIR)).pack(
            side="left", padx=6
        )
        self.gen_status = tk.StringVar(
            value="Ready."
            if EDGE_TTS_AVAILABLE
            else "edge-tts not installed — run:  uv pip install edge-tts"
        )
        ttk.Label(bar, textvariable=self.gen_status, foreground="#888").pack(side="left", padx=8)
        self.gen_progress = ttk.Progressbar(bar, mode="determinate", length=160)
        self.gen_progress.pack(side="right")

    # --- question list (middle) -------------------------------------------
    def _build_question_list(self) -> None:
        wrapper = ttk.Frame(self, padding=(12, 6))
        wrapper.pack(fill="both", expand=True)

        header = ttk.Frame(wrapper)
        header.pack(fill="x")
        self.questions_header = ttk.Label(
            header, text="Questions  (0)", font=("TkDefaultFont", 13, "bold")
        )
        self.questions_header.pack(side="left")
        ttk.Label(header, text="— this list is the sequence you will play", foreground="#888").pack(
            side="left", padx=8
        )

        tree_frame = ttk.Frame(wrapper)
        tree_frame.pack(fill="both", expand=True, pady=(6, 6))
        cols = ("num", "english", "spoken", "lang", "audio")
        self.tree = ttk.Treeview(
            tree_frame, columns=cols, show="headings", height=8, selectmode="browse"
        )
        self.tree.heading("num", text="#")
        self.tree.heading("english", text="Question (English)")
        self.tree.heading("spoken", text="Spoken text")
        self.tree.heading("lang", text="Language")
        self.tree.heading("audio", text="Audio")
        self.tree.column("num", width=42, anchor="center", stretch=False)
        self.tree.column("english", width=270, stretch=True)
        self.tree.column("spoken", width=270, stretch=True)
        self.tree.column("lang", width=84, anchor="center", stretch=False)
        self.tree.column("audio", width=70, anchor="center", stretch=False)
        self.tree.tag_configure("played", foreground="#8a8a8a")
        self.tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        scroll.pack(side="left", fill="y")
        self.tree.config(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Double-1>", self.on_edit)

        buttons = ttk.Frame(wrapper)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="+ Add question", command=self.on_add).pack(side="left")
        ttk.Button(buttons, text="Edit", command=self.on_edit).pack(side="left", padx=4)
        ttk.Button(buttons, text="Delete", command=self.on_delete).pack(side="left")
        ttk.Button(buttons, text="▼ Move down", command=lambda: self._move(1)).pack(side="right")
        ttk.Button(buttons, text="▲ Move up", command=lambda: self._move(-1)).pack(
            side="right", padx=4
        )

    # --- question model helpers -------------------------------------------
    def _index_of(self, qid: str) -> int:
        for i, q in enumerate(self.questions):
            if q["id"] == qid:
                return i
        return -1

    def _q_by_id(self, qid: str) -> dict | None:
        idx = self._index_of(qid)
        return self.questions[idx] if idx >= 0 else None

    def _current_q(self) -> dict | None:
        return self._q_by_id(self.current_id) if self.current_id else None

    def _selected_question(self) -> dict | None:
        sel = self.tree.selection()
        return self._q_by_id(sel[0]) if sel else None

    @staticmethod
    def _spoken_of(q: dict) -> str:
        return q["english"] if q["mode"] == "as_typed" else q.get("translation", "")

    def _refresh_tree(self) -> None:
        keep = self.current_id
        self.tree.delete(*self.tree.get_children())
        for i, q in enumerate(self.questions):
            spoken = self._spoken_of(q)
            if q["mode"] == "translate" and not spoken:
                spoken = "(translates on generate)"
            ready = bool(q.get("audio")) and (CLIPS_DIR / q["audio"]).exists()
            tags = ("played",) if q["id"] in self.played else ()
            self.tree.insert(
                "",
                "end",
                iid=q["id"],
                values=(i + 1, q["english"], spoken, q["locale"], "ready" if ready else "—"),
                tags=tags,
            )
        self.questions_header.config(text=f"Questions  ({len(self.questions)})")
        if keep and self.tree.exists(keep):
            self.tree.selection_set(keep)
            self.tree.see(keep)

    def _on_tree_select(self, _event: object = None) -> None:
        sel = self.tree.selection()
        if sel:
            self.current_id = sel[0]
            self._update_rehearsal()

    def _select_current(self) -> None:
        if self.current_id and self.tree.exists(self.current_id):
            self.tree.selection_set(self.current_id)
            self.tree.see(self.current_id)

    # --- add / edit / delete / move ---------------------------------------
    def on_add(self) -> None:
        dlg = QuestionDialog(
            self,
            self.voices_by_locale,
            self.locale_labels,
            self.api_keys,
            question=None,
            defaults=self._defaults(),
        )
        self.wait_window(dlg)
        if dlg.result:
            self.questions.append(dlg.result)
            if self.current_id is None:
                self.current_id = dlg.result["id"]
            self._refresh_tree()
            self._update_rehearsal()

    def on_edit(self, _event: object = None) -> None:
        q = self._selected_question()
        if not q:
            messagebox.showinfo("Edit", "Select a question first.")
            return
        before = (q["english"], q["translation"], q["voice"], q["mode"])
        dlg = QuestionDialog(
            self,
            self.voices_by_locale,
            self.locale_labels,
            self.api_keys,
            question=q,
            defaults=self._defaults(),
        )
        self.wait_window(dlg)
        if not dlg.result:
            return
        new = dlg.result
        after = (new["english"], new["translation"], new["voice"], new["mode"])
        if before != after and q.get("audio"):
            # the spoken content changed — drop the now-stale clip
            with contextlib.suppress(OSError):
                (CLIPS_DIR / q["audio"]).unlink()
            new["audio"] = ""
        self.questions[self._index_of(q["id"])] = new
        self._refresh_tree()
        self._update_rehearsal()

    def on_delete(self) -> None:
        q = self._selected_question()
        if not q:
            messagebox.showinfo("Delete", "Select a question first.")
            return
        if not messagebox.askyesno("Delete question", f"Delete:\n\n{q['english']}"):
            return
        if q.get("audio"):
            with contextlib.suppress(OSError):
                (CLIPS_DIR / q["audio"]).unlink()
        self.questions = [x for x in self.questions if x["id"] != q["id"]]
        self.played.discard(q["id"])
        if self.current_id == q["id"]:
            self.current_id = self.questions[0]["id"] if self.questions else None
        self._refresh_tree()
        self._update_rehearsal()

    def _move(self, delta: int) -> None:
        q = self._selected_question()
        if not q:
            return
        i = self._index_of(q["id"])
        j = i + delta
        if j < 0 or j >= len(self.questions):
            return
        self.questions[i], self.questions[j] = self.questions[j], self.questions[i]
        self._refresh_tree()
        self._update_rehearsal()

    # --- generate ----------------------------------------------------------
    def on_generate_all(self) -> None:
        if not EDGE_TTS_AVAILABLE:
            messagebox.showerror("edge-tts missing", "Install it first:\n\nuv pip install edge-tts")
            return
        if not self.questions:
            messagebox.showinfo("Generate", "Add some questions first.")
            return
        pending = [
            q for q in self.questions if not (q.get("audio") and (CLIPS_DIR / q["audio"]).exists())
        ]
        if not pending:
            self.gen_status.set("All questions already have audio.")
            return
        self.generate_btn.config(state="disabled")
        self.gen_progress.config(maximum=len(pending), value=0)
        self.gen_status.set(f"Generating 0 / {len(pending)}...")
        threading.Thread(target=self._generate_worker, args=(pending,), daemon=True).start()

    def _generate_worker(self, pending: list[dict]) -> None:
        errors: list[str] = []
        for done, q in enumerate(pending, 1):
            try:
                if q["mode"] == "translate" and not q.get("translation", "").strip():
                    language = LOCALE_NAMES.get(q["locale"], q["locale"])
                    q["translation"] = translate_text(q["english"], language, self.api_keys)
                spoken = self._spoken_of(q)
                if not spoken.strip():
                    raise RuntimeError("no text to speak")
                fname = f"{slugify(q['english'])}_{q['id']}.wav"
                old = q.get("audio", "")
                generate_clip(spoken, q["voice"], CLIPS_DIR, fname)
                q["audio"] = fname
                if old and old != fname:
                    with contextlib.suppress(OSError):
                        (CLIPS_DIR / old).unlink()
            except Exception as exc:
                errors.append(f"• {q['english'][:40]}: {exc}")
            self.after(0, self._generate_progress, done, len(pending))
        self.after(0, self._generate_done, errors)

    def _generate_progress(self, done: int, total: int) -> None:
        self.gen_progress.config(value=done)
        self.gen_status.set(f"Generating {done} / {total}...")
        self._refresh_tree()

    def _generate_done(self, errors: list[str]) -> None:
        self.generate_btn.config(state="normal")
        self._refresh_tree()
        self._update_rehearsal()
        if errors:
            self.gen_status.set(f"Done with {len(errors)} error(s).")
            messagebox.showwarning("Some clips failed", "\n".join(errors[:12]))
        else:
            self.gen_status.set("All audio generated.")

    # --- rehearsal ---------------------------------------------------------
    def _update_rehearsal(self) -> None:
        q = self._current_q()
        total = len(self.questions)
        if not q:
            self.rehearsal_count.config(text="")
            self.rehearsal_en.config(text="No questions yet — add some above.")
            self.rehearsal_tr.config(text="")
            return
        idx = self._index_of(q["id"])
        played = sum(1 for x in self.questions if x["id"] in self.played)
        self.rehearsal_count.config(text=f"Question {idx + 1} of {total}   ·   {played} played")
        self.rehearsal_en.config(text=q["english"])
        spoken = self._spoken_of(q)
        if q["mode"] == "translate" and not spoken:
            spoken = "(not translated yet — generate audio)"
        self.rehearsal_tr.config(text=spoken)

    def on_play(self) -> None:
        q = self._current_q()
        if not q:
            return
        audio = q.get("audio")
        if not audio or not (CLIPS_DIR / audio).exists():
            if messagebox.askyesno(
                "No audio",
                "This question has no audio yet. Generate audio for all now?",
            ):
                self.on_generate_all()
            return
        self._stop_playback()
        try:
            self.player_proc = subprocess.Popen(["afplay", str(CLIPS_DIR / audio)])
        except FileNotFoundError:
            messagebox.showerror(
                "afplay missing", "afplay is a macOS tool — this player needs macOS."
            )
            return
        self.played.add(q["id"])
        self._refresh_tree()
        self._update_rehearsal()
        self.rehearsal_status.config(text=f"Playing:  {q['english']}")

    def on_next(self) -> None:
        q = self._current_q()
        if not q:
            return
        self.played.add(q["id"])
        idx = self._index_of(q["id"])
        if idx < len(self.questions) - 1:
            self.current_id = self.questions[idx + 1]["id"]
            self._select_current()
            self._update_rehearsal()
            self.on_play()
        else:
            self._refresh_tree()
            self._update_rehearsal()
            self.rehearsal_status.config(text="End of script — rehearsal complete.")

    def on_prev(self) -> None:
        q = self._current_q()
        if not q:
            return
        idx = self._index_of(q["id"])
        if idx > 0:
            self.current_id = self.questions[idx - 1]["id"]
            self._select_current()
            self._update_rehearsal()

    def on_restart(self) -> None:
        self.played.clear()
        self.current_id = self.questions[0]["id"] if self.questions else None
        self._select_current()
        self._refresh_tree()
        self._update_rehearsal()
        self.rehearsal_status.config(text="Rehearsal restarted — start from question 1.")

    def _on_space(self, _event: object = None) -> str:
        q = self._current_q()
        if q:
            if q["id"] not in self.played:
                self.on_play()
            else:
                self.on_next()
        return "break"

    def _stop_playback(self) -> None:
        if self.player_proc and self.player_proc.poll() is None:
            self.player_proc.terminate()
        self.player_proc = None

    # --- script save / open / new -----------------------------------------
    def on_new(self) -> None:
        if self.questions and not messagebox.askyesno(
            "New script", "Discard the current script and start a new one?"
        ):
            return
        self.questions = []
        self.played.clear()
        self.current_id = None
        self.script_path = None
        self.script_name_var.set("untitled")
        self._refresh_tree()
        self._update_rehearsal()

    def on_save(self) -> None:
        if not self.questions:
            messagebox.showinfo("Save", "Nothing to save yet.")
            return
        path = self.script_path
        if path is None:
            chosen = filedialog.asksaveasfilename(
                title="Save test script",
                initialdir=str(SCRIPTS_DIR),
                initialfile="test_script.json",
                defaultextension=".json",
                filetypes=[("Test script", "*.json")],
            )
            if not chosen:
                return
            path = Path(chosen)
        data = {
            "name": path.stem,
            "saved": datetime.now().isoformat(timespec="seconds"),
            "defaults": self._defaults(),
            "questions": self.questions,
        }
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        self.script_path = path
        self.script_name_var.set(path.stem)
        self.gen_status.set(f"Saved script -> {path}")

    def on_open(self) -> None:
        chosen = filedialog.askopenfilename(
            title="Open test script",
            initialdir=str(SCRIPTS_DIR),
            filetypes=[("Test script", "*.json")],
        )
        if not chosen:
            return
        try:
            data = json.loads(Path(chosen).read_text(encoding="utf-8"))
            questions = data.get("questions", [])
            if not isinstance(questions, list):
                raise ValueError("file has no question list")
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            messagebox.showerror("Could not open", str(exc))
            return
        for q in questions:  # tolerate older/partial files
            q.setdefault("id", new_qid())
            q.setdefault("mode", "translate")
            q.setdefault("translation", "")
            q.setdefault("audio", "")
            q.setdefault("locale", "")
            q.setdefault("voice", "")
            q.setdefault("english", "")
        self.questions = questions
        self.played.clear()
        self.current_id = questions[0]["id"] if questions else None
        self.script_path = Path(chosen)
        self.script_name_var.set(self.script_path.stem)
        self._refresh_tree()
        self._update_rehearsal()
        self.gen_status.set(f"Opened {len(questions)} question(s).")

    def _on_close(self) -> None:
        self._stop_playback()
        self.destroy()


def main() -> None:
    VoxteraTestRunner().mainloop()


if __name__ == "__main__":
    main()
