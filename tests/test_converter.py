"""Basic smoke tests for eml2pdf."""

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from pathlib import Path

import pytest

# Add parent directory to path so we can import eml2pdf
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from eml2pdf import (
    EmailParser,
    HTMLBuilder,
    ParsedEmail,
    EmailToPDFConverter,
    sanitize_filename,
    format_date,
    parse_page_size,
    generate_output_path,
)


# ═══════════════════════════════════════════════════════════════════
# FIXTURES
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def sample_eml(tmp_path):
    """Create a sample EML file with both plain text and HTML parts."""
    msg = MIMEMultipart("alternative")
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    msg["Subject"] = "Test Email"
    msg["Date"] = "Mon, 1 Jan 2024 12:00:00 +0000"
    msg.attach(MIMEText("Hello plain", "plain"))
    msg.attach(MIMEText("<p>Hello <b>HTML</b></p>", "html"))

    eml_path = tmp_path / "test.eml"
    eml_path.write_bytes(msg.as_bytes())
    return eml_path


@pytest.fixture
def plain_text_eml(tmp_path):
    """Create a sample EML file with only plain text."""
    msg = MIMEText("Hello, this is a plain text email.", "plain")
    msg["From"] = "sender@example.com"
    msg["To"] = "receiver@example.com"
    msg["Subject"] = "Plain Text Only"
    msg["Date"] = "Tue, 2 Jan 2024 14:30:00 +0000"

    eml_path = tmp_path / "plain.eml"
    eml_path.write_bytes(msg.as_bytes())
    return eml_path


@pytest.fixture
def eml_with_attachment(tmp_path):
    """Create a sample EML file with an attachment."""
    msg = MIMEMultipart("mixed")
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    msg["Subject"] = "Email with Attachment"
    msg["Date"] = "Wed, 3 Jan 2024 09:00:00 +0000"

    msg.attach(MIMEText("See the attached file.", "plain"))

    # Add a fake text attachment
    attachment = MIMEText("This is the attachment content.", "plain")
    attachment.add_header("Content-Disposition", "attachment", filename="notes.txt")
    msg.attach(attachment)

    eml_path = tmp_path / "with_attachment.eml"
    eml_path.write_bytes(msg.as_bytes())
    return eml_path


# ═══════════════════════════════════════════════════════════════════
# PARSER TESTS
# ═══════════════════════════════════════════════════════════════════

def test_parser_extracts_headers(sample_eml):
    result = EmailParser().parse(sample_eml)
    assert result.headers["From"] == "alice@example.com"
    assert result.headers["To"] == "bob@example.com"
    assert result.headers["Subject"] == "Test Email"


def test_parser_extracts_html_body(sample_eml):
    result = EmailParser().parse(sample_eml)
    assert result.html_body is not None
    assert "Hello" in result.html_body
    assert "<b>HTML</b>" in result.html_body


def test_parser_extracts_text_body(plain_text_eml):
    result = EmailParser().parse(plain_text_eml)
    assert result.text_body is not None
    assert "plain text email" in result.text_body


def test_parser_extracts_date(sample_eml):
    result = EmailParser().parse(sample_eml)
    assert "2024-01-01" in result.headers["Date"]


def test_parser_extracts_attachments(eml_with_attachment):
    result = EmailParser().parse(eml_with_attachment)
    assert len(result.attachments) >= 1
    attachment_names = [a.name for a in result.attachments]
    assert "notes.txt" in attachment_names


# ═══════════════════════════════════════════════════════════════════
# HTML BUILDER TESTS
# ═══════════════════════════════════════════════════════════════════

def test_builder_produces_html(sample_eml):
    parsed = EmailParser().parse(sample_eml)
    html = HTMLBuilder().build(parsed)
    assert "<!DOCTYPE html>" in html
    assert "alice@example.com" in html
    assert "Test Email" in html


def test_builder_hides_headers_when_requested(sample_eml):
    parsed = EmailParser().parse(sample_eml)
    html = HTMLBuilder(hide_headers=True).build(parsed)
    assert "alice@example.com" not in html


