#!/usr/bin/env python3
"""Generate restaurant menu PDFs from the Kempinski menu markdown.

Reads kempinski-hotel/{en,tr}/kempinski_ciragan_menu_<id>.md and writes a clean,
multi-page PDF per restaurant/language to assets/menus/{en,tr}/<id>.pdf — the
files the WhatsApp bot sends when a guest accepts a menu offer.

Uses DejaVuSans (full Turkish glyph coverage: ç ğ ı ö ş ü İ) so Turkish menus
render correctly. Re-run whenever a menu .md changes.

    python scripts/build_menu_pdfs.py
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

_REPO = Path(__file__).resolve().parent.parent
_DEJAVU = "/usr/share/fonts/truetype/dejavu"
_HOTEL = "Çırağan Palace Kempinski Istanbul"

# Pretty display names per restaurant id (filename slug).
_NAMES = {
    "tugra": "Tuğra",
    "ruya": "Ruya İstanbul",
    "gazebo": "Gazebo",
    "bellini": "Bellini",
    "bosphorus_grill": "Bosphorus Grill",
}


def _register_fonts() -> tuple[str, str]:
    pdfmetrics.registerFont(TTFont("DejaVu", f"{_DEJAVU}/DejaVuSans.ttf"))
    pdfmetrics.registerFont(TTFont("DejaVu-Bold", f"{_DEJAVU}/DejaVuSans-Bold.ttf"))
    return "DejaVu", "DejaVu-Bold"


def _styles(base: str, bold: str):
    ss = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("mTitle", parent=ss["Title"], fontName=bold, fontSize=20,
                                 leading=24, alignment=TA_CENTER, spaceAfter=2),
        "sub": ParagraphStyle("mSub", parent=ss["Normal"], fontName=base, fontSize=10,
                              leading=13, alignment=TA_CENTER, textColor="#666666", spaceAfter=14),
        "h2": ParagraphStyle("mH2", parent=ss["Heading2"], fontName=bold, fontSize=14,
                             leading=18, spaceBefore=12, spaceAfter=4, textColor="#5a4a2a"),
        "h3": ParagraphStyle("mH3", parent=ss["Heading3"], fontName=bold, fontSize=11.5,
                             leading=15, spaceBefore=8, spaceAfter=2),
        "body": ParagraphStyle("mBody", parent=ss["Normal"], fontName=base, fontSize=10,
                               leading=14, spaceAfter=3),
        "bullet": ParagraphStyle("mBul", parent=ss["Normal"], fontName=base, fontSize=10,
                                 leading=14, leftIndent=10, spaceAfter=2),
    }


def _inline(text: str) -> str:
    """Escape XML, then re-enable **bold** as reportlab <b> markup."""
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    return text


def _md_to_flowables(md: str, styles) -> list:
    # Strip a leading YAML frontmatter block (--- ... ---).
    if md.startswith("---"):
        end = md.find("\n---", 3)
        if end != -1:
            md = md[end + 4 :]
    flow: list = []
    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("### "):
            flow.append(Paragraph(_inline(line[4:]), styles["h3"]))
        elif line.startswith("## "):
            flow.append(Paragraph(_inline(line[3:]), styles["h2"]))
        elif line.startswith("# "):
            continue  # the doc title is replaced by our header
        elif line.lstrip().startswith(("- ", "* ")):
            flow.append(Paragraph("• " + _inline(line.lstrip()[2:]), styles["bullet"]))
        else:
            flow.append(Paragraph(_inline(line), styles["body"]))
    return flow


def build_pdf(md_path: Path, out_path: Path, restaurant: str, base: str, styles) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(out_path), pagesize=A4,
        topMargin=18 * mm, bottomMargin=16 * mm, leftMargin=18 * mm, rightMargin=18 * mm,
        title=f"{restaurant} — Menu", author=_HOTEL,
    )
    story = [
        Paragraph(html.escape(restaurant), styles["title"]),
        Paragraph(html.escape(_HOTEL), styles["sub"]),
        Spacer(1, 4),
    ]
    story += _md_to_flowables(md_path.read_text(encoding="utf-8"), styles)
    doc.build(story)


def main() -> None:
    base, bold = _register_fonts()
    styles = _styles(base, bold)
    count = 0
    for lang in ("en", "tr"):
        src_dir = _REPO / "kempinski-hotel" / lang
        for md in sorted(src_dir.glob("kempinski_ciragan_menu_*.md")):
            slug = md.stem.replace("kempinski_ciragan_menu_", "").removesuffix("_tr")
            name = _NAMES.get(slug, slug.replace("_", " ").title())
            out = _REPO / "assets" / "menus" / lang / f"{slug}.pdf"
            build_pdf(md, out, name, base, styles)
            print(f"  {lang}/{slug}.pdf  ←  {md.name}")
            count += 1
    print(f"Built {count} menu PDF(s) under assets/menus/")


if __name__ == "__main__":
    main()
