#!/usr/bin/env python3
"""
eml2pdf — Convert EML email files to PDF documents.

A command-line tool and Python library for converting RFC 822 email files
into PDF format while preserving headers, HTML formatting, inline images,
and optionally extracting attachments.

Usage (CLI):
    eml2pdf email.eml
    eml2pdf ./emails/ -o ./pdfs/ --extract-attachments

Usage (Library):
    from eml2pdf import EmailToPDFConverter
    converter = EmailToPDFConverter()
    converter.convert("email.eml", "output.pdf")
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import logging
import multiprocessing
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html import escape
from pathlib import Path
from typing import Optional, Tuple

import chardet
import weasyprint
from bs4 import BeautifulSoup

# New optional dependency
try:
    from markdown import markdown
    HAS_MARKDOWN = True
except ImportError:
    HAS_MARKDOWN = False

__version__ = "1.1.0"
__author__ = "Michaël BETTAN"

logger = logging.getLogger("eml2pdf")


# ═══════════════════════════════════════════════════════════════════
# PAGE SIZE DEFINITIONS
# ═══════════════════════════════════════════════════════════════════

_BASE_SIZES = {
    "a3":     "297mm 420mm",
    "a4":     "210mm 297mm",
    "a5":     "148mm 210mm",
    "b4":     "250mm 353mm",
    "b5":     "176mm 250mm",
    "letter": "8.5in 11in",
    "legal":  "8.5in 14in",
    "ledger": "11in 17in",
}


def parse_page_size(spec: str) -> str:
    """Parse a page-size spec like 'a4', 'a4 landscape', 'letter landscape'.

    Returns a CSS @page size value.
    """
    parts = spec.lower().strip().split()
    if not parts:
        return _BASE_SIZES["a4"]

    name = parts[0]
    landscape = "landscape" in parts[1:]

    base = _BASE_SIZES.get(name)
    if not base:
        logger.warning(f"Unknown page size '{name}', falling back to a4")
        base = _BASE_SIZES["a4"]

    if landscape:
        # Swap dimensions
        w, h = base.split()
        return f"{h} {w}"
    return base


# ═══════════════════════════════════════════════════════════════════
# HTML TEMPLATES
# ═══════════════════════════════════════════════════════════════════

HTML_WRAPPER = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>{title}</title>
    <style>
        @page {{
            size: {page_size};
            margin: 0.75in;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                         Helvetica, Arial, sans-serif;
            margin: 0;
            padding: 0;
            color: #1f2937;
            line-height: 1.5;
            font-size: 14px;
        }}
        .email-body {{
            max-width: {max_width}px;
            margin: 0 auto;
        }}
        img {{ max-width: 100%; height: auto; }}
        table {{ max-width: 100%; border-collapse: collapse; }}
        pre {{
            white-space: pre-wrap;
            word-wrap: break-word;
            font-family: "SFMono-Regular", Consolas, "Liberation Mono",
                         Menlo, Courier, monospace;
            font-size: 13px;
            background-color: #f8f9fa;
            padding: 16px;
            border-radius: 6px;
            border: 1px solid #e5e7eb;
        }}
        blockquote {{
            border-left: 3px solid #d1d5db;
            padding-left: 12px;
            color: #6b7280;
            margin-left: 0;
        }}
        a {{ color: #2563eb; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
    </style>
</head>
<body>
{header}
<div class="email-body">{body}</div>
{footer}
</body>
</html>
"""

HEADER_BLOCK = """
<div style="font-family: inherit; border: 1px solid #e5e7eb; border-radius: 6px;
            background-color: #f9fafb; padding: 16px; margin-bottom: 24px;
            font-size: 13px;">
    <table style="width: 100%;">
        {rows}
    </table>
</div>
"""

HEADER_ROW = """
<tr>
    <td style="width: 90px; padding: 4px 8px; vertical-align: top;
               font-weight: 600; color: #4b5563;">{label}:</td>
    <td style="padding: 4px 8px; overflow-wrap: break-word; color: #111827;">{value}</td>
</tr>
"""

