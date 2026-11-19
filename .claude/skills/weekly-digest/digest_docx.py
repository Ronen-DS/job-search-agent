# Turns a weekly digest (digests/<date>.md) into a Word file
# (digests/<date>.docx) with python-docx.
#
# - "# " is the title, "## " section headings (Heading 1), "### " job
#   headings (Heading 2), "- " bullets, other lines plain paragraphs.
# - **bold** stays bold; every http(s) link becomes a clickable hyperlink.
# - Hebrew: a paragraph that starts with Hebrew is set right-to-left
#   (right-aligned, bullets on the right). Inside any paragraph, Hebrew
#   pieces are marked as right-to-left text, so mixed Hebrew/English lines
#   (e.g. "Example Labs: מדען/ית נתונים") display in the right order.
#
# The .docx is made only from a digest that passes the link check
# (.claude/hooks/check_digest.py), the same check as the digest hook.
#
# make_digest.py calls save_docx() after saving the .md. To (re)make one by hand:
#   python .claude/skills/weekly-digest/digest_docx.py digests/2026-11-18.md

import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.opc.constants import RELATIONSHIP_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / ".claude" / "hooks"))
from check_digest import DIGEST_NAME, validate  # noqa: E402  (same check as the hook)

FONT = "Arial"  # has Hebrew letters on every Windows / Mac / Office install
HEBREW_LETTER = re.compile(r"[֐-׿יִ-ﭏ]")
# A stretch of Hebrew: Hebrew letters plus the spaces, digits and
# punctuation between them (e.g. 'מדען/ית נתונים בכיר/ה', 'מש"א')
HEBREW_PIECE = re.compile(r"[֐-׿יִ-ﭏ](?:[֐-׿יִ-ﭏ\s\d\"'׳״/.,\-()]*[֐-׿יִ-ﭏ])?")
LINK = re.compile(r"https?://[^\s)>\]]+")
BOLD = re.compile(r"\*\*(.+?)\*\*")
LINK_COLOR = "0563C1"  # Word's usual hyperlink blue


# ---------------------------------------------------------------------------
# Low-level helpers (python-docx has no direct API for these)
# ---------------------------------------------------------------------------
def starts_with_hebrew(text):
    """True if the first letter in text (Hebrew or Latin) is Hebrew."""
    first = re.search(r"[A-Za-z֐-׿יִ-ﭏ]", text)
    return bool(first and HEBREW_LETTER.match(first.group(0)))


def set_rtl_paragraph(paragraph):
    """Right-to-left paragraph: <w:bidi/> plus right alignment."""
    p_pr = paragraph._p.get_or_add_pPr()
    p_pr.append(OxmlElement("w:bidi"))
    paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT


def style_run_properties(r_pr, rtl, bold=False):
    """Fonts (Hebrew uses the 'complex script' font slot), bold, and the
    right-to-left mark for Hebrew pieces."""
    fonts = OxmlElement("w:rFonts")
    for slot in ("w:ascii", "w:hAnsi", "w:cs"):
        fonts.set(qn(slot), FONT)
    r_pr.append(fonts)
    if bold:
        r_pr.append(OxmlElement("w:b"))
        r_pr.append(OxmlElement("w:bCs"))  # bold for Hebrew text too
    if rtl:
        r_pr.append(OxmlElement("w:rtl"))
        lang = OxmlElement("w:lang")
        lang.set(qn("w:bidi"), "he-IL")
        r_pr.append(lang)


def add_text(paragraph, text, bold=False):
    """Add text as runs, marking each Hebrew piece right-to-left."""
    position = 0
    for match in HEBREW_PIECE.finditer(text):
        if match.start() > position:
            run = paragraph.add_run(text[position:match.start()])
            style_run_properties(run._r.get_or_add_rPr(), rtl=False, bold=bold)
        run = paragraph.add_run(match.group(0))
        style_run_properties(run._r.get_or_add_rPr(), rtl=True, bold=bold)
        position = match.end()
    if position < len(text):
        run = paragraph.add_run(text[position:])
        style_run_properties(run._r.get_or_add_rPr(), rtl=False, bold=bold)


def add_hyperlink(paragraph, url):
    """A clickable link (blue, underlined) showing the URL itself."""
    r_id = paragraph.part.relate_to(url, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), r_id)
    run = OxmlElement("w:r")
    r_pr = OxmlElement("w:rPr")
    style_run_properties(r_pr, rtl=False)
    color = OxmlElement("w:color")
    color.set(qn("w:val"), LINK_COLOR)
    r_pr.append(color)
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    r_pr.append(underline)
    run.append(r_pr)
    text = OxmlElement("w:t")
    text.text = url
    text.set(qn("xml:space"), "preserve")
    run.append(text)
    link.append(run)
    paragraph._p.append(link)


def add_inline(paragraph, text):
    """Text with **bold** pieces and links."""
    for piece_number, piece in enumerate(BOLD.split(text)):
        bold = piece_number % 2 == 1  # split() puts the bold parts at odd places
        position = 0
        for match in LINK.finditer(piece):
            url = match.group(0).rstrip(".,;")
            end = match.start() + len(url)
            add_text(paragraph, piece[position:match.start()], bold)
            add_hyperlink(paragraph, url)
            position = end
        add_text(paragraph, piece[position:], bold)


# ---------------------------------------------------------------------------
# Markdown -> docx
# ---------------------------------------------------------------------------
def markdown_to_docx(md_text):
    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = FONT
    normal.font.size = Pt(11)

    styles = [("### ", "Heading 2"), ("## ", "Heading 1"), ("# ", "Title"), ("- ", "List Bullet")]
    for line in md_text.splitlines():
        if not line.strip():
            continue
        style, text = "Normal", line.strip()
        for prefix, style_name in styles:
            if line.startswith(prefix):
                style, text = style_name, line[len(prefix):].strip()
                break
        paragraph = doc.add_paragraph(style=style)
        if starts_with_hebrew(BOLD.sub(r"\1", text)):
            set_rtl_paragraph(paragraph)
        add_inline(paragraph, text)
        if style.startswith("Heading") or style == "Title":
            for run in paragraph.runs:
                run.font.color.rgb = RGBColor(0x1F, 0x3A, 0x5F)
    return doc


def save_docx(md_path):
    """Make <digest>.docx next to the .md. Refuses a digest that fails the
    link check. Returns the .docx path."""
    md_path = Path(md_path)
    match = DIGEST_NAME.search(str(md_path))
    if not match:
        raise ValueError(f"expected a path like digests/YYYY-MM-DD.md, got {md_path}")
    md_text = md_path.read_text(encoding="utf-8")
    problems = validate(md_text, match.group(1))
    if problems:
        raise ValueError("digest fails the link check, no .docx made: " + "; ".join(problems))
    docx_path = md_path.with_suffix(".docx")
    markdown_to_docx(md_text).save(docx_path)
    return docx_path


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) != 2:
        sys.exit("usage: python .claude/skills/weekly-digest/digest_docx.py digests/<date>.md")
    try:
        print(f"Saved {save_docx(sys.argv[1])}")
    except (ValueError, OSError) as error:
        print(f"DOCX not saved: {error}")
        sys.exit(1)
