"""Shared utilities used by multiple pipeline modules."""

import json
import subprocess
import sys
import os
import re
import time
import urllib.request
import urllib.error
import html as html_module

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


# ─── PDF Download ───────────────────────────────────────────────────────────

def resolve_input(path_or_url):
    """If input is a URL, download the PDF. Returns local file path."""
    if path_or_url.startswith(('http://', 'https://')):
        print(f"Downloading PDF from {path_or_url}...")
        filename = path_or_url.split('/')[-1].split('?')[0]
        if not filename.endswith('.pdf'):
            filename = 'prospectus.pdf'
        local_path = os.path.join('/tmp', filename)

        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
            'Accept': 'application/pdf,*/*',
        }
        req = urllib.request.Request(path_or_url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                with open(local_path, 'wb') as f:
                    f.write(resp.read())
            size_mb = os.path.getsize(local_path) / (1024 * 1024)
            print(f"  Downloaded: {local_path} ({size_mb:.1f} MB)")
        except urllib.error.HTTPError as e:
            print(f"  HTTP Error {e.code}: {e.reason}")
            print("  Tip: Some exchanges block direct downloads. Try downloading manually and passing the local path.")
            sys.exit(1)
        return local_path
    return path_or_url


# ─── PDF Text Extraction ───────────────────────────────────────────────────

def extract_text(pdf_path, start_page=None, end_page=None):
    """Extract text from PDF using pdftotext."""
    cmd = ["pdftotext"]
    if start_page:
        cmd += ["-f", str(start_page)]
    if end_page:
        cmd += ["-l", str(end_page)]
    cmd += [pdf_path, "-"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        # Fallback: try with -layout flag
        cmd.insert(1, "-layout")
        result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout

def get_page_count(pdf_path):
    """Get total page count from PDF."""
    result = subprocess.run(["pdfinfo", pdf_path], capture_output=True, text=True)
    for line in result.stdout.split('\n'):
        if line.startswith('Pages:'):
            return int(line.split(':')[1].strip())
    return 0


# ─── Claude API ─────────────────────────────────────────────────────────────

def call_claude(system_prompt, user_prompt, max_tokens=4096, retries=3):
    """Call Claude API with retry logic."""
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    for attempt in range(retries):
        try:
            msg = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}]
            )
            return msg.content[0].text
        except Exception as e:
            if attempt < retries - 1:
                wait = 2 ** attempt * 5
                print(f"  API error ({e}), retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise

def strip_code_fences(text):
    """Remove markdown code fences from API response."""
    text = text.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1]
    if text.endswith('```'):
        text = text.rsplit('```', 1)[0]
    if text.startswith('json'):
        text = text[4:]
    if text.startswith('html'):
        text = text[4:]
    return text.strip()


def call_claude_vision(system_prompt, pdf_b64, user_prompt, max_tokens=4096, retries=3):
    """Call Claude API with a PDF document as visual input."""
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    for attempt in range(retries):
        try:
            msg = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": pdf_b64
                            }
                        },
                        {
                            "type": "text",
                            "text": user_prompt
                        }
                    ]
                }]
            )
            return msg.content[0].text
        except Exception as e:
            if attempt < retries - 1:
                wait = 2 ** attempt * 5
                print(f"  API error ({e}), retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise


def _call_and_parse_json(system, user, max_tokens=8192):
    """Call Claude and parse JSON response with repair logic."""
    result = call_claude(system, user, max_tokens=max_tokens)
    result = strip_code_fences(result)
    try:
        return json.loads(result)
    except json.JSONDecodeError as e:
        print(f"  Warning: JSON parse error ({e}). Attempting repair...")
        result = re.sub(r',\s*}', '}', result)
        result = re.sub(r',\s*]', ']', result)
        open_braces = result.count('{') - result.count('}')
        open_brackets = result.count('[') - result.count(']')
        result += ']' * open_brackets + '}' * open_braces
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            print(f"  ERROR: Could not parse JSON. First 300 chars:\n{result[:300]}")
            return {}


def _call_and_parse_json_vision(system, pdf_b64, user, max_tokens=8192):
    """Call Claude vision API and parse JSON response with repair logic."""
    result = call_claude_vision(system, pdf_b64, user, max_tokens=max_tokens)
    result = strip_code_fences(result)
    try:
        return json.loads(result)
    except json.JSONDecodeError:
        result = re.sub(r',\s*}', '}', result)
        result = re.sub(r',\s*]', ']', result)
        open_braces = result.count('{') - result.count('}')
        open_brackets = result.count('[') - result.count(']')
        result += ']' * open_brackets + '}' * open_braces
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            print(f"  ERROR: Could not parse vision JSON. First 300 chars:\n{result[:300]}")
            return {}


# ─── PDF Chunk Utilities ────────────────────────────────────────────────────

def split_pdf_to_chunks(pdf_path, pages_per_chunk=50):
    """Split PDF into chunks, returning list of (base64_data, start_page, end_page)."""
    from pypdf import PdfReader, PdfWriter
    import io, base64
    reader = PdfReader(pdf_path)
    total = len(reader.pages)
    chunks = []
    for start in range(0, total, pages_per_chunk):
        end = min(start + pages_per_chunk, total)
        writer = PdfWriter()
        for i in range(start, end):
            writer.add_page(reader.pages[i])
        buf = io.BytesIO()
        writer.write(buf)
        data = buf.getvalue()
        # If chunk > 30MB, reduce size by halving
        if len(data) > 30 * 1024 * 1024 and pages_per_chunk > 10:
            half = pages_per_chunk // 2
            return split_pdf_to_chunks(pdf_path, half)
        b64 = base64.standard_b64encode(data).decode()
        chunks.append((b64, start + 1, end))  # 1-indexed
    return chunks


def find_page_ranges_for_keywords(pdf_path, keywords, context_pages=5):
    """Use pdftotext page-by-page to find which pages contain keywords. Returns page ranges."""
    total_pages = get_page_count(pdf_path)
    hit_pages = set()
    for page in range(1, total_pages + 1):
        text = extract_text(pdf_path, start_page=page, end_page=page)
        for kw in keywords:
            if kw.lower() in text.lower():
                hit_pages.add(page)
                break
    if not hit_pages:
        return []
    # Consolidate into ranges with context padding
    pages = sorted(hit_pages)
    ranges = []
    for p in pages:
        start = max(1, p - context_pages)
        end = min(total_pages, p + context_pages)
        if ranges and start <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], end)
        else:
            ranges.append((start, end))
    return ranges


def _make_pdf_chunk_for_ranges(pdf_path, page_ranges):
    """Create a single base64 PDF from page ranges."""
    from pypdf import PdfReader, PdfWriter
    import io, base64
    reader = PdfReader(pdf_path)
    writer = PdfWriter()
    for start, end in page_ranges:
        for i in range(start - 1, min(end, len(reader.pages))):
            writer.add_page(reader.pages[i])
    buf = io.BytesIO()
    writer.write(buf)
    return base64.standard_b64encode(buf.getvalue()).decode()


# ─── HTML Helpers ───────────────────────────────────────────────────────────

def esc(text):
    """HTML-escape a string."""
    if text is None:
        return ''
    return html_module.escape(str(text))

def fmt_num(val):
    """Format a number with commas, or return 'N/A'."""
    if val is None:
        return 'N/A'
    try:
        return f"{float(val):,.0f}"
    except (ValueError, TypeError):
        return str(val)