ATTACHMENT_FOOTER = """
<div style="margin-top: 32px; padding-top: 12px;
            border-top: 2px solid #e5e7eb; font-size: 12px; color: #4b5563;">
    <strong>&#128206; Attachments ({count}):</strong>
    <table style="width: 100%; margin-top: 8px; border-collapse: collapse;
                  font-size: 11px;">
        <thead>
            <tr style="background: #f3f4f6;">
                <th style="text-align: left; padding: 6px 8px;
                           border-bottom: 1px solid #e5e7eb;">Name</th>
                <th style="text-align: left; padding: 6px 8px;
                           border-bottom: 1px solid #e5e7eb;">Type</th>
                <th style="text-align: right; padding: 6px 8px;
                           border-bottom: 1px solid #e5e7eb;">Size</th>
                <th style="text-align: left; padding: 6px 8px;
                           border-bottom: 1px solid #e5e7eb;">MD5</th>
            </tr>
        </thead>
        <tbody>{rows}</tbody>
    </table>
</div>
"""


def humanize_size(num_bytes: int) -> str:
    """Convert byte count to human-readable string (KB, MB, etc.)."""
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024.0:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} TB"


# ═══════════════════════════════════════════════════════════════════
# DATA MODELS
# ═══════════════════════════════════════════════════════════════════

@dataclass
class Attachment:
    """Represents an email attachment."""
    name: str
    content: bytes
    content_type: str

    @property
    def size(self) -> int:
        return len(self.content)


@dataclass
class ParsedEmail:
    """Structured representation of a parsed email."""
    headers: dict = field(default_factory=dict)
    html_body: Optional[str] = None
    text_body: Optional[str] = None
    attachments: list = field(default_factory=list)
    inline_images: dict = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════
# UTILITIES
# ═══════════════════════════════════════════════════════════════════

def decode_part(part) -> str:
    """Decode an email part's payload to a string.

    Multi-tier fallback: declared charset → chardet detection → UTF-8 replace.
    Also handles Unicode escape sequences inserted by some email clients.
    """
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""

    charset = part.get_content_charset() or "utf-8"

    try:
        decoded = payload.decode(charset)
    except (LookupError, UnicodeDecodeError):
        logger.debug(f"Strict decode failed for {charset}, falling back")
        detected = chardet.detect(payload)
        encoding = detected.get("encoding") or "utf-8"
        try:
            decoded = payload.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            decoded = payload.decode(charset, errors="replace")

    # Handle Unicode escape sequences (\uXXXX) some clients insert
    if re.search(r"\\u[0-9a-fA-F]{4}|\\U[0-9a-fA-F]{8}", decoded):
        try:
            decoded = decoded.encode("utf-8").decode("unicode-escape")
        except (UnicodeDecodeError, UnicodeEncodeError) as exc:
            logger.debug(f"Unicode escape decoding skipped: {exc}")

    return decoded


def sanitize_filename(name: str) -> str:
    """Make a filename filesystem-safe.

    Strips dangerous characters, prevents path traversal, and truncates
    to 200 characters.
    """
    if not name:
        return "unnamed"
    # Strip path traversal sequences and unsafe chars
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    # Remove any remaining path separators to prevent traversal
    cleaned = cleaned.replace("..", "_")
    return cleaned[:200] or "unnamed"


def format_date(date_str: str) -> str:
    """Convert RFC 2822 date string to a readable format."""
    if not date_str:
        return ""
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.strftime("%Y-%m-%d %H:%M:%S %z").strip()
    except (TypeError, ValueError):
        return date_str


# ═══════════════════════════════════════════════════════════════════
# OUTPUT FILENAME GENERATION
# ═══════════════════════════════════════════════════════════════════

