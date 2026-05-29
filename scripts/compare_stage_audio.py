#!/usr/bin/env python3
"""Compare per-stage mic recordings to find which pipeline stage breaks audio.

Run a call with ``STAGE_AUDIO_DEBUG=true`` to produce, in the call's log
folder, one WAV per pre-gate stage:

    input_raw.wav        what the browser sent (stage 0)
    stage_1_pre_emphasis.wav
    stage_2_rnnoise.wav        (only if RNNOISE_ENABLED)
    stage_3_leakage_guard.wav
    stage_4_audio_monitor.wav

These stages all pass every frame through (the leakage guard zeroes frames
rather than dropping them), so the WAVs are the same length and overlay on
the same clock. This script loads them in order, measures how much audio each
stage blanks to silence, and reports the FIRST stage that introduces a gap the
previous stage did not — i.e. the processor that breaks your sound. It also
writes a stacked waveform PNG for a visual check.

Usage:
    python scripts/compare_stage_audio.py logs/calls/<session_id>
    python scripts/compare_stage_audio.py logs/calls/<session_id> --out break.png
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

# Stage filenames in pipeline order. Missing files are skipped (e.g. rnnoise
# off), so the comparison still works with whatever subset is present.
STAGE_ORDER = [
    ("0_input_raw", "input_raw.wav"),
    ("1_pre_emphasis", "stage_1_pre_emphasis.wav"),
    ("2_rnnoise", "stage_2_rnnoise.wav"),
    ("3_leakage_guard", "stage_3_leakage_guard.wav"),
    ("4_audio_monitor", "stage_4_audio_monitor.wav"),
]

SR = 16000
GAP_MIN_MS = 300  # contiguous silence >= this counts as a "break"
SPEECH_RMS = 100.0  # int16 RMS above this in stage 0 = real speech was present


def load(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)


def zero_gaps(d: np.ndarray, min_samples: int) -> list[tuple[int, int]]:
    """Return [start, end) sample ranges of contiguous zeros >= min_samples."""
    z = (d == 0).astype(np.int8)
    df = np.diff(z)
    st = np.where(df == 1)[0] + 1
    en = np.where(df == -1)[0] + 1
    if z.size and z[0] == 1:
        st = np.r_[0, st]
    if z.size and z[-1] == 1:
        en = np.r_[en, len(z)]
    return [(int(a), int(b)) for a, b in zip(st, en, strict=False) if b - a >= min_samples]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("call_dir", type=Path, help="logs/calls/<session_id>")
    ap.add_argument("--out", type=Path, default=None, help="PNG output path")
    args = ap.parse_args()

    call_dir: Path = args.call_dir
    if not call_dir.is_dir():
        print(f"error: not a directory: {call_dir}", file=sys.stderr)
        return 2

    stages: list[tuple[str, np.ndarray]] = []
    for label, fname in STAGE_ORDER:
        p = call_dir / fname
        if p.exists():
            stages.append((label, load(p)))
    if len(stages) < 2:
        print(
            "error: need at least 2 stage WAVs to compare. Found: "
            f"{[lbl for lbl, _ in stages]}. Did you run with STAGE_AUDIO_DEBUG=true?",
            file=sys.stderr,
        )
        return 2

    # Align on the shortest length so per-sample comparison is valid.
    n = min(len(d) for _, d in stages)
    stages = [(lbl, d[:n]) for lbl, d in stages]
    ref = stages[0][1]  # input_raw — ground truth for where speech actually was

    print(f"Comparing {len(stages)} stages over {n / SR:.1f}s of audio\n")
    print(f"{'stage':<20} {'blanked(s)':>11} {'over-speech(s)':>15} {'#gaps':>6}")
    print("-" * 56)

    prev_blanked = 0.0
    culprit = None
    for label, d in stages:
        gaps = zero_gaps(d, int(GAP_MIN_MS / 1000 * SR))
        blanked = sum(b - a for a, b in gaps) / SR
        over_speech = 0.0
        for a, b in gaps:
            if np.sqrt(np.mean(ref[a:b] ** 2)) > SPEECH_RMS:
                over_speech += (b - a) / SR
        print(f"{label:<20} {blanked:>11.1f} {over_speech:>15.1f} {len(gaps):>6}")
        # First stage that adds >0.5s of new blanking over its predecessor.
        if culprit is None and blanked - prev_blanked > 0.5:
            culprit = label
        prev_blanked = blanked

    print()
    if culprit:
        print(f">>> BREAK INTRODUCED AT STAGE: {culprit}")
        print("    Audio is intact up to the previous stage, then this stage blanks it.")
    else:
        print(">>> No single stage stands out — blanking is uniform or absent.")

    # Visual: stacked envelopes.
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        def env(d: np.ndarray, win: int = 400) -> np.ndarray:
            m = len(d) // win
            return np.max(np.abs(d[: m * win].reshape(m, win)), axis=1)

        fig, axes = plt.subplots(len(stages), 1, figsize=(15, 2.2 * len(stages)), sharex=True)
        if len(stages) == 1:
            axes = [axes]
        te = np.arange(len(env(ref))) * 400 / SR
        for ax, (label, d) in zip(axes, stages, strict=False):
            e = env(d)
            ax.fill_between(te[: len(e)], e, -e, lw=0, color="#577590")
            for a, b in zero_gaps(d, int(GAP_MIN_MS / 1000 * SR)):
                over = np.sqrt(np.mean(ref[a:b] ** 2)) > SPEECH_RMS
                ax.axvspan(a / SR, b / SR, color="#e63946" if over else "#f4a261", alpha=0.5)
            ax.set_title(label, loc="left", fontsize=11)
            ax.set_yticks([])
        axes[-1].set_xlabel("time (s)")
        fig.suptitle("Per-stage mic audio — red = blanked over speech", fontweight="bold")
        plt.tight_layout()
        out = args.out or (call_dir / "stage_compare.png")
        plt.savefig(out, dpi=110, bbox_inches="tight")
        print(f"\nwaveform written: {out}")
    except ImportError:
        print("\n(matplotlib not installed — skipped PNG; numbers above are the answer)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
