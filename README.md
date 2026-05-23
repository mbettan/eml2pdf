# eml2pdf

Convert EML email files to PDF documents while preserving formatting,
inline images, headers, and attachments.

## Features

- **High-fidelity HTML rendering** — Preserves CSS styles, layout, and formatting
- **Inline image embedding** — Resolves `cid:` references to embedded base64 images
- **HTML sanitization** — Strips `<script>`, `<iframe>`, `<object>`, `<embed>` and `on*` handlers
- **Batch processing** — Convert entire directories of EML files
- **Attachment extraction** — Optionally save attachments to disk
- **Configurable output** — Page size (A4/Letter/Legal), DPI, header visibility
- **Dual interface** — Works as CLI tool or Python library

## Installation

### System Dependencies (macOS)

WeasyPrint requires a few system libraries for rendering PDF, fonts, and images.

```bash
brew install cairo pango gdk-pixbuf libffi
```

### Python Dependencies

```bash
pip install -r requirements.txt
```

Or install individually:

```bash
pip install weasyprint beautifulsoup4 chardet markdown
```

### System Dependencies (Other Platforms)

- **Linux (Debian/Ubuntu):** `sudo apt-get install libcairo2 libpango-1.0-0 libgdk-pixbuf2.0-0 libffi-dev shared-mime-info`
- **Linux (Fedora):** `sudo dnf install cairo pango gdk-pixbuf2 libffi shared-mime-info`

## Quick Start

```bash
# Convert a single file (using the convert_file subcommand)
python eml2pdf.py convert_file email.eml

# Convert a single file with custom output
python eml2pdf.py convert_file email.eml output.pdf

# Batch convert a directory (using the convert_dir subcommand)
python eml2pdf.py convert_dir ./emails/ ./pdfs/

# Batch with attachments, attachment list in PDF, and smart output naming
python eml2pdf.py convert_dir ./emails/ ./pdfs/ -a -aa --smart-naming

# Hide email headers in output
python eml2pdf.py convert_file email.eml -hh

# Use Letter page size with landscape orientation
python eml2pdf.py convert_file email.eml -p "Letter landscape"
```

## Python Library API

```python
from eml2pdf import EmailToPDFConverter

# Basic conversion
converter = EmailToPDFConverter()
converter.convert("email.eml", "output.pdf")

# Batch conversion with all options
converter = EmailToPDFConverter(
    page_size="a4",
    dpi=300,
    hide_headers=False,
    add_attachment_names=True,
    extract_attachments=True,
    attachments_dir="./extracted",
)
stats = converter.convert_directory("./emails/", "./pdfs/", num_procs=4, smart_naming=True)
print(f"Converted {stats['success']}/{stats['total']} files")
```

## CLI Options

```
Usage: eml2pdf [-V] {convert_file,convert_dir} ...

Subcommands:
  convert_file                Convert a single EML file to a PDF
  convert_dir                 Convert all EML files in a directory

Shared Options (available under subcommands):
  -p, --page SIZE             Page size: a3, a4, a5, b4, b5, letter, legal, ledger (optionally with "landscape") [default: a4]
  -r, --dpi N                 DPI for rendering [default: 300]
  -hh, --hide-headers         Suppress email header block
  -aa, --add-attachment-names Include attachment table with MD5 checksums
  -a, --extract-attachments   Save attachments to disk
  -ad, --attachments-dir PATH Custom directory for attachments
  --unsafe                    Disable HTML sanitization and allow remote resources (external images)
  --skip-ssl-verification     Skip SSL certificate verification for remote fetches
  --max-content-width PX      Max content width in pixels [default: 720]
  -d, --debug-html            Write intermediate HTML next to PDF
  -v, --verbose               Verbose debug output (forces serial mode)
  -q, --quiet                 Suppress all non-error output
```

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Invalid input |
| 2 | Some files failed (batch) |
| 3 | PDF rendering error |
| 99 | Unexpected error |

## Running Tests

```bash
pip install pytest
pytest tests/ -v
```

## Architecture

```
┌────────────────────────────────────────────────────┐
│                    CLI Layer                        │
│              (argparse, logging setup)              │
└────────────────────┬───────────────────────────────┘
                     │
┌────────────────────▼───────────────────────────────┐
│           EmailToPDFConverter (Orchestrator)        │
└────┬──────────────┬──────────────┬─────────────────┘
     │              │              │
     ▼              ▼              ▼
┌─────────┐  ┌─────────────┐  ┌──────────────┐
│EmailParser│  │ HTMLBuilder │  │ PDFRenderer  │
│ (stdlib  │  │(BeautifulSoup)│  │ (WeasyPrint) │
│  email)  │  │              │  │              │
└─────────┘  └─────────────┘  └──────────────┘
```