def generate_output_path(date_str: str, subject: str,
                          output_dir: Path) -> Path:
    """Generate a {date}-{subject}.pdf path, handling collisions.

    Inspired by plenaerts/eml2pdf naming convention. Produces sortable,
    human-readable archive filenames.
    """
    # Parse date to YYYY-MM-DD prefix
    date_prefix = "unknown-date"
    if date_str:
        try:
            try:
                dt = parsedate_to_datetime(date_str)
            except Exception:
                dt = datetime.strptime(date_str.split()[0], "%Y-%m-%d")
            date_prefix = dt.strftime("%Y-%m-%d")
        except (TypeError, ValueError, IndexError):
            pass

    # Sanitize subject
    safe_subject = sanitize_filename(subject or "no-subject")
    # Replace whitespace with underscores for nicer filenames
    safe_subject = re.sub(r"\s+", "_", safe_subject)[:120]

    base_name = f"{date_prefix}-{safe_subject}"
    candidate = output_dir / f"{base_name}.pdf"

    counter = 1
    while candidate.exists():
        candidate = output_dir / f"{base_name}_{counter}.pdf"
        counter += 1

    return candidate


# ═══════════════════════════════════════════════════════════════════
# CUSTOM URL FETCHER (SECURITY: controls remote resource loading)
# ═══════════════════════════════════════════════════════════════════

class CustomURLFetcher:
    """Custom URL fetcher that controls remote resource loading and SSL verification."""

    def __init__(self, allow_external: bool = False, skip_ssl_verification: bool = False):
        self.allow_external = allow_external
        self.skip_ssl_verification = skip_ssl_verification

    def __call__(self, url: str, timeout: int = 10, ssl_context=None):
        if url.startswith("data:"):
            return weasyprint.urls.default_url_fetcher(url)

        if not self.allow_external:
            logger.debug(f"  Blocked external URL fetch: {url}")
            raise ValueError(
                f"External URL fetching is disabled for security. Blocked: {url}"
            )

        if self.skip_ssl_verification:
            import ssl
            ssl_context = ssl._create_unverified_context()

        return weasyprint.urls.default_url_fetcher(url, timeout=timeout, ssl_context=ssl_context)



# ═══════════════════════════════════════════════════════════════════
# EMAIL PARSER
# ═══════════════════════════════════════════════════════════════════

class EmailParser:
    """Parses an EML file into a ParsedEmail object."""

    HEADER_FIELDS = ("From", "To", "Cc", "Bcc", "Reply-To", "Subject", "Date")

    def parse(self, eml_path: Path) -> ParsedEmail:
        """Parse an EML file and return structured email data."""
        with open(eml_path, "rb") as f:
            msg = BytesParser(policy=policy.default).parse(f)

        result = ParsedEmail()
        self._extract_headers(msg, result)
        self._extract_parts(msg, result)
        return result

    def _extract_headers(self, msg, result: ParsedEmail) -> None:
        for field_name in self.HEADER_FIELDS:
            value = msg.get(field_name, "")
            if field_name == "Date":
                value = format_date(value)
            result.headers[field_name] = str(value) if value else ""

    def _extract_parts(self, msg, result: ParsedEmail) -> None:
        for part in msg.walk():
            if part.is_multipart():
                continue

            ctype = part.get_content_type()
            disposition = str(part.get("Content-Disposition", "")).lower()
            cid = (part.get("Content-ID") or "").strip("<>")

            # Inline image
            if ctype.startswith("image/") and (cid or "inline" in disposition):
                self._handle_inline_image(part, cid, ctype, result)
                continue

            # Attachment
            if "attachment" in disposition or part.get_filename():
                self._handle_attachment(part, ctype, result)
                continue

            # Body content
            if ctype == "text/html" and result.html_body is None:
                result.html_body = decode_part(part)
            elif ctype == "text/plain" and result.text_body is None:
                result.text_body = decode_part(part)

    @staticmethod
    def _handle_inline_image(part, cid: str, ctype: str, result: ParsedEmail) -> None:
        payload = part.get_payload(decode=True)
        if payload and cid:
            b64 = base64.b64encode(payload).decode("ascii")
            result.inline_images[cid] = f"data:{ctype};base64,{b64}"

    @staticmethod
    def _handle_attachment(part, ctype: str, result: ParsedEmail) -> None:
        filename = part.get_filename() or "unnamed"
        payload = part.get_payload(decode=True) or b""
        result.attachments.append(
            Attachment(name=filename, content=payload, content_type=ctype)
        )