def test_builder_includes_attachment_footer(eml_with_attachment):
    parsed = EmailParser().parse(eml_with_attachment)
    html = HTMLBuilder(add_attachment_names=True).build(parsed)
    assert "Attachments" in html
    assert "notes.txt" in html


def test_builder_plain_text_fallback(plain_text_eml):
    parsed = EmailParser().parse(plain_text_eml)
    html = HTMLBuilder().build(parsed)
    assert "<pre>" in html
    assert "plain text email" in html


def test_builder_sanitizes_scripts():
    parsed = ParsedEmail(
        headers={"Subject": "Malicious Email"},
        html_body="<p>Hi</p><script>alert(1)</script>",
    )
    html = HTMLBuilder().build(parsed)
    assert "<script>" not in html
    assert "alert" not in html


def test_builder_sanitizes_iframes():
    parsed = ParsedEmail(
        headers={"Subject": "Phishing"},
        html_body='<p>Click here</p><iframe src="http://evil.com"></iframe>',
    )
    html = HTMLBuilder().build(parsed)
    assert "<iframe" not in html


def test_builder_sanitizes_event_handlers():
    parsed = ParsedEmail(
        headers={"Subject": "XSS"},
        html_body='<div onmouseover="steal()">Hover me</div>',
    )
    html = HTMLBuilder().build(parsed)
    assert "onmouseover" not in html
    assert "steal" not in html


def test_builder_no_body():
    parsed = ParsedEmail(headers={"Subject": "Empty"})
    html = HTMLBuilder().build(parsed)
    assert "No content available" in html


# ═══════════════════════════════════════════════════════════════════
# UTILITY TESTS
# ═══════════════════════════════════════════════════════════════════

def test_sanitize_filename_special_chars():
    assert sanitize_filename('file<>:".txt') == "file____.txt"


def test_sanitize_filename_empty():
    assert sanitize_filename("") == "unnamed"


def test_sanitize_filename_none():
    assert sanitize_filename(None) == "unnamed"


def test_sanitize_filename_path_traversal():
    result = sanitize_filename("../../etc/passwd")
    assert ".." not in result
    assert "/" not in result


def test_sanitize_filename_long():
    long_name = "a" * 300 + ".txt"
    result = sanitize_filename(long_name)
    assert len(result) <= 200


def test_format_date_valid():
    formatted = format_date("Mon, 1 Jan 2024 12:00:00 +0000")
    assert "2024-01-01" in formatted


def test_format_date_empty():
    assert format_date("") == ""


def test_format_date_invalid():
    result = format_date("not a date")
    assert result == "not a date"


# ═══════════════════════════════════════════════════════════════════
# CONVERTER INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════

def test_convert_single_file(sample_eml, tmp_path):
    """Integration test: convert a single EML file to PDF."""
    pdf_path = tmp_path / "output.pdf"
    converter = EmailToPDFConverter()
    result = converter.convert(sample_eml, pdf_path)
    assert result == pdf_path
    assert pdf_path.exists()
    assert pdf_path.stat().st_size > 0
    # Verify it's a valid PDF (starts with %PDF)
    with open(pdf_path, "rb") as f:
        header = f.read(5)
    assert header == b"%PDF-"


def test_convert_plain_text(plain_text_eml, tmp_path):
    """Integration test: convert plain text EML to PDF."""
    pdf_path = tmp_path / "plain_output.pdf"
    converter = EmailToPDFConverter()
    result = converter.convert(plain_text_eml, pdf_path)
    assert pdf_path.exists()
    assert pdf_path.stat().st_size > 0


def test_convert_with_attachments_extraction(eml_with_attachment, tmp_path):
    """Integration test: convert EML and extract attachments."""
    pdf_path = tmp_path / "att_output.pdf"
    converter = EmailToPDFConverter(extract_attachments=True)
    converter.convert(eml_with_attachment, pdf_path)
    assert pdf_path.exists()
    att_dir = tmp_path / "att_output-attachments"
    assert att_dir.exists()


