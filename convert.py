#!/usr/bin/env python3
"""Convert a scanned PDF to EPUB using Claude's Vision API for OCR."""

import argparse
import base64
import json
import re
import sys
from pathlib import Path

import anthropic
import fitz  # PyMuPDF
from ebooklib import epub


SYSTEM_PROMPT = """You are an expert OCR transcription assistant. Your task is to faithfully transcribe all visible text from this scanned book page.

Rules:
- Transcribe ALL text exactly as it appears, preserving paragraph breaks
- Use markdown headings (## or ###) for chapter titles and section headings
- Preserve italic and bold emphasis where visible using markdown (*italic*, **bold**)
- Completely SKIP any illustrations, diagrams, photos, figures, charts, and their captions — do not mention them at all
- If a page contains only an illustration with no body text, respond with: [blank page]
- Do NOT add any commentary, summaries, or notes of your own
- Do NOT wrap output in markdown code fences
- If a page is blank or contains only a page number, respond with: [blank page]
- Preserve any footnotes, marking them clearly
- If text is partially cut off at page edges, transcribe what is visible"""


def render_page(doc: fitz.Document, page_num: int, dpi: int) -> bytes:
    """Render a PDF page as a JPEG image."""
    page = doc[page_num]
    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    return pix.tobytes("jpeg", jpg_quality=80)


def extract_text(client: anthropic.Anthropic, image_bytes: bytes, model: str) -> str:
    """Send a page image to Claude and get the transcribed text."""
    b64_image = base64.standard_b64encode(image_bytes).decode("utf-8")

    message = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": b64_image,
                        },
                    },
                    {
                        "type": "text",
                        "text": "Transcribe all text from this scanned book page.",
                    },
                ],
            }
        ],
        system=SYSTEM_PROMPT,
    )

    return message.content[0].text


def load_progress(progress_path: Path) -> dict:
    """Load existing progress from JSON file."""
    if progress_path.exists():
        with open(progress_path) as f:
            return json.load(f)
    return {}


def save_progress(progress_path: Path, progress: dict):
    """Save progress to JSON file."""
    with open(progress_path, "w") as f:
        json.dump(progress, f, indent=2)


def detect_chapters(pages: dict) -> list[tuple[str, list[int]]]:
    """Detect chapter boundaries from extracted text.

    Returns a list of (chapter_title, [page_numbers]) tuples.
    """
    chapter_pattern = re.compile(
        r"^#{1,3}\s*(chapter\s+\w+.*?)$", re.IGNORECASE | re.MULTILINE
    )

    chapters = []
    chapter_starts = []

    sorted_pages = sorted(pages.items(), key=lambda x: int(x[0]))

    for page_str, text in sorted_pages:
        page_num = int(page_str)
        match = chapter_pattern.search(text)
        if match:
            chapter_starts.append((page_num, match.group(1).strip()))

    if not chapter_starts:
        # Fall back: group every ~20 pages
        all_pages = [int(p) for p in pages]
        all_pages.sort()
        group_size = 20
        for i in range(0, len(all_pages), group_size):
            group = all_pages[i : i + group_size]
            title = f"Section {i // group_size + 1}"
            chapters.append((title, group))
        return chapters

    # Build chapters from detected boundaries
    all_pages = sorted(int(p) for p in pages)
    for i, (start_page, title) in enumerate(chapter_starts):
        if i + 1 < len(chapter_starts):
            end_page = chapter_starts[i + 1][0]
        else:
            end_page = all_pages[-1] + 1
        chapter_pages = [p for p in all_pages if start_page <= p < end_page]
        if chapter_pages:
            chapters.append((title, chapter_pages))

    # Handle pages before the first chapter
    if chapter_starts:
        first_chapter_page = chapter_starts[0][0]
        front_matter = [p for p in all_pages if p < first_chapter_page]
        if front_matter:
            chapters.insert(0, ("Front Matter", front_matter))

    return chapters


def build_epub(
    pages: dict,
    output_path: Path,
    title: str,
    authors: list[str],
):
    """Build an EPUB file from extracted text."""
    book = epub.EpubBook()
    book.set_identifier("pdf-converter-output")
    book.set_title(title)
    book.set_language("en")
    for author in authors:
        book.add_author(author)

    style = """
body { font-family: serif; line-height: 1.6; margin: 1em; }
h1, h2, h3 { margin-top: 1.5em; }
p { text-indent: 1.5em; margin: 0.3em 0; }
p:first-of-type { text-indent: 0; }
"""
    css = epub.EpubItem(
        uid="style", file_name="style/default.css", media_type="text/css", content=style
    )
    book.add_item(css)

    chapters_data = detect_chapters(pages)
    epub_chapters = []
    spine = ["nav"]

    for i, (chapter_title, chapter_pages) in enumerate(chapters_data):
        # Combine text from all pages in this chapter
        combined_text = []
        for page_num in chapter_pages:
            text = pages.get(str(page_num), "")
            if text and text.strip() != "[blank page]":
                combined_text.append(text)

        if not combined_text:
            continue

        # Convert markdown-ish text to simple HTML
        html_body = markdown_to_html("\n\n".join(combined_text))

        chapter = epub.EpubHtml(
            title=chapter_title,
            file_name=f"chapter_{i:03d}.xhtml",
            lang="en",
        )
        chapter.content = f"<html><body><h1>{chapter_title}</h1>\n{html_body}</body></html>"
        chapter.add_item(css)

        book.add_item(chapter)
        epub_chapters.append(chapter)
        spine.append(chapter)

    book.toc = epub_chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = spine

    epub.write_epub(str(output_path), book)