# ═══════════════════════════════════════════════════════════════════
# HTML BUILDER
# ═══════════════════════════════════════════════════════════════════

class HTMLBuilder:
    """Builds a complete HTML document from a ParsedEmail.

    Handles inline image embedding, HTML sanitization (removing scripts,
    iframes, event handlers), and optional header/footer rendering.
    """

    UNSAFE_TAGS = ("script", "iframe", "object", "embed")

    def __init__(self, hide_headers: bool = False,
                 add_attachment_names: bool = False,
                 page_size: str = "a4",
                 unsafe: bool = False,
                 max_content_width_px: int = 720):
        self.hide_headers = hide_headers
        self.add_attachment_names = add_attachment_names
        self.page_size = page_size
        self.unsafe = unsafe
        self.max_content_width_px = max_content_width_px

    def build(self, parsed: ParsedEmail) -> str:
        """Build the final HTML string from parsed email data."""
        title = parsed.headers.get("Subject", "Email") or "Email"
        header = "" if self.hide_headers else self._build_header(parsed)
        body = self._build_body(parsed)
        footer = self._build_footer(parsed) if self.add_attachment_names else ""

        # Fix WeasyPrint layout loops and alignment issues:
        # 1. Reset height/min-height to avoid infinite layout loops on nested percentage heights
        # 2. Reset float to avoid WeasyPrint layout hangs and visual clipping on column wraps
        # 3. Force max-width with !important to prevent large images from overflowing pages
        layout_fix = (
            "\n<style>\n"
            "  body, html, #bodyTable, #bodyCell, table, tr, td, div { height: auto !important; min-height: 0 !important; }\n"
            "  table, tr, td, div, img { float: none !important; }\n"
            "  img { max-width: 100% !important; height: auto !important; }\n"
            "  table { max-width: 100% !important; }\n"
            "</style>\n"
        )
        body += layout_fix

        css_page_size = parse_page_size(self.page_size)

        return HTML_WRAPPER.format(
            title=escape(title),
            header=header,
            body=body,
            footer=footer,
            page_size=css_page_size,
            max_width=self.max_content_width_px,
        )

    def _build_header(self, parsed: ParsedEmail) -> str:
        rows = []
        for label in EmailParser.HEADER_FIELDS:
            value = parsed.headers.get(label, "")
            if value:
                rows.append(HEADER_ROW.format(
                    label=escape(label),
                    value=escape(value),
                ))
        return HEADER_BLOCK.format(rows="".join(rows))

    def _build_body(self, parsed: ParsedEmail) -> str:
        if parsed.html_body:
            return self._process_html_body(parsed)
        if parsed.text_body:
            if HAS_MARKDOWN:
                # Render plain text as markdown for nicer output
                try:
                    return markdown(parsed.text_body,
                                    extensions=["nl2br", "fenced_code"])
                except Exception as exc:
                    logger.debug(f"Markdown render failed, using <pre>: {exc}")
            return f"<pre>{escape(parsed.text_body)}</pre>"
        return "<p><em>No content available</em></p>"

    def _process_html_body(self, parsed: ParsedEmail) -> str:
        soup = BeautifulSoup(parsed.html_body, "html.parser")

        # Replace cid: references with embedded base64 images
        for img in soup.find_all("img"):
            src = img.get("src", "")
            if src.lower().startswith("cid:"):
                cid = src[4:]
                if cid in parsed.inline_images:
                    img["src"] = parsed.inline_images[cid]

        # Sanitize unsafe elements unless unsafe mode is enabled
        if not self.unsafe:
            for tag_name in self.UNSAFE_TAGS:
                for tag in soup(tag_name):
                    tag.decompose()

            # Remove on* event handlers (prevents XSS in rendered content)
            for tag in soup.find_all(True):
                for attr in list(tag.attrs):
                    if attr.lower().startswith("on"):
                        del tag.attrs[attr]

        return str(soup)

    def _build_footer(self, parsed: ParsedEmail) -> str:
        if not parsed.attachments:
            return ""
        rows = []
        for att in parsed.attachments:
            md5 = hashlib.md5(att.content).hexdigest()
            rows.append(
                f"<tr>"
                f"<td style='padding:6px 8px;border-bottom:1px solid #f3f4f6;'>"
                f"{escape(att.name)}</td>"
                f"<td style='padding:6px 8px;border-bottom:1px solid #f3f4f6;"
                f"color:#6b7280;'>{escape(att.content_type)}</td>"
                f"<td style='padding:6px 8px;border-bottom:1px solid #f3f4f6;"
                f"text-align:right;'>{humanize_size(att.size)}</td>"
                f"<td style='padding:6px 8px;border-bottom:1px solid #f3f4f6;"
                f"font-family:monospace;color:#6b7280;'>{md5}</td>"
                f"</tr>"
            )
        return ATTACHMENT_FOOTER.format(
            count=len(parsed.attachments),
            rows="".join(rows),
        )


