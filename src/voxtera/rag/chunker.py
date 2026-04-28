"""Markdown-aware text chunker for RAG ingestion.

Splits text into overlapping chunks that respect document structure:
headings → fenced blocks → paragraphs → tables → FAQ pairs → lists →
sentences.  Uses tiktoken for accurate token counts matching the embedding
model.

Key features beyond naive splitting:
- **Heading context propagation**: each chunk is prefixed with its ancestor
  heading path so embeddings capture *what section* the text belongs to.
- **Fenced code-block preservation**: triple-backtick / tilde blocks are
  never split mid-block (prevents heading-regex misfires inside code).
- **Table-aware chunking**: markdown tables are kept atomic when small;
  large tables are split row-by-row with the header row repeated.
- **FAQ / Q&A pair detection**: consecutive ``Q: … / A: …`` paragraphs
  are merged so questions are never separated from their answers.
- **Blockquote preservation**: ``>``-prefixed blocks are kept as atomic
  units.
- **List-aware splitting**: markdown list items (``-``, ``*``, ``1.``) are
  kept as atomic units and never split mid-item.
- **Robust sentence splitting**: handles abbreviations (Dr., Mr., etc.),
  decimal numbers, and ellipses without false breaks.
- **Sentence-boundary overlap**: overlap between adjacent chunks ends at the
  last complete sentence boundary rather than cutting mid-sentence.
- **Adaptive chunk sizing** (opt-in): automatically shrinks target size for
  dense/numeric content and grows it for narrative prose.
- **Hash-based deduplication** (opt-in): removes exact-duplicate chunks.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import tiktoken

_ENCODING = tiktoken.get_encoding("cl100k_base")

# ---------------------------------------------------------------------------
# Heading detection
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"(?=^#{1,6}\s)", re.MULTILINE)
_HEADING_LINE_RE = re.compile(r"^(#{1,6})\s+(.+?)$", re.MULTILINE)

# ---------------------------------------------------------------------------
# Sentence splitting — robust against abbreviations, decimals, ellipses
#
# Multilingual: Voxtera serves tourism across languages.  The abbreviation
# set is loaded from abbreviations.json (one key per language) so new
# languages can be added without touching code.
# A language-agnostic heuristic in _ends_with_abbreviation() catches any
# remaining single-letter or all-caps abbreviations regardless of language.
# ---------------------------------------------------------------------------


def _load_abbreviations() -> frozenset[str]:
    """Load and flatten all abbreviations from the JSON data file."""
    path = Path(__file__).parent / "abbreviations.json"
    with path.open(encoding="utf-8") as f:
        data: dict[str, list[str]] = json.load(f)
    return frozenset(abbr for values in data.values() for abbr in values)


_ABBREVIATIONS = _load_abbreviations()

# Sentence-end: .!? followed by whitespace and an uppercase letter or number,
# but NOT preceded by a known abbreviation.
_SENTENCE_END_RE = re.compile(
    r"(?<=[.!?])"  # look-behind for sentence-ending punctuation
    r"(?<![.][.])"  # exclude ellipsis (..)
    r"\s+"  # one or more whitespace chars
    r"(?=[A-Z0-9\"'¿¡(])"  # look-ahead: new sentence starts with upper/digit/quote
)

# ---------------------------------------------------------------------------
# List detection
# ---------------------------------------------------------------------------

_LIST_ITEM_RE = re.compile(
    r"^(?:[-*+]|\d+[.)]) ",  # unordered (-, *, +) or ordered (1. 1))
    re.MULTILINE,
)

# ---------------------------------------------------------------------------
# Fenced code-block detection
# ---------------------------------------------------------------------------

_FENCE_OPEN_RE = re.compile(r"^(`{3,}|~{3,})")

# ---------------------------------------------------------------------------
# Table detection
# ---------------------------------------------------------------------------

_TABLE_SEP_RE = re.compile(r"^\|?[\s\-:|]+\|")

# ---------------------------------------------------------------------------
# FAQ / Q&A detection
# ---------------------------------------------------------------------------

_FAQ_Q_RE = re.compile(
    r"^(?:\*{0,2}Q(?:uestion)?(?:\s*\d*)?\s*[:.)\]\-]\*{0,2})",
    re.IGNORECASE,
)
_FAQ_A_RE = re.compile(
    r"^(?:\*{0,2}A(?:nswer)?(?:\s*\d*)?\s*[:.)\]\-]\*{0,2})",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Content density (for adaptive sizing)
# ---------------------------------------------------------------------------

_NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)*\b")


@dataclass(frozen=True)
class Chunk:
    """One chunk of text with its pre-computed token count."""

    text: str
    token_count: int


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def _token_len(text: str) -> int:
    """Return the number of tokens in *text*."""
    return len(_ENCODING.encode(text))


# ---------------------------------------------------------------------------
# Splitting hierarchy
# ---------------------------------------------------------------------------


def _split_heading_sections(text: str) -> list[str]:
    """Split *text* on markdown heading boundaries.

    Each heading starts a new section.  Non-heading text before the first
    heading is returned as its own section.
    """
    parts = _HEADING_RE.split(text)
    return [p for p in parts if p.strip()]


def _split_paragraphs(text: str) -> list[str]:
    """Split *text* on blank lines (two consecutive newlines)."""
    parts = re.split(r"\n\s*\n", text)
    return [p.strip() for p in parts if p.strip()]


def _split_list_items(text: str) -> list[str]:
    """Split a paragraph into individual list items if it is a list.

    If the paragraph doesn't look like a list, returns the paragraph as-is
    in a single-element list.
    """
    lines = text.split("\n")
    # Only treat as a list if ≥ 2 lines start with list markers.
    list_lines = [ln for ln in lines if _LIST_ITEM_RE.match(ln.lstrip())]
    if len(list_lines) < 2:
        return [text]

    items: list[str] = []
    current: list[str] = []
    for line in lines:
        if _LIST_ITEM_RE.match(line.lstrip()) and current:
            items.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        items.append("\n".join(current))
    return [item.strip() for item in items if item.strip()]


def _split_sentences(text: str) -> list[str]:
    """Split *text* on sentence boundaries, handling abbreviations.

    Avoids splitting after common abbreviations (Dr., Mr., etc.) and
    decimal numbers (4.5, 3.14).
    """
    # First pass: regex split on sentence boundaries.
    candidates = _SENTENCE_END_RE.split(text)
    if len(candidates) <= 1:
        return [text.strip()] if text.strip() else []

    # Second pass: re-join false splits caused by abbreviations.
    merged: list[str] = []
    for candidate in candidates:
        stripped = candidate.strip()
        if not stripped:
            continue
        if merged and _ends_with_abbreviation(merged[-1]):
            merged[-1] = merged[-1] + " " + stripped
        else:
            merged.append(stripped)
    return merged


def _ends_with_abbreviation(text: str) -> bool:
    """Return True if *text* likely ends with an abbreviation.

    Uses three strategies so it works across languages:
    1. Known abbreviation list (multilingual).
    2. Single-letter word + period → almost always an initial ("J.", "M.").
    3. All-caps word ≤ 4 chars + period → acronym ("U.S.", "E.U.", "S.A.").
    """
    words = text.split()
    if not words:
        return False
    raw_last = words[-1]
    if not raw_last.endswith("."):
        return False
    stripped = raw_last.rstrip(".").lower()

    # Strategy 1: known abbreviation.
    if stripped in _ABBREVIATIONS:
        return True
    # Strategy 2: single letter + period (initial in any language).
    if len(stripped) == 1 and stripped.isalpha():
        return True
    # Strategy 3: short all-caps word (acronym / abbreviation).
    bare = raw_last.rstrip(".")
    return len(bare) <= 4 and bare.isupper() and bare.isalpha()


def _split_by_tokens(text: str, max_tokens: int) -> list[str]:
    """Last-resort split: break *text* into pieces of at most *max_tokens*."""
    tokens = _ENCODING.encode(text)
    pieces: list[str] = []
    for i in range(0, len(tokens), max_tokens):
        pieces.append(_ENCODING.decode(tokens[i : i + max_tokens]))
    return pieces


def _is_heading(text: str) -> bool:
    """Return True if *text* starts with a markdown heading."""
    return bool(re.match(r"^#{1,6}\s", text.lstrip()))


def _heading_level(text: str) -> int:
    """Return the heading level (1-6) or 0 if not a heading."""
    m = re.match(r"^(#{1,6})\s", text.lstrip())
    return len(m.group(1)) if m else 0


# ---------------------------------------------------------------------------
# Heading context tracker
# ---------------------------------------------------------------------------


class _HeadingStack:
    """Track the current heading ancestry path (e.g. ``# Hotel > ## Rooms``)."""

    def __init__(self) -> None:
        self._stack: list[tuple[int, str]] = []

    def push(self, text: str) -> None:
        """Register a heading.  Pops deeper/equal headings."""
        level = _heading_level(text)
        if level == 0:
            return
        m = _HEADING_LINE_RE.match(text.lstrip())
        title = m.group(2).strip() if m else text.strip()
        # Pop headings at same or deeper level.
        while self._stack and self._stack[-1][0] >= level:
            self._stack.pop()
        self._stack.append((level, title))

    def context_prefix(self) -> str:
        """Return the heading path as a compact prefix string.

        Example: ``# Hotel > ## Restaurant > ### Breakfast``
        """
        if not self._stack:
            return ""
        return " > ".join(f"{'#' * lvl} {title}" for lvl, title in self._stack)


# ---------------------------------------------------------------------------
# Fenced code-block splitting
# ---------------------------------------------------------------------------


def _split_fenced_and_rest(text: str) -> list[tuple[str, bool]]:
    """Split *text* into ``(segment, is_fenced)`` tuples.

    Fenced code blocks (``` or ~~~) are returned as atomic segments so that
    later splitting stages (heading, paragraph) never see their contents.
    """
    lines = text.split("\n")
    segments: list[tuple[str, bool]] = []
    current: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0

    for line in lines:
        stripped = line.strip()
        if not in_fence:
            m = _FENCE_OPEN_RE.match(stripped)
            if m:
                # Flush non-fenced accumulator.
                if current:
                    segments.append(("\n".join(current), False))
                    current = []
                fence_char = m.group(1)[0]
                fence_len = len(m.group(1))
                current = [line]
                in_fence = True
            else:
                current.append(line)
        else:
            current.append(line)
            # Closing fence: same char, at least as long, nothing else.
            if len(stripped) >= fence_len and all(c == fence_char for c in stripped):
                segments.append(("\n".join(current), True))
                current = []
                in_fence = False

    if current:
        segments.append(("\n".join(current), in_fence))
    return segments


# ---------------------------------------------------------------------------
# Table helpers
# ---------------------------------------------------------------------------


def _is_table(text: str) -> bool:
    """Return True if *text* looks like a markdown table."""
    lines = [ln for ln in text.strip().split("\n") if ln.strip()]
    if len(lines) < 2:
        return False
    pipe_lines = sum(1 for ln in lines if "|" in ln)
    return pipe_lines >= len(lines) * 0.8


def _split_table(text: str, max_tokens: int) -> list[str]:
    """Split a markdown table, repeating the header in every chunk.

    If the table fits within *max_tokens*, returns it as-is.
    """
    if _token_len(text) <= max_tokens:
        return [text]

    lines = text.strip().split("\n")

    # Identify header + separator rows.
    header_lines: list[str] = []
    data_lines: list[str] = []
    separator_found = False

    for line in lines:
        if not separator_found:
            header_lines.append(line)
            if _TABLE_SEP_RE.match(line.strip()):
                separator_found = True
        else:
            data_lines.append(line)

    if not separator_found:
        # Not a proper table — fall back to returning as-is.
        return [text]

    header = "\n".join(header_lines)
    header_tokens = _token_len(header)

    chunks: list[str] = []
    current_rows: list[str] = []
    current_tokens = header_tokens

    for row in data_lines:
        row_tokens = _token_len(row)
        if current_tokens + row_tokens > max_tokens and current_rows:
            chunks.append(header + "\n" + "\n".join(current_rows))
            current_rows = []
            current_tokens = header_tokens
        current_rows.append(row)
        current_tokens += row_tokens

    if current_rows:
        chunks.append(header + "\n" + "\n".join(current_rows))

    return chunks if chunks else [text]


# ---------------------------------------------------------------------------
# FAQ pair merging
# ---------------------------------------------------------------------------


def _merge_faq_pairs(paragraphs: list[str]) -> list[str]:
    """Merge consecutive Q + A paragraphs so they stay in one chunk."""
    merged: list[str] = []
    i = 0
    while i < len(paragraphs):
        p = paragraphs[i].strip()
        if _FAQ_Q_RE.match(p) and i + 1 < len(paragraphs):
            next_p = paragraphs[i + 1].strip()
            if _FAQ_A_RE.match(next_p):
                merged.append(p + "\n\n" + next_p)
                i += 2
                continue
        merged.append(paragraphs[i])
        i += 1
    return merged


# ---------------------------------------------------------------------------
# Blockquote detection
# ---------------------------------------------------------------------------


def _is_blockquote(text: str) -> bool:
    """Return True if every non-empty line starts with ``>``."""
    lines = [ln for ln in text.split("\n") if ln.strip()]
    return bool(lines) and all(ln.lstrip().startswith(">") for ln in lines)


# ---------------------------------------------------------------------------
# Content density (adaptive sizing)
# ---------------------------------------------------------------------------


def _content_density(text: str) -> float:
    """Estimate content density on a 0.0 (narrative) – 1.0 (very dense) scale.

    Dense content has many numbers, short words, or pipe characters (tables).
    """
    words = text.split()
    if not words:
        return 0.5
    numbers = len(_NUMBER_RE.findall(text))
    number_ratio = numbers / len(words)
    avg_word_len = sum(len(w) for w in words) / len(words)
    short_word_factor = max(0.0, 1.0 - avg_word_len / 8.0)
    pipe_ratio = text.count("|") / max(len(text), 1)
    return min(1.0, number_ratio * 2 + short_word_factor * 0.3 + pipe_ratio * 5)


# ---------------------------------------------------------------------------
# Atomic splits
# ---------------------------------------------------------------------------


def _atomic_splits(text: str, max_tokens: int) -> list[str]:
    """Break *text* into the smallest logical pieces.

    Strategy: fenced blocks → headings → paragraphs → (tables | FAQ | blockquotes
    | lists) → sentences → token-level.
    Every returned piece is guaranteed to be ≤ *max_tokens*.
    """
    pieces: list[str] = []

    def _append(t: str) -> None:
        if _token_len(t) <= max_tokens:
            pieces.append(t)
        else:
            pieces.extend(_split_by_tokens(t, max_tokens))

    for segment_text, is_fenced in _split_fenced_and_rest(text):
        if is_fenced:
            _append(segment_text)
            continue
        for section in _split_heading_sections(segment_text):
            paragraphs = _split_paragraphs(section)
            paragraphs = _merge_faq_pairs(paragraphs)
            for para in paragraphs:
                if _FAQ_Q_RE.match(para.strip()):
                    # Merged FAQ pair — keep atomic.
                    _append(para)
                elif _is_table(para):
                    for table_chunk in _split_table(para, max_tokens):
                        _append(table_chunk)
                elif _is_blockquote(para):
                    _append(para)
                else:
                    for item in _split_list_items(para):
                        for sentence in _split_sentences(item):
                            _append(sentence)
    return pieces


# ---------------------------------------------------------------------------
# Sentence-boundary overlap
# ---------------------------------------------------------------------------


def _sentence_boundary_overlap(text: str, tokens_budget: int) -> str:
    """Return the tail sentences of *text* that fit within *tokens_budget*.

    Unlike raw-token overlap, this ensures the overlap starts and ends at
    a sentence boundary for cleaner context.  Falls back to raw token tail
    if no sentence boundary fits.
    """
    if tokens_budget <= 0:
        return ""
    sentences = _split_sentences(text)
    if not sentences:
        return ""

    # Walk backwards through sentences, accumulating until budget is reached.
    overlap_parts: list[str] = []
    total = 0
    for sent in reversed(sentences):
        sent_tokens = _token_len(sent)
        if total + sent_tokens > tokens_budget:
            break
        overlap_parts.append(sent)
        total += sent_tokens

    if overlap_parts:
        overlap_parts.reverse()
        return " ".join(overlap_parts)

    # Fallback: no single sentence fits — use raw token tail.
    tokens = _ENCODING.encode(text)
    tail_tokens = tokens[-tokens_budget:]
    return _ENCODING.decode(tail_tokens)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def chunk_text(
    text: str,
    *,
    target_tokens: int = 400,
    max_tokens: int = 600,
    overlap_tokens: int = 40,
    adaptive: bool = False,
    deduplicate: bool = False,
) -> list[Chunk]:
    """Split *text* into overlapping chunks respecting markdown structure.

    Each chunk is prefixed with its ancestor heading path (e.g.
    ``# Hotel > ## Restaurant``) so the embedding captures section context.

    Parameters
    ----------
    text:
        The full document text.
    target_tokens:
        Ideal chunk size in tokens.
    max_tokens:
        Hard upper limit per chunk.
    overlap_tokens:
        Approximate number of tokens carried over from the previous chunk.
    adaptive:
        When True, automatically adjust *target_tokens* based on content
        density — smaller for number-heavy/tabular content, larger for
        narrative prose.
    deduplicate:
        When True, remove exact-duplicate chunks (normalised whitespace).

    Returns
    -------
    list[Chunk]
        Ordered list of chunks.  Empty input yields ``[]``.
    """
    if not text or not text.strip():
        return []

    if adaptive:
        density = _content_density(text)
        # Dense: 60 % of target, narrative: 120 % of target.
        factor = 1.2 - 0.6 * density
        target_tokens = max(50, min(int(target_tokens * factor), max_tokens - 1))

    splits = _atomic_splits(text, target_tokens)
    if not splits:
        return []

    heading_stack = _HeadingStack()
    chunks: list[Chunk] = []
    current_parts: list[str] = []
    current_tokens = 0

    def _flush() -> None:
        """Emit the current accumulator as a chunk with heading context."""
        if not current_parts:
            return
        chunk_body = " ".join(current_parts)

        # Prepend heading context prefix.
        ctx = heading_stack.context_prefix()
        if ctx:
            chunk_body = ctx + "\n" + chunk_body

        # Prepend sentence-boundary overlap from the previous chunk.
        if chunks and overlap_tokens > 0:
            content_tokens = _token_len(chunk_body)
            budget = max(0, max_tokens - content_tokens - 1)
            effective_overlap = min(overlap_tokens, budget)
            if effective_overlap > 0:
                overlap = _sentence_boundary_overlap(chunks[-1].text, effective_overlap)
                if overlap:
                    chunk_body = overlap + " " + chunk_body

        tcount = _token_len(chunk_body)
        chunks.append(Chunk(text=chunk_body, token_count=tcount))

    for piece in splits:
        piece_tokens = _token_len(piece)

        # Track heading ancestry.
        if _is_heading(piece):
            # Only flush on a heading when the current chunk already has
            # meaningful content.  This lets small adjacent sections merge
            # into one chunk so the embedding model gets enough context.
            # Without this, a 40-token section like "## Pool & Thermal Area"
            # would become its own chunk and score poorly for queries.
            _MIN_FLUSH = target_tokens // 3  # noqa: N806
            if current_parts and current_tokens >= _MIN_FLUSH:
                _flush()
                current_parts = []
                current_tokens = 0
            heading_stack.push(piece)

        # Would adding this piece exceed the target?
        if current_tokens + piece_tokens > target_tokens and current_parts:
            _flush()
            current_parts = []
            current_tokens = 0

        current_parts.append(piece)
        current_tokens += piece_tokens

    _flush()

    if deduplicate:
        seen: set[str] = set()
        unique: list[Chunk] = []
        for chunk in chunks:
            normalized = " ".join(chunk.text.split())
            if normalized not in seen:
                seen.add(normalized)
                unique.append(chunk)
        return unique

    return chunks