def markdown_to_html(text: str) -> str:
    """Convert simple markdown to HTML."""
    lines = text.split("\n")
    html_parts = []
    in_paragraph = False

    for line in lines:
        stripped = line.strip()

        if not stripped:
            if in_paragraph:
                html_parts.append("</p>")
                in_paragraph = False
            continue

        # Headings
        if stripped.startswith("### "):
            if in_paragraph:
                html_parts.append("</p>")
                in_paragraph = False
            html_parts.append(f"<h3>{_inline_format(stripped[4:])}</h3>")
            continue
        if stripped.startswith("## "):
            if in_paragraph:
                html_parts.append("</p>")
                in_paragraph = False
            html_parts.append(f"<h2>{_inline_format(stripped[3:])}</h2>")
            continue
        if stripped.startswith("# "):
            if in_paragraph:
                html_parts.append("</p>")
                in_paragraph = False
            html_parts.append(f"<h1>{_inline_format(stripped[2:])}</h1>")
            continue

        # Illustration markers
        if stripped.startswith("[Illustration:") or stripped == "[blank page]":
            if in_paragraph:
                html_parts.append("</p>")
                in_paragraph = False
            html_parts.append(f"<p><em>{_escape_html(stripped)}</em></p>")
            continue

        # Regular paragraph text
        if not in_paragraph:
            html_parts.append(f"<p>{_inline_format(stripped)}")
            in_paragraph = True
        else:
            html_parts.append(f" {_inline_format(stripped)}")

    if in_paragraph:
        html_parts.append("</p>")

    return "\n".join(html_parts)


def _escape_html(text: str) -> str:
    """Escape HTML special characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline_format(text: str) -> str:
    """Handle bold and italic markdown inline formatting."""
    text = _escape_html(text)
    # Bold: **text**
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    # Italic: *text*
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    return text


def parse_filename_metadata(filename: str) -> tuple[str, list[str]]:
    """Try to extract title and authors from the filename.

    Expects format like: 'Title - Author1 and Author2.pdf'
    """
    stem = Path(filename).stem
    if " - " in stem:
        parts = stem.split(" - ", 1)
        title = parts[0].strip()
        author_str = parts[1].strip()
        # Split authors by "and", "&", or ","
        authors = re.split(r"\s+and\s+|&|,", author_str)
        authors = [a.strip() for a in authors if a.strip()]
        return title, authors
    return stem, ["Unknown"]


def main():
    parser = argparse.ArgumentParser(
        description="Convert a scanned PDF to EPUB using Claude's Vision API for OCR."
    )
    parser.add_argument("input", help="Path to the input PDF file")
    parser.add_argument("--output", "-o", help="Path to the output EPUB file")
    parser.add_argument(
        "--model",
        default="claude-haiku-4-5",
        help="Claude model to use (default: claude-haiku-4-5)",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=150,
        help="DPI for page rendering (default: 150)",
    )
    parser.add_argument(
        "--start-page",
        type=int,
        default=0,
        help="First page to process (0-indexed, default: 0)",
    )
    parser.add_argument(
        "--end-page",
        type=int,
        default=None,
        help="Last page to process (0-indexed, inclusive)",
    )
    parser.add_argument(
        "--epub-only",
        action="store_true",
        help="Skip OCR, just build EPUB from existing progress file",
    )

    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: {input_path} not found", file=sys.stderr)
        sys.exit(1)

    if args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.with_suffix(".epub")

    progress_path = input_path.with_suffix(".progress.json")

    # Open PDF to get page count
    doc = fitz.open(str(input_path))
    total_pages = len(doc)
    start = args.start_page
    end = args.end_page if args.end_page is not None else total_pages - 1
    end = min(end, total_pages - 1)

    print(f"PDF: {input_path.name} ({total_pages} pages)")
    print(f"Processing pages {start}-{end} (DPI: {args.dpi}, Model: {args.model})")

    # Load existing progress
    progress = load_progress(progress_path)
    print(f"Progress file: {progress_path.name} ({len(progress)} pages already done)")

    if not args.epub_only:
        client = anthropic.Anthropic()

        for page_num in range(start, end + 1):
            page_key = str(page_num)
            if page_key in progress:
                continue

            print(f"[{page_num + 1}/{total_pages}] Processing page {page_num}...")

            image_bytes = render_page(doc, page_num, args.dpi)
            text = extract_text(client, image_bytes, args.model)
            progress[page_key] = text
            save_progress(progress_path, progress)

        print(f"\nOCR complete. {len(progress)} pages extracted.")

    doc.close()

    # Build EPUB
    title, authors = parse_filename_metadata(input_path.name)
    print(f"\nBuilding EPUB: {output_path.name}")
    print(f"  Title: {title}")
    print(f"  Authors: {', '.join(authors)}")

    build_epub(progress, output_path, title, authors)
    print(f"Done! Output: {output_path} ({output_path.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