class EmailToPDFConverter:
    """Main converter orchestrating parsing, HTML build, and PDF generation.

    Usage:
        converter = EmailToPDFConverter(page_size="a4", dpi=300)
        converter.convert("email.eml", "output.pdf")
        converter.convert_directory("./emails/", "./pdfs/")
    """

    def __init__(
        self,
        page_size: str = "a4",
        dpi: int = 300,
        wkhtmltopdf_path: Optional[str] = None,
        proxy: Optional[str] = None,
        hide_headers: bool = False,
        add_attachment_names: bool = False,
        extract_attachments: bool = False,
        attachments_dir: Optional[str] = None,
        allow_external_images: bool = False,
        skip_ssl_verification: bool = False,
        debug_html: bool = False,
        unsafe: bool = False,
        max_content_width_px: int = 720,
    ):
        self.page_size = page_size
        self.dpi = dpi
        # wkhtmltopdf_path and proxy kept for API compatibility but unused
        # with WeasyPrint backend
        self.wkhtmltopdf_path = wkhtmltopdf_path
        self.proxy = proxy
        self.hide_headers = hide_headers
        self.add_attachment_names = add_attachment_names
        self.extract_attachments = extract_attachments
        self.attachments_dir = attachments_dir
        self.allow_external_images = allow_external_images or unsafe
        self.skip_ssl_verification = skip_ssl_verification
        self.debug_html = debug_html
        self.unsafe = unsafe
        self.max_content_width_px = max_content_width_px

        self.parser = EmailParser()
        self.builder = HTMLBuilder(
            hide_headers=hide_headers,
            add_attachment_names=add_attachment_names,
            page_size=page_size,
            unsafe=unsafe,
            max_content_width_px=max_content_width_px,
        )

    # ───────────────── Public API ─────────────────

    def convert(
        self,
        eml_path: str | Path,
        pdf_path: Optional[str | Path] = None,
    ) -> Path:
        """Convert a single EML file to PDF. Returns output path."""
        eml_path = Path(eml_path)
        if not eml_path.exists():
            raise FileNotFoundError(f"EML file not found: {eml_path}")
        if not eml_path.is_file():
            raise ValueError(f"Not a file: {eml_path}")

        pdf_path = Path(pdf_path) if pdf_path else eml_path.with_suffix(".pdf")
        pdf_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info(f"Converting: {eml_path.name}")

        parsed = self.parser.parse(eml_path)
        html = self.builder.build(parsed)

        # Write intermediate HTML for debugging if requested
        if self.debug_html:
            html_path = pdf_path.with_suffix(".html")
            html_path.write_text(html, encoding="utf-8")
            logger.debug(f"  Wrote debug HTML: {html_path}")

        self._render_pdf(html, pdf_path)

        logger.info(f"  ✓ PDF created: {pdf_path}")

        if self.extract_attachments:
            self._extract_attachments(parsed, pdf_path)

        return pdf_path

    def convert_directory(
        self,
        input_dir: str | Path,
        output_dir: Optional[str | Path] = None,
        num_procs: int = 1,
        smart_naming: bool = False,
    ) -> dict:
        """Convert all EML files in directory, optionally in parallel."""
        input_dir = Path(input_dir)
        if not input_dir.is_dir():
            raise NotADirectoryError(f"Not a directory: {input_dir}")

        eml_files = sorted(input_dir.glob("*.eml"))
        if not eml_files:
            logger.warning(f"No EML files found in: {input_dir}")
            return {"total": 0, "success": 0, "failed": 0}

        if output_dir:
            Path(output_dir).mkdir(parents=True, exist_ok=True)

        # Force serial if debug logging is on (so output isn't interleaved)
        if logger.isEnabledFor(logging.DEBUG):
            num_procs = 1
            logger.debug("Debug mode: forcing serial processing")

        # Clamp num_procs
        num_procs = max(1, min(num_procs, len(eml_files),
                               multiprocessing.cpu_count()))

        logger.info(
            f"Found {len(eml_files)} EML file(s); "
            f"using {num_procs} process(es)"
        )

        stats = {"total": len(eml_files), "success": 0, "failed": 0}
        converter_kwargs = self._kwargs_for_worker()

        worker_args = [
            (str(f), str(output_dir) if output_dir else None,
             converter_kwargs, smart_naming)
            for f in eml_files
        ]

        if num_procs > 1:
            with multiprocessing.Pool(num_procs) as pool:
                results = pool.map(_convert_worker, worker_args)
        else:
            results = [_convert_worker(a) for a in worker_args]

        for name, success, err in results:
            if success:
                stats["success"] += 1
                logger.info(f"  ✓ {name}")
            else:
                stats["failed"] += 1
                logger.error(f"  ✗ {name} — {err}")

        logger.info(
            f"\n═══ Done: {stats['success']} succeeded, "
            f"{stats['failed']} failed ═══"
        )
        return stats

    def _kwargs_for_worker(self) -> dict:
        """Serialize current config for worker processes."""
        return {
            "page_size": self.page_size,
            "dpi": self.dpi,
            "wkhtmltopdf_path": self.wkhtmltopdf_path,
            "proxy": self.proxy,
            "hide_headers": self.hide_headers,
            "add_attachment_names": self.add_attachment_names,
            "extract_attachments": self.extract_attachments,
            "attachments_dir": self.attachments_dir,
            "allow_external_images": self.allow_external_images,
            "skip_ssl_verification": self.skip_ssl_verification,
            "debug_html": self.debug_html,
            "unsafe": self.unsafe,
            "max_content_width_px": self.max_content_width_px,
        }

    # ───────────────── Internals ─────────────────

    def _render_pdf(self, html: str, pdf_path: Path) -> None:
        """Render HTML string to PDF using WeasyPrint."""
        try:
            # Suppress noisy CSS warnings from email HTML
            wp_logger = logging.getLogger("weasyprint")
            original_level = wp_logger.level
            wp_logger.setLevel(logging.ERROR)

            fetcher = CustomURLFetcher(
                allow_external=self.allow_external_images,
                skip_ssl_verification=self.skip_ssl_verification,
            )
            html_doc = weasyprint.HTML(
                string=html,
                url_fetcher=fetcher,
            )
            html_doc.write_pdf(
                str(pdf_path),
                dpi=self.dpi,
                presentational_hints=True,
            )

            wp_logger.setLevel(original_level)
        except Exception as exc:
            raise RuntimeError(
                f"PDF rendering failed: {exc}"
            ) from exc

    def _extract_attachments(self, parsed: ParsedEmail, pdf_path: Path) -> None:
        """Extract email attachments to disk with collision-safe naming."""
        if not parsed.attachments:
            return

        target_dir = (
            Path(self.attachments_dir)
            if self.attachments_dir
            else pdf_path.parent / f"{pdf_path.stem}-attachments"
        )
        target_dir.mkdir(parents=True, exist_ok=True)

        for att in parsed.attachments:
            safe_name = sanitize_filename(att.name)
            out_path = target_dir / safe_name

            # Handle filename collisions
            counter = 1
            while out_path.exists():
                stem, suffix = Path(safe_name).stem, Path(safe_name).suffix
                out_path = target_dir / f"{stem}_{counter}{suffix}"
                counter += 1

            with open(out_path, "wb") as f:
                f.write(att.content)
            logger.debug(f"  Extracted: {out_path.name}")

        logger.info(
            f"  📎 Extracted {len(parsed.attachments)} attachment(s) "
            f"-> {target_dir}"
        )