def test_convert_directory(tmp_path):
    """Integration test: batch convert a directory of EML files."""
    input_dir = tmp_path / "emails"
    output_dir = tmp_path / "pdfs"
    input_dir.mkdir()
    output_dir.mkdir()

    # Create 3 sample EML files
    for i in range(3):
        msg = MIMEText(f"Email body {i}", "plain")
        msg["From"] = f"sender{i}@example.com"
        msg["To"] = "receiver@example.com"
        msg["Subject"] = f"Test Email {i}"
        (input_dir / f"test{i}.eml").write_bytes(msg.as_bytes())

    converter = EmailToPDFConverter()
    stats = converter.convert_directory(input_dir, output_dir)

    assert stats["total"] == 3
    assert stats["success"] == 3
    assert stats["failed"] == 0
    assert len(list(output_dir.glob("*.pdf"))) == 3


def test_convert_nonexistent_file():
    """Converting a nonexistent file should raise FileNotFoundError."""
    converter = EmailToPDFConverter()
    with pytest.raises(FileNotFoundError):
        converter.convert("/tmp/nonexistent_file_xyz.eml")


def test_convert_auto_output_path(sample_eml):
    """When no output path given, PDF should be created next to the EML."""
    converter = EmailToPDFConverter()
    expected_pdf = sample_eml.with_suffix(".pdf")
    try:
        result = converter.convert(sample_eml)
        assert result == expected_pdf
        assert expected_pdf.exists()
    finally:
        if expected_pdf.exists():
            expected_pdf.unlink()


# ═══════════════════════════════════════════════════════════════════
# REMOTE IMAGES & LAYOUT OVERRIDE TESTS
# ═══════════════════════════════════════════════════════════════════

def test_custom_url_fetcher_blocks_external_by_default():
    from eml2pdf import CustomURLFetcher
    fetcher = CustomURLFetcher()
    with pytest.raises(ValueError) as excinfo:
        fetcher("http://example.com/image.png")
    assert "disabled for security" in str(excinfo.value)


def test_custom_url_fetcher_allows_data_uris():
    from eml2pdf import CustomURLFetcher
    fetcher = CustomURLFetcher()
    # base64 data URI of 1x1 transparent png
    res = fetcher("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    assert res is not None


def test_custom_url_fetcher_blocks_file_uris_by_default():
    from eml2pdf import CustomURLFetcher
    fetcher = CustomURLFetcher(allow_external=False)
    with pytest.raises(ValueError):
        fetcher("file:///tmp/test.png")


def test_builder_injects_layout_fixes():
    parsed = ParsedEmail(
        headers={"Subject": "Layout test"},
        html_body="<p>Test</p>",
    )
    html = HTMLBuilder().build(parsed)
    assert "height: auto !important" in html


def test_cli_parses_external_image_flags():
    from eml2pdf import build_arg_parser
    parser = build_arg_parser()
    args = parser.parse_args(["convert_file", "email.eml", "--unsafe", "--skip-ssl-verification"])
    assert args.command == "convert_file"
    assert args.input_file == "email.eml"
    assert args.unsafe is True
    assert args.skip_ssl_verification is True


def test_parse_page_size():
    assert parse_page_size("A4 landscape") == "297mm 210mm"
    assert parse_page_size("letter") == "8.5in 11in"
    assert parse_page_size("a3") == "297mm 420mm"
    assert parse_page_size("invalidsize") == "210mm 297mm"


def test_generate_output_path(tmp_path):
    # Valid date and subject
    p1 = generate_output_path("Mon, 1 Jan 2024 12:00:00 +0000", "Hello World", tmp_path)
    assert p1.name == "2024-01-01-Hello_World.pdf"

    # Empty date and subject fallback
    p2 = generate_output_path("", "", tmp_path)
    assert p2.name == "unknown-date-no-subject.pdf"

    # Collision handling
    p1.touch()
    p3 = generate_output_path("Mon, 1 Jan 2024 12:00:00 +0000", "Hello World", tmp_path)
    assert p3.name == "2024-01-01-Hello_World_1.pdf"