# ═══════════════════════════════════════════════════════════════════
# WORKER (top-level for multiprocessing pickling)
# ═══════════════════════════════════════════════════════════════════

def _convert_worker(args: Tuple) -> Tuple[str, bool, str]:
    """Worker function for parallel conversion.

    Returns (filename, success, error_message).
    Must be top-level so multiprocessing can pickle it.
    """
    eml_path, output_dir, converter_kwargs, use_smart_naming = args
    try:
        converter = EmailToPDFConverter(**converter_kwargs)
        parsed = converter.parser.parse(eml_path)

        if use_smart_naming and output_dir:
            pdf_path = generate_output_path(
                parsed.headers.get("Date", ""),
                parsed.headers.get("Subject", ""),
                Path(output_dir),
            )
        elif output_dir:
            pdf_path = Path(output_dir) / f"{Path(eml_path).stem}.pdf"
        else:
            pdf_path = None

        converter.convert(eml_path, pdf_path)
        return (Path(eml_path).name, True, "")
    except Exception as exc:
        return (Path(eml_path).name, False, str(exc))


# ═══════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════

def setup_logging(debug: bool = False, quiet: bool = False) -> None:
    """Configure root logger."""
    if quiet:
        level = logging.ERROR
    elif debug:
        level = logging.DEBUG
    else:
        level = logging.INFO
    logging.basicConfig(level=level, format="%(message)s", force=True)


def _add_shared_options(parser: argparse.ArgumentParser) -> None:
    """Add options shared between convert_file and convert_dir."""
    parser.add_argument("-p", "--page", default="a4", metavar="SIZE",
                        help='Page size: a3, a4, a5, b4, b5, letter, legal, '
                             'ledger; optionally with "landscape" '
                             '(e.g. "a4 landscape"). Default: a4')
    parser.add_argument("-r", "--dpi", type=int, default=300,
                        help="DPI for rendering (default: 300)")
    parser.add_argument("-hh", "--hide-headers", action="store_true",
                        help="Do not include email headers in PDF")
    parser.add_argument("-aa", "--add-attachment-names", action="store_true",
                        help="Append attachment table at end of PDF")
    parser.add_argument("-a", "--extract-attachments", action="store_true",
                        help="Save attachments to disk")
    parser.add_argument("-ad", "--attachments-dir",
                        help="Custom directory for extracted attachments")
    parser.add_argument("--unsafe", action="store_true",
                        help="Disable HTML sanitization and allow remote "
                             "resources. WARNING: may expose tracking pixels "
                             "and execute remote requests.")
    parser.add_argument("--skip-ssl-verification", action="store_true",
                        help="Skip SSL verification for remote fetches")
    parser.add_argument("--max-content-width", type=int, default=720,
                        metavar="PX",
                        help="Max content width in pixels (default: 720)")
    parser.add_argument("-d", "--debug-html", action="store_true",
                        help="Write intermediate HTML next to PDF")

    log_group = parser.add_mutually_exclusive_group()
    log_group.add_argument("-v", "--verbose", action="store_true",
                           help="Verbose debug output (forces serial mode)")
    log_group.add_argument("-q", "--quiet", action="store_true",
                           help="Show only errors")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eml2pdf",
        description="Convert EML email files to PDF documents.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="version",
                        version=f"eml2pdf {__version__}")

    subparsers = parser.add_subparsers(
        dest="command",
        title="Subcommands",
        description="Use {subcommand} --help for details",
        metavar="{convert_file,convert_dir}",
    )

    # convert_file
    p_file = subparsers.add_parser(
        "convert_file",
        help="Convert a single EML file to a PDF",
        description="Convert a single EML file to a PDF.",
    )
    p_file.add_argument("input_file", help="Input EML file")
    p_file.add_argument("output_file", nargs="?",
                        help="Output PDF (defaults to <input>.pdf)")
    _add_shared_options(p_file)

    # convert_dir
    p_dir = subparsers.add_parser(
        "convert_dir",
        help="Convert all EML files in a directory",
        description="Convert all EML files in an input dir to PDFs in "
                    "an output dir.",
    )
    p_dir.add_argument("input_dir", help="Directory containing EML files")
    p_dir.add_argument("output_dir", help="Directory for PDF output")
    p_dir.add_argument("-n", "--num-procs", type=int,
                       default=multiprocessing.cpu_count(),
                       metavar="N",
                       help=f"Number of parallel processes "
                            f"(default: {multiprocessing.cpu_count()})")
    p_dir.add_argument("--smart-naming", action="store_true",
                       help="Use {date}-{subject}.pdf naming "
                            "instead of {original-stem}.pdf")
    _add_shared_options(p_dir)

    return parser


def _build_converter(args) -> EmailToPDFConverter:
    """Construct converter from parsed args."""
    return EmailToPDFConverter(
        page_size=args.page,
        dpi=args.dpi,
        hide_headers=args.hide_headers,
        add_attachment_names=args.add_attachment_names,
        extract_attachments=args.extract_attachments,
        attachments_dir=args.attachments_dir,
        allow_external_images=args.unsafe,
        skip_ssl_verification=args.skip_ssl_verification,
        max_content_width_px=args.max_content_width,
        debug_html=args.debug_html,
        unsafe=args.unsafe,
    )


def main(argv: Optional[list] = None) -> int:
    """CLI entry point."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    setup_logging(
        debug=getattr(args, "verbose", False),
        quiet=getattr(args, "quiet", False),
    )

    try:
        converter = _build_converter(args)

        if args.command == "convert_file":
            input_path = Path(args.input_file)
            if not input_path.is_file():
                logger.error(f"Error: not a file: {input_path}")
                return 1
            converter.convert(input_path, args.output_file)

        elif args.command == "convert_dir":
            input_path = Path(args.input_dir)
            if not input_path.is_dir():
                logger.error(f"Error: not a directory: {input_path}")
                return 1
            stats = converter.convert_directory(
                input_path,
                args.output_dir,
                num_procs=args.num_procs,
                smart_naming=args.smart_naming,
            )
            if stats["failed"] > 0:
                return 2

    except FileNotFoundError as exc:
        logger.error(f"Error: {exc}")
        return 1
    except RuntimeError as exc:
        logger.error(f"Error: {exc}")
        return 3
    except KeyboardInterrupt:
        logger.error("\nInterrupted by user")
        return 130
    except Exception as exc:
        logger.error(f"Unexpected error: {exc}")
        if getattr(args, "verbose", False):
            raise
        return 99

    return 0



if __name__ == "__main__":
    sys.exit(main())
