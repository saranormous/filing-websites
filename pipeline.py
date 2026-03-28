#!/usr/bin/env python3
"""Pipeline: Chinese IPO Prospectus PDF → structured data.json + full translation + static site.

Usage:
    python3 pipeline.py <pdf_path_or_url> <output_dir>
    python3 pipeline.py --translate-only <pdf_path_or_url> <output_dir>

Supports:
    - Local PDF files
    - URLs (SSE, HKEX, or direct PDF links — auto-downloaded)
"""

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

# Standard Chinese prospectus section headers (SSE format: "第X节 ...")
SSE_SECTION_PATTERNS = [
    (r'第[一二三四五六七八九十]+[节節]\s*概[览述覽]', 'overview', 'Overview'),
    (r'第[一二三四五六七八九十]+[节節]\s*(?:本次发行|发行概况|發行概況)', 'offering', 'Offering Details'),
    (r'第[一二三四五六七八九十]+[节節]\s*(?:风险因素|風險因素)', 'risks', 'Risk Factors'),
    (r'第[一二三四五六七八九十]+[节節]\s*(?:发行人基本|公司基本|发行人概况|發行人基本)', 'company', 'Company Information'),
    (r'第[一二三四五六七八九十]+[节節]\s*(?:业务与技术|业务和技术|公司业务|業務與技術)', 'business', 'Business & Technology'),
    (r'第[一二三四五六七八九十]+[节節]\s*(?:财务会计|财务|財務會計)', 'financials', 'Financial Information'),
    (r'第[一二三四五六七八九十]+[节節]\s*(?:募集资金|募投|募集資金)', 'proceeds', 'Use of Proceeds'),
    (r'第[一二三四五六七八九十]+[节節]\s*(?:股东|股本|股東)', 'shareholders', 'Shareholders & Equity'),
    (r'第[一二三四五六七八九十]+[节節]\s*(?:公司治理|治理结构|公司治理結構)', 'governance', 'Corporate Governance'),
    (r'第[一二三四五六七八九十]+[节節]\s*(?:其他|附录|附錄)', 'other', 'Other Information'),
]

# HKEX-style section markers (bilingual prospectuses)
# These must match the ENTIRE line (^...$) to avoid mid-paragraph false positives
HKEX_SECTION_PATTERNS = [
    (r'^RISK FACTORS$', 'risks', 'Risk Factors'),
    (r'^風險因素$', 'risks', 'Risk Factors'),
    (r'^BUSINESS$', 'business', 'Business'),
    (r'^業務$', 'business', 'Business'),
    (r'^FINANCIAL INFORMATION$', 'financials', 'Financial Information'),
    (r'^財務資料$', 'financials', 'Financial Information'),
    (r'^USE OF PROCEEDS$', 'proceeds', 'Use of Proceeds'),
    (r'^所得款項用途$', 'proceeds', 'Use of Proceeds'),
    (r'^SHARE CAPITAL$', 'shareholders', 'Share Capital'),
    (r'^股本$', 'shareholders', 'Share Capital'),
    (r'^SUMMARY$', 'overview', 'Summary'),
    (r'^概要$', 'overview', 'Summary'),
    (r'^HISTORY,?\s*REORGANIZATION AND CORPORATE STRUCTURE$', 'company', 'History & Corporate Structure'),
    (r'^歷史、重組及公司架構$', 'company', 'History & Corporate Structure'),
    (r'^DIRECTORS AND SENIOR MANAGEMENT$', 'governance', 'Directors & Senior Management'),
    (r'^董事及高級管理層$', 'governance', 'Directors & Senior Management'),
    (r'^FUTURE PLANS AND USE OF PROCEEDS$', 'proceeds', 'Future Plans & Use of Proceeds'),
    (r'^未來計劃及所得款項用途$', 'proceeds', 'Future Plans & Use of Proceeds'),
    (r'^UNDERWRITING$', 'other', 'Underwriting'),
    (r'^包銷$', 'other', 'Underwriting'),
    (r'^APPENDIX', 'other', 'Appendix'),
    (r'^附錄', 'other', 'Appendix'),
]

SECTION_PATTERNS = SSE_SECTION_PATTERNS + HKEX_SECTION_PATTERNS

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


# ─── Section Detection ──────────────────────────────────────────────────────

def detect_sections(full_text):
    """Split Chinese prospectus text into sections based on standard headers.

    For SSE filings: matches "第X节 ..." patterns (very specific, low false-positive rate).
    For HKEX filings: matches standalone ALL-CAPS headings like "RISK FACTORS" on their own line,
    requiring the line to be short (<80 chars) to avoid mid-paragraph matches.
    """
    lines = full_text.split('\n')
    sections = []
    current_section = {'id': 'preamble', 'title_en': 'Preamble & Cover', 'title_cn': '封面与序言', 'start': 0}

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        # Skip lines that are too long to be section headers
        if len(stripped) > 80:
            continue

        matched = False
        for pattern, sec_id, title_en in SECTION_PATTERNS:
            if re.search(pattern, stripped):
                # For HKEX patterns (^ anchored), the regex handles exactness.
                # For SSE patterns, they're specific enough already.
                # Extra guard: don't create a new section if last one was < 500 chars
                # (prevents splitting on repeated headers in TOC)
                if sections:
                    last_len = i - current_section['start']
                    if last_len < 5:  # Skip if previous "section" was just a few lines (TOC entry)
                        continue

                current_section['end'] = i
                sections.append(current_section)
                current_section = {
                    'id': sec_id,
                    'title_en': title_en,
                    'title_cn': stripped,
                    'start': i
                }
                matched = True
                break

    current_section['end'] = len(lines)
    sections.append(current_section)

    for sec in sections:
        sec['text'] = '\n'.join(lines[sec['start']:sec['end']])
        del sec['start']
        del sec['end']

    # Filter out empty/tiny sections
    sections = [s for s in sections if len(s['text'].strip()) > 100]

    # Merge consecutive sections with same ID (e.g., multiple "risks" fragments)
    merged = []
    for sec in sections:
        if merged and merged[-1]['id'] == sec['id']:
            merged[-1]['text'] += '\n\n' + sec['text']
        else:
            merged.append(sec)

    return merged


# ─── Translation ────────────────────────────────────────────────────────────

def translate_chunk(chunk_text, chunk_num, total_chunks):
    """Translate a single chunk of Chinese text to English."""
    system = """You are a professional translator specializing in Chinese financial and legal documents.
Translate the following Chinese IPO prospectus text into clear, accurate English.

Rules:
- Translate ALL text faithfully and completely — do not summarize or skip content
- Preserve the original structure: headings, numbered lists, table-like data
- Keep Chinese company names in both English and Chinese (e.g. "Unitree Technology (宇树科技)")
- Keep financial figures in their original units (万元, 亿元, etc.) but add English equivalents in parentheses where helpful
- For tables, reproduce them as plain text tables with | separators
- Preserve paragraph breaks
- If text appears to be a header or title, keep it on its own line
- Output ONLY the translated text, no commentary"""

    user = f"Translate this Chinese IPO prospectus text (chunk {chunk_num}/{total_chunks}):\n\n{chunk_text}"
    return call_claude(system, user, max_tokens=8192)


def translate_full_text(pdf_path, output_dir=None):
    """Extract and translate the full prospectus text, section by section.

    Supports resuming: if output_dir contains a partial full_text.json,
    already-translated sections are skipped.
    """
    print("Extracting full text from PDF...")
    full_text = extract_text(pdf_path)
    total_pages = get_page_count(pdf_path)
    print(f"  Total text: {len(full_text)} chars, {total_pages} pages")

    print("Detecting sections...")
    sections = detect_sections(full_text)
    print(f"  Found {len(sections)} sections: {[s['id'] for s in sections]}")

    # Load existing checkpoint if resuming
    checkpoint_path = os.path.join(output_dir, 'full_text.json') if output_dir else None
    translated_sections = []
    resume_from = 0
    if checkpoint_path and os.path.exists(checkpoint_path):
        try:
            with open(checkpoint_path) as f:
                existing = json.load(f)
            translated_sections = existing.get('sections', [])
            resume_from = len(translated_sections)
            total_chars = sum(len(s.get('content', '')) for s in translated_sections)
            print(f"  Resuming from section {resume_from + 1}/{len(sections)} ({total_chars:,} chars already translated)")
        except (json.JSONDecodeError, KeyError):
            print("  Existing checkpoint is corrupt, starting fresh")
            translated_sections = []
            resume_from = 0

    for sec_idx, section in enumerate(sections):
        if sec_idx < resume_from:
            continue  # Already translated

        sec_text = section['text']
        print(f"\nTranslating section {sec_idx+1}/{len(sections)}: {section['title_en']} ({len(sec_text):,} chars)...")

        if len(sec_text) < 200:
            print("  Skipping (too short)")
            translated_sections.append({
                'id': section['id'],
                'title_en': section['title_en'],
                'title_cn': section['title_cn'],
                'content': sec_text  # Keep original short text
            })
            # Checkpoint
            if checkpoint_path:
                _save_checkpoint(checkpoint_path, translated_sections, total_pages)
            continue

        # Split into ~6K char chunks at paragraph boundaries
        chunks = []
        current_chunk = ""
        for para in sec_text.split('\n\n'):
            if len(current_chunk) + len(para) > 6000 and current_chunk:
                chunks.append(current_chunk)
                current_chunk = para
            else:
                current_chunk += ('\n\n' if current_chunk else '') + para
        if current_chunk:
            chunks.append(current_chunk)

        translated_parts = []
        for i, chunk in enumerate(chunks):
            if len(chunk.strip()) < 50:
                translated_parts.append(chunk)
                continue
            print(f"  Chunk {i+1}/{len(chunks)} ({len(chunk):,} chars)...", end=' ', flush=True)
            translated = translate_chunk(chunk, i+1, len(chunks))
            translated_parts.append(translated)
            print(f"→ {len(translated):,} chars")
            if i < len(chunks) - 1:
                time.sleep(1)

        translated_sections.append({
            'id': section['id'],
            'title_en': section['title_en'],
            'title_cn': section['title_cn'],
            'content': '\n\n'.join(translated_parts)
        })

        # Checkpoint after each section
        if checkpoint_path:
            _save_checkpoint(checkpoint_path, translated_sections, total_pages)
            print(f"  ✓ Checkpointed ({len(translated_sections)}/{len(sections)} sections)")

    result = {
        'sections': translated_sections,
        'total_pages': total_pages,
        'translation_model': 'claude-sonnet-4-6',
        'translated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    }
    return result


def _save_checkpoint(path, sections, total_pages):
    """Write translation checkpoint to disk."""
    data = {
        'sections': sections,
        'total_pages': total_pages,
        'translation_model': 'claude-sonnet-4-6',
        'translated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        '_checkpoint': True
    }
    with open(path, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ─── Structured Data Extraction ─────────────────────────────────────────────

def _find_sections_by_keywords(lines, keywords, window=200):
    """Find all text windows around keyword matches."""
    found = []
    used = set()
    for i, line in enumerate(lines):
        for kw in keywords:
            if kw in line and i not in used:
                start = max(0, i - 5)
                end = min(len(lines), i + window)
                if start not in used:
                    used.add(start)
                    found.append('\n'.join(lines[start:end]))
    return found


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


def extract_structured_data(pdf_path):
    """Extract structured data from prospectus using multi-pass targeted extraction.

    Pass 1: Company overview + offering details (first 500 lines)
    Pass 2: Shareholder cap table (search for 持股/percentage patterns)
    Pass 3: Financial tables (search for 营业收入/净利润/资产 patterns)
    Pass 4: Risks + products + use of proceeds
    Then merge all passes into a single data.json.
    """
    print("Extracting text from PDF...")
    full_text = extract_text(pdf_path)
    lines = full_text.split('\n')
    chunk1 = '\n'.join(lines[:500])

    # ── Pass 1: Company overview ──
    print("  Pass 1/4: Company overview & offering...")
    overview = _call_and_parse_json(
        """Extract company overview from this Chinese IPO prospectus. Output JSON:
{
  "meta": { "document": "IPO Prospectus (Draft)", "issuer": "English name", "issuer_cn": "Chinese name", "filing_date": "YYYY-MM-DD", "exchange": "...", "board": "...", "sponsor": "...", "source_language": "Chinese" },
  "company": { "name_en": "...", "name_cn": "...", "founded": "YYYY", "headquarters": "...", "registered_capital_rmb": number, "website": "...", "industry": "...", "controller": { "name": "...", "direct_shares_pct": number, "total_control_pct": number, "roles": ["..."] }, "employees": { "total": number, "rd": number } },
  "offering": { "shares_offered_max": number, "post_ipo_shares_approx": number, "offering_pct_approx": number, "par_value_rmb": number },
  "products": [ { "name": "English name", "type": "...", "specs": "..." } ]
}
Translate to English. Output ONLY valid JSON.""",
        f"Extract from this prospectus overview:\n\n{chunk1[:15000]}",
        max_tokens=4096
    )

    # ── Pass 2: Shareholders ──
    print("  Pass 2/4: Shareholders & cap table...")
    sh_keywords = ['持股比例', '持股数', '股东名称', '股份比例', '持股', '股東', '% of shares', 'Shareholding']
    sh_chunks = _find_sections_by_keywords(lines, sh_keywords, window=300)
    sh_text = '\n\n---\n\n'.join(sh_chunks[:5])[:20000]
    shareholders_data = {}
    if sh_text:
        shareholders_data = _call_and_parse_json(
            """Extract ALL shareholders with percentages from this Chinese IPO prospectus.
Output JSON: { "shareholders_pre_ipo": [{ "name": "English name", "shares_10k": number_or_null, "pct": number, "type": "Individual|VC|Institution" }] }
Include EVERY shareholder listed. Translate names to English. Output ONLY valid JSON.""",
            sh_text,
            max_tokens=8192
        )

    # ── Pass 3: Financials ──
    print("  Pass 3/4: Financial tables...")
    fin_keywords = ['营业收入', '净利润', '总资产', '净资产', '毛利率', '利润表', '资产负债', '现金流量',
                    '營業收入', '淨利潤', '總資產', 'Revenue', 'Net profit', 'Total assets']
    fin_chunks = _find_sections_by_keywords(lines, fin_keywords, window=100)
    fin_text = '\n\n---\n\n'.join(fin_chunks[:8])[:20000]
    financials_data = {}
    if fin_text:
        financials_data = _call_and_parse_json(
            """Extract ALL financial data from these Chinese IPO prospectus tables. Output JSON:
{
  "financials": {
    "currency": "RMB", "unit": "10K (万元)",
    "income_statement": [{ "period": "2022", "revenue": number_万, "net_profit": number_万, "gross_margin_pct": number, "rd_expense": number_万 }],
    "balance_sheet": [{ "date": "YYYY-12-31", "total_assets": number_万, "total_liabilities": number_万, "equity": number_万, "cash": number_万 }],
    "cash_flow": [{ "period": "2022", "operating": number_万, "investing": number_万, "financing": number_万 }]
  },
  "revenue_breakdown": {
    "by_product": [{ "product": "English name", "2022": number_万, "2023": number_万, "2024": number_万 }],
    "by_geography_pct": [{ "region": "Domestic", "2022": number, "2023": number, "2024": number }]
  }
}
Extract EVERY number. Use 万元 units. Output ONLY valid JSON.""",
            fin_text,
            max_tokens=8192
        )

    # ── Pass 4: Risks + proceeds ──
    print("  Pass 4/4: Risks & use of proceeds...")
    risk_keywords = ['风险因素', '風險因素', 'RISK FACTORS', '募集资金', '募投项目', '所得款項']
    risk_chunks = _find_sections_by_keywords(lines, risk_keywords, window=150)
    risk_text = '\n\n---\n\n'.join(risk_chunks[:5])[:15000]
    risks_data = {}
    if risk_text:
        risks_data = _call_and_parse_json(
            """Extract risk factors and use of proceeds from this Chinese IPO prospectus. Output JSON:
{
  "key_risks": ["Risk 1 in English", "Risk 2", ...],
  "use_of_proceeds": { "total_rmb_10k": number, "projects": [{ "name": "English name", "amount_rmb_10k": number, "focus": "..." }] }
}
Translate to English. Output ONLY valid JSON.""",
            risk_text,
            max_tokens=4096
        )

    # ── Merge all passes ──
    print("  Merging passes...")
    merged = overview or {}
    if shareholders_data.get('shareholders_pre_ipo'):
        merged['shareholders_pre_ipo'] = shareholders_data['shareholders_pre_ipo']
    if financials_data.get('financials'):
        merged['financials'] = financials_data['financials']
    if financials_data.get('revenue_breakdown'):
        merged['revenue_breakdown'] = financials_data['revenue_breakdown']
    if risks_data.get('key_risks'):
        merged['key_risks'] = risks_data['key_risks']
    if risks_data.get('use_of_proceeds'):
        merged['use_of_proceeds'] = risks_data['use_of_proceeds']

    # Ensure required fields exist
    merged.setdefault('meta', {})
    merged.setdefault('company', {})
    merged.setdefault('offering', {})
    merged.setdefault('financials', {'currency': 'RMB', 'unit': '10K (万元)', 'income_statement': [], 'balance_sheet': [], 'cash_flow': []})
    merged.setdefault('shareholders_pre_ipo', [])
    merged.setdefault('key_risks', [])
    merged.setdefault('use_of_proceeds', {'projects': []})
    merged.setdefault('products', [])
    merged['meta']['translation_note'] = 'AI-translated for reference only'

    # Report what we got
    sh_count = len(merged.get('shareholders_pre_ipo', []))
    fin_count = len(merged.get('financials', {}).get('income_statement', []))
    risk_count = len(merged.get('key_risks', []))
    sh_with_pct = sum(1 for s in merged.get('shareholders_pre_ipo', []) if s.get('pct'))
    print(f"  Result: {sh_count} shareholders ({sh_with_pct} with %), {fin_count} income periods, {risk_count} risks")

    return merged


# ─── Vision-Based Extraction ─────────────────────────────────────────────────

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


def extract_structured_data_vision(pdf_path):
    """Extract structured data using vision — Claude reads the actual PDF pages.

    Same 4-pass structure and output schema as extract_structured_data,
    but sends PDF page images instead of pdftotext output.
    """
    print("Vision extraction: reading PDF pages directly...")
    total_pages = get_page_count(pdf_path)
    print(f"  {total_pages} pages")

    # ── Pass 1: Overview (pages 1-50) ──
    print("  Vision Pass 1/4: Company overview & offering (pages 1-50)...")
    overview_chunks = split_pdf_to_chunks(pdf_path, pages_per_chunk=50)
    overview_b64 = overview_chunks[0][0] if overview_chunks else None
    overview = {}
    if overview_b64:
        overview = _call_and_parse_json_vision(
            """You are reading an IPO prospectus document. You can see the actual pages with all tables, charts, and formatting.
Extract company overview. Output JSON:
{
  "meta": { "document": "IPO Prospectus (Draft)", "issuer": "English name", "issuer_cn": "Chinese name", "filing_date": "YYYY-MM-DD", "exchange": "...", "board": "...", "sponsor": "...", "source_language": "Chinese" },
  "company": { "name_en": "...", "name_cn": "...", "founded": "YYYY", "headquarters": "...", "registered_capital_rmb": number, "website": "...", "industry": "...", "controller": { "name": "...", "direct_shares_pct": number, "total_control_pct": number, "roles": ["..."] }, "employees": { "total": number, "rd": number } },
  "offering": { "shares_offered_max": number, "post_ipo_shares_approx": number, "offering_pct_approx": number, "par_value_rmb": number },
  "products": [ { "name": "English name", "type": "...", "specs": "..." } ]
}
Translate all Chinese text to English. Output ONLY valid JSON.""",
            overview_b64,
            "Extract company overview, offering details, and products from these prospectus pages. Read all visible tables and data.",
            max_tokens=4096
        )
    time.sleep(2)

    # ── Pass 2: Shareholders (targeted pages) ──
    print("  Vision Pass 2/4: Shareholders & cap table...")
    sh_keywords = ['持股比例', '持股数', '股东名称', '股份比例', '持股', '股東', 'Shareholding', '% of shares']
    sh_ranges = find_page_ranges_for_keywords(pdf_path, sh_keywords, context_pages=3)
    shareholders_data = {}
    if sh_ranges:
        total_sh_pages = sum(e - s + 1 for s, e in sh_ranges)
        print(f"    Found shareholder data on pages: {sh_ranges} ({total_sh_pages} pages)")
        sh_b64 = _make_pdf_chunk_for_ranges(pdf_path, sh_ranges[:5])  # Limit to first 5 ranges
        shareholders_data = _call_and_parse_json_vision(
            """You are reading IPO prospectus pages that contain shareholder/cap table information.
You can see the actual tables with all rows and columns. Read them exactly as displayed.
Output JSON: { "shareholders_pre_ipo": [{ "name": "English name (translate Chinese)", "shares_10k": number_or_null, "pct": number, "type": "Individual|VC|Institution" }] }
Include EVERY shareholder row visible in the tables. Extract exact percentage values from the table columns. Output ONLY valid JSON.""",
            sh_b64,
            "Extract ALL shareholders with their exact shareholding percentages from the tables visible on these pages.",
            max_tokens=8192
        )
    else:
        print("    No shareholder keywords found")
    time.sleep(2)

    # ── Pass 3: Financials (targeted pages) ──
    print("  Vision Pass 3/4: Financial tables...")
    fin_keywords = ['营业收入', '净利润', '总资产', '利润表', '资产负债', '现金流量',
                    '營業收入', '淨利潤', '總資產', 'Revenue', 'Net profit', 'Total assets', 'Income Statement']
    fin_ranges = find_page_ranges_for_keywords(pdf_path, fin_keywords, context_pages=3)
    financials_data = {}
    if fin_ranges:
        total_fin_pages = sum(e - s + 1 for s, e in fin_ranges)
        print(f"    Found financial data on pages: {fin_ranges} ({total_fin_pages} pages)")
        # Limit to first 8 ranges to stay under API limits
        fin_b64 = _make_pdf_chunk_for_ranges(pdf_path, fin_ranges[:8])
        financials_data = _call_and_parse_json_vision(
            """You are reading IPO prospectus pages that contain financial tables and statements.
You can see the actual tables with all rows, columns, and numbers. Read them exactly as displayed.
Output JSON:
{
  "financials": {
    "currency": "RMB", "unit": "10K (万元)",
    "income_statement": [{ "period": "2022", "revenue": number_万, "net_profit": number_万, "gross_margin_pct": number, "rd_expense": number_万 }],
    "balance_sheet": [{ "date": "YYYY-12-31", "total_assets": number_万, "total_liabilities": number_万, "equity": number_万, "cash": number_万 }],
    "cash_flow": [{ "period": "2022", "operating": number_万, "investing": number_万, "financing": number_万 }]
  },
  "revenue_breakdown": {
    "by_product": [{ "product": "English name", "2022": number_万, "2023": number_万, "2024": number_万 }],
    "by_geography_pct": [{ "region": "Domestic", "2022": number, "2023": number, "2024": number }]
  }
}
Read EVERY number from the financial tables exactly as printed. Determine the correct unit from the table headers. Output ONLY valid JSON.""",
            fin_b64,
            "Extract ALL financial data from the tables visible on these pages. Read every row and column.",
            max_tokens=8192
        )
    else:
        print("    No financial keywords found")
    time.sleep(2)

    # ── Pass 4: Risks + proceeds ──
    print("  Vision Pass 4/4: Risks & use of proceeds...")
    risk_keywords = ['风险因素', '風險因素', 'RISK FACTORS', '募集资金', '募投项目', '所得款項', 'Use of Proceeds']
    risk_ranges = find_page_ranges_for_keywords(pdf_path, risk_keywords, context_pages=3)
    risks_data = {}
    if risk_ranges:
        total_risk_pages = sum(e - s + 1 for s, e in risk_ranges)
        print(f"    Found risk/proceeds data on pages: {risk_ranges} ({total_risk_pages} pages)")
        risk_b64 = _make_pdf_chunk_for_ranges(pdf_path, risk_ranges[:5])
        risks_data = _call_and_parse_json_vision(
            """You are reading IPO prospectus pages containing risk factors and use of proceeds.
Extract all risk factors and proceeds allocation. Output JSON:
{
  "key_risks": ["Risk 1 in English", "Risk 2", ...],
  "use_of_proceeds": { "total_rmb_10k": number, "projects": [{ "name": "English name", "amount_rmb_10k": number, "focus": "..." }] }
}
Translate all Chinese to English. Output ONLY valid JSON.""",
            risk_b64,
            "Extract all risk factors and use of proceeds from these pages.",
            max_tokens=4096
        )
    else:
        print("    No risk keywords found")

    # ── Merge ──
    print("  Merging vision passes...")
    merged = overview or {}
    if shareholders_data.get('shareholders_pre_ipo'):
        merged['shareholders_pre_ipo'] = shareholders_data['shareholders_pre_ipo']
    if financials_data.get('financials'):
        merged['financials'] = financials_data['financials']
    if financials_data.get('revenue_breakdown'):
        merged['revenue_breakdown'] = financials_data['revenue_breakdown']
    if risks_data.get('key_risks'):
        merged['key_risks'] = risks_data['key_risks']
    if risks_data.get('use_of_proceeds'):
        merged['use_of_proceeds'] = risks_data['use_of_proceeds']

    merged.setdefault('meta', {})
    merged.setdefault('company', {})
    merged.setdefault('offering', {})
    merged.setdefault('financials', {'currency': 'RMB', 'unit': '10K (万元)', 'income_statement': [], 'balance_sheet': [], 'cash_flow': []})
    merged.setdefault('shareholders_pre_ipo', [])
    merged.setdefault('key_risks', [])
    merged.setdefault('use_of_proceeds', {'projects': []})
    merged.setdefault('products', [])
    merged['meta']['translation_note'] = 'AI-translated for reference only'
    merged['meta']['extraction_method'] = 'vision'

    sh_count = len(merged.get('shareholders_pre_ipo', []))
    fin_count = len(merged.get('financials', {}).get('income_statement', []))
    risk_count = len(merged.get('key_risks', []))
    sh_with_pct = sum(1 for s in merged.get('shareholders_pre_ipo', []) if s.get('pct'))
    print(f"  Vision result: {sh_count} shareholders ({sh_with_pct} with %), {fin_count} income periods, {risk_count} risks")

    return merged


def extract_structured_data_reducto(pdf_path):
    """Extract structured data using Reducto for PDF parsing, then Claude for structuring.

    Reducto handles the PDF → structured text conversion (preserving tables),
    then we send that structured text to Claude for the same 4-pass extraction.
    """
    print("Reducto extraction: parsing PDF with Reducto API...")
    from pathlib import Path
    from reducto import Reducto

    reducto_key = os.environ.get('REDUCTO_API_KEY', '')
    if not reducto_key:
        print("  ERROR: REDUCTO_API_KEY not set")
        return {}

    client = Reducto(api_key=reducto_key)
    upload = client.upload(file=Path(pdf_path))
    result = client.parse.run(input=upload)

    print(f"  Parsed: {result.usage.num_pages} pages, {result.usage.credits} credits")

    # Fetch result from URL (gzipped JSON)
    import gzip
    result_url = result.result.url
    with urllib.request.urlopen(result_url) as resp:
        raw = resp.read()
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
        parsed = json.loads(raw)

    # Collect all blocks with page info for targeted selection
    all_blocks = []
    full_text = ''
    for chunk in parsed.get('chunks', []):
        full_text += chunk.get('content', '') + '\n\n'
        for block in chunk.get('blocks', []):
            page = block.get('bbox', {}).get('page', 0) if isinstance(block.get('bbox'), dict) else 0
            all_blocks.append({
                'type': block.get('type', ''),
                'content': block.get('content', ''),
                'page': page
            })

    tables = [b for b in all_blocks if b['type'] == 'Table']
    text_blocks = [b for b in all_blocks if b['type'] in ('Text', 'Section Header', 'Title', 'List Item')]
    print(f"  Total text: {len(full_text):,} chars, {len(tables)} tables, {len(all_blocks)} blocks")

    def _find_nearby_tables(keywords, context_pages=3):
        """Find tables on pages near keyword matches in text blocks."""
        hit_pages = set()
        for b in text_blocks:
            for kw in keywords:
                if kw.lower() in b['content'].lower():
                    hit_pages.add(b['page'])
                    break
        # Include tables on nearby pages
        nearby = set()
        for p in hit_pages:
            for offset in range(-context_pages, context_pages + 1):
                nearby.add(p + offset)
        return [t for t in tables if t['page'] in nearby]

    def _get_text_near_keywords(keywords, window=200):
        """Get text blocks near keyword matches."""
        lines = full_text.split('\n')
        return _find_sections_by_keywords(lines, keywords, window=window)

    chunk1 = full_text[:15000]

    # ── Pass 1: Overview (first ~50 pages of text) ──
    print("  Reducto Pass 1/4: Company overview...")
    early_blocks = [b for b in all_blocks if b['page'] <= 50]
    early_text = '\n'.join(b['content'] for b in early_blocks)[:15000]
    overview = _call_and_parse_json(
        """You are an expert financial analyst. This text was extracted from an IPO prospectus by a document parser that preserves table structure.
Extract company overview. Output JSON:
{
  "meta": { "document": "IPO Prospectus (Draft)", "issuer": "English name", "issuer_cn": "Chinese name", "filing_date": "YYYY-MM-DD", "exchange": "...", "board": "...", "sponsor": "...", "source_language": "Chinese" },
  "company": { "name_en": "...", "name_cn": "...", "founded": "YYYY", "headquarters": "...", "registered_capital_rmb": number, "website": "...", "industry": "...", "controller": { "name": "...", "direct_shares_pct": number, "total_control_pct": number, "roles": ["..."] }, "employees": { "total": number, "rd": number } },
  "offering": { "shares_offered_max": number, "post_ipo_shares_approx": number, "offering_pct_approx": number, "par_value_rmb": number },
  "products": [ { "name": "English name", "type": "...", "specs": "..." } ]
}
Translate to English. Output ONLY valid JSON.""",
        f"Extract from this prospectus (table structure preserved by Reducto):\n\n{early_text}",
        max_tokens=4096
    )

    # ── Pass 2: Shareholders (find the company cap table, not fund tables) ──
    print("  Reducto Pass 2/4: Shareholders...")
    # Use controller name from Pass 1 to find the actual company cap table
    controller_name = (overview or {}).get('company', {}).get('controller', {}).get('name', '')
    controller_cn = ''
    # Also get Chinese controller name from company name_cn
    company_cn = (overview or {}).get('company', {}).get('name_cn', '')

    # Find tables that: (1) have 持股比例/% columns AND (2) contain the controller or company name
    # This distinguishes the company cap table from fund composition tables
    cap_tables = []
    for t in tables:
        content = t['content']
        pct_count = content.count('%')
        has_cap_headers = '持股比例' in content or ('股东' in content and '比例' in content)
        # Must have controller name or company name to be the right table
        has_company_ref = ('王兴兴' in content or '宇树' in content or  # Unitree-specific fallbacks
                          (controller_name and controller_name.lower() in content.lower()) or
                          any(cn in content for cn in ['实际控制人', '控股股东', '发行人']))
        if pct_count >= 3 and has_cap_headers and has_company_ref:
            cap_tables.append(t)

    # Fallback: if no company-specific tables found, use all tables with 持股比例
    if not cap_tables:
        cap_tables = [t for t in tables if '持股比例' in t['content'] and t['content'].count('%') >= 5]

    sh_context = _get_text_near_keywords(['持股比例', '股东名称', '发行人股本'], window=100)
    print(f"    Found {len(cap_tables)} company cap tables")
    shareholders_data = {}
    if cap_tables:
        sh_input = 'COMPANY SHAREHOLDER CAP TABLES:\n\n'
        for t in cap_tables[:10]:
            sh_input += f'[Page {t["page"]}]\n{t["content"]}\n\n---\n\n'
        sh_input += '\nCONTEXT:\n\n' + '\n\n'.join(sh_context[:2])[:5000]
        shareholders_data = _call_and_parse_json(
            """You are given the company's shareholder cap tables from an IPO prospectus.
These tables show who owns shares in the ISSUER COMPANY (not in investment funds).
There may be multiple historical tables — use the one with the MOST shareholders (the most complete/recent one).
Extract ALL shareholders with their exact percentages.
Output JSON: { "shareholders_pre_ipo": [{ "name": "English name (translate Chinese)", "shares_10k": number_or_null, "pct": number, "type": "Individual|VC|Institution" }] }
Output ONLY valid JSON.""",
            sh_input[:20000],
            max_tokens=8192
        )

    # ── Pass 3: Financials (targeted tables near financial keywords) ──
    print("  Reducto Pass 3/4: Financial tables...")
    fin_keywords = ['营业收入', '净利润', '总资产', '利润表', '资产负债', '现金流量',
                    'Revenue', 'Net profit', 'Total assets', 'Income Statement']
    fin_nearby_tables = _find_nearby_tables(fin_keywords, context_pages=3)
    fin_context = _get_text_near_keywords(fin_keywords, window=50)
    print(f"    Found {len(fin_nearby_tables)} tables near financial keywords")
    financials_data = {}
    if fin_nearby_tables:
        fin_input = 'TABLES NEAR FINANCIAL SECTIONS (page numbers shown):\n\n'
        for t in fin_nearby_tables[:20]:
            fin_input += f'[Page {t["page"]}]\n{t["content"]}\n\n---\n\n'
        fin_input += '\nCONTEXT:\n\n' + '\n\n'.join(fin_context[:3])[:5000]
        financials_data = _call_and_parse_json(
            """You are given tables from an IPO prospectus, filtered to pages near financial statement sections.
Find the income statement, balance sheet, and cash flow tables. Ignore non-financial tables (definitions, TOC, etc.).
Output JSON:
{
  "financials": {
    "currency": "RMB", "unit": "10K (万元)",
    "income_statement": [{ "period": "2022", "revenue": number_万, "net_profit": number_万, "gross_margin_pct": number, "rd_expense": number_万 }],
    "balance_sheet": [{ "date": "YYYY-12-31", "total_assets": number_万, "total_liabilities": number_万, "equity": number_万, "cash": number_万 }],
    "cash_flow": [{ "period": "2022", "operating": number_万, "investing": number_万, "financing": number_万 }]
  },
  "revenue_breakdown": {
    "by_product": [{ "product": "English name", "2022": number_万, "2023": number_万, "2024": number_万 }],
    "by_geography_pct": [{ "region": "Domestic", "2022": number, "2023": number, "2024": number }]
  }
}
Extract EVERY number from the financial tables. Output ONLY valid JSON.""",
            fin_input[:25000],
            max_tokens=8192
        )

    # ── Pass 4: Risks ──
    print("  Reducto Pass 4/4: Risks & proceeds...")
    risk_keywords = ['风险因素', '風險因素', 'RISK FACTORS', '募集资金', '所得款項']
    risk_context = _get_text_near_keywords(risk_keywords, window=150)
    risk_text = '\n\n---\n\n'.join(risk_context[:5])[:15000]
    risks_data = {}
    if risk_text:
        risks_data = _call_and_parse_json(
            """Extract risk factors and use of proceeds from this IPO prospectus text. Output JSON:
{
  "key_risks": ["Risk 1 in English", ...],
  "use_of_proceeds": { "total_rmb_10k": number, "projects": [{ "name": "English name", "amount_rmb_10k": number, "focus": "..." }] }
}
Translate to English. Output ONLY valid JSON.""",
            risk_text,
            max_tokens=4096
        )

    # Merge
    print("  Merging Reducto passes...")
    merged = overview or {}
    if shareholders_data.get('shareholders_pre_ipo'):
        merged['shareholders_pre_ipo'] = shareholders_data['shareholders_pre_ipo']
    if financials_data.get('financials'):
        merged['financials'] = financials_data['financials']
    if financials_data.get('revenue_breakdown'):
        merged['revenue_breakdown'] = financials_data['revenue_breakdown']
    if risks_data.get('key_risks'):
        merged['key_risks'] = risks_data['key_risks']
    if risks_data.get('use_of_proceeds'):
        merged['use_of_proceeds'] = risks_data['use_of_proceeds']

    merged.setdefault('meta', {})
    merged.setdefault('company', {})
    merged.setdefault('offering', {})
    merged.setdefault('financials', {'currency': 'RMB', 'unit': '10K (万元)', 'income_statement': [], 'balance_sheet': [], 'cash_flow': []})
    merged.setdefault('shareholders_pre_ipo', [])
    merged.setdefault('key_risks', [])
    merged.setdefault('use_of_proceeds', {'projects': []})
    merged.setdefault('products', [])
    merged['meta']['translation_note'] = 'AI-translated for reference only'
    merged['meta']['extraction_method'] = 'reducto'

    sh_count = len(merged.get('shareholders_pre_ipo', []))
    fin_count = len(merged.get('financials', {}).get('income_statement', []))
    risk_count = len(merged.get('key_risks', []))
    sh_with_pct = sum(1 for s in merged.get('shareholders_pre_ipo', []) if s.get('pct'))
    print(f"  Reducto result: {sh_count} shareholders ({sh_with_pct} with %), {fin_count} income periods, {risk_count} risks")

    return merged


def compare_extractions(text_data, vision_data):
    """Compare text-based and vision-based extraction results. Returns report dict."""
    report = {'dimensions': []}

    def _compare_field(name, text_val, vision_val):
        winner = 'tie'
        if text_val and not vision_val:
            winner = 'text'
        elif vision_val and not text_val:
            winner = 'vision'
        elif text_val and vision_val and text_val != vision_val:
            winner = 'different'
        report['dimensions'].append({
            'field': name, 'text': text_val, 'vision': vision_val, 'winner': winner
        })

    # Shareholders
    text_sh = text_data.get('shareholders_pre_ipo', [])
    vision_sh = vision_data.get('shareholders_pre_ipo', [])
    text_sh_pct = sum(1 for s in text_sh if s.get('pct'))
    vision_sh_pct = sum(1 for s in vision_sh if s.get('pct'))
    _compare_field('shareholders_count', len(text_sh), len(vision_sh))
    _compare_field('shareholders_with_pct', text_sh_pct, vision_sh_pct)
    sh_winner = 'vision' if vision_sh_pct > text_sh_pct else ('text' if text_sh_pct > vision_sh_pct else 'tie')
    report['dimensions'][-1]['winner'] = sh_winner

    # Financials
    text_fin = text_data.get('financials', {})
    vision_fin = vision_data.get('financials', {})
    text_inc = text_fin.get('income_statement', [])
    vision_inc = vision_fin.get('income_statement', [])
    text_bs = text_fin.get('balance_sheet', [])
    vision_bs = vision_fin.get('balance_sheet', [])
    text_rev_count = sum(1 for r in text_inc if r.get('revenue'))
    vision_rev_count = sum(1 for r in vision_inc if r.get('revenue'))
    _compare_field('income_periods', len(text_inc), len(vision_inc))
    _compare_field('periods_with_revenue', text_rev_count, vision_rev_count)
    _compare_field('balance_sheet_periods', len(text_bs), len(vision_bs))

    # Compare specific revenue values where both have data
    text_revs = {r.get('period'): r.get('revenue') for r in text_inc if r.get('revenue')}
    vision_revs = {r.get('period'): r.get('revenue') for r in vision_inc if r.get('revenue')}
    common_periods = set(text_revs.keys()) & set(vision_revs.keys())
    if common_periods:
        matches = sum(1 for p in common_periods if abs(text_revs[p] - vision_revs[p]) < text_revs[p] * 0.05)
        _compare_field('revenue_value_matches', f'{matches}/{len(common_periods)}', f'{matches}/{len(common_periods)}')

    # Metadata completeness
    text_meta_fields = sum(1 for v in [
        text_data.get('company', {}).get('founded'),
        text_data.get('company', {}).get('controller', {}).get('name'),
        text_data.get('company', {}).get('headquarters'),
        text_data.get('company', {}).get('industry'),
        text_data.get('meta', {}).get('filing_date'),
    ] if v)
    vision_meta_fields = sum(1 for v in [
        vision_data.get('company', {}).get('founded'),
        vision_data.get('company', {}).get('controller', {}).get('name'),
        vision_data.get('company', {}).get('headquarters'),
        vision_data.get('company', {}).get('industry'),
        vision_data.get('meta', {}).get('filing_date'),
    ] if v)
    _compare_field('metadata_completeness', f'{text_meta_fields}/5', f'{vision_meta_fields}/5')

    # Risks
    text_risks = len(text_data.get('key_risks', []))
    vision_risks = len(vision_data.get('key_risks', []))
    _compare_field('risk_count', text_risks, vision_risks)

    # Products
    text_prods = len(text_data.get('products', []))
    vision_prods = len(vision_data.get('products', []))
    _compare_field('product_count', text_prods, vision_prods)

    # Overall winner
    text_wins = sum(1 for d in report['dimensions'] if d['winner'] == 'text')
    vision_wins = sum(1 for d in report['dimensions'] if d['winner'] == 'vision')
    report['summary'] = {
        'text_wins': text_wins,
        'vision_wins': vision_wins,
        'ties': len(report['dimensions']) - text_wins - vision_wins,
        'overall': 'vision' if vision_wins > text_wins else ('text' if text_wins > vision_wins else 'tie')
    }

    return report


def print_eval_report(report):
    """Print a human-readable eval comparison report."""
    print("\n" + "=" * 60)
    print("  EXTRACTION QUALITY COMPARISON: TEXT vs VISION")
    print("=" * 60)
    for d in report['dimensions']:
        winner_mark = {'text': '← TEXT', 'vision': 'VISION →', 'tie': '  TIE  ', 'different': ' DIFF  '}
        print(f"  {d['field']:30s}  {str(d['text']):>10s}  {winner_mark.get(d['winner'], ''):^9s}  {str(d['vision']):<10s}")
    s = report['summary']
    print("-" * 60)
    print(f"  Text wins: {s['text_wins']}  |  Vision wins: {s['vision_wins']}  |  Ties: {s['ties']}")
    print(f"  Overall winner: {s['overall'].upper()}")
    print("=" * 60)


# ─── Executive Summary ───────────────────────────────────────────────────────

def generate_executive_summary(data, full_text_data=None):
    """Generate a narrative executive summary from structured data + translation."""
    print("Generating executive summary...")

    # Build context from data.json
    context = json.dumps(data, ensure_ascii=False, default=str)[:12000]

    # Add first section of translation if available
    translation_excerpt = ''
    if full_text_data and full_text_data.get('sections'):
        for sec in full_text_data['sections'][:3]:
            translation_excerpt += sec.get('content', '')[:3000] + '\n\n'
        translation_excerpt = translation_excerpt[:6000]

    result = call_claude(
        """You write concise, insightful executive summaries of IPO prospectuses for investors.
Write a 150-200 word executive summary covering:
1. What the company does (one sentence)
2. Key financial metrics (revenue, margins, growth)
3. What they're raising and where they're listing
4. Who the key shareholders/investors are
5. What they'll use the money for
6. The 2-3 biggest risks

IMPORTANT: Convert ALL monetary figures to USD using 1 RMB = $0.138 (i.e. divide RMB by 7.25). Always show dollar amounts (e.g. "$161M revenue" not "RMB 1.17B"). For HKD, use 1 HKD = $0.128.
Note: if the data uses 万元 (10K RMB) units, multiply by 10,000 first to get full RMB, then convert to USD.

Be specific with numbers. Write in plain English, no jargon. No markdown formatting — just clean paragraphs.
Output ONLY the summary text, nothing else.""",
        f"Prospectus data:\n{context}\n\nTranslation excerpt:\n{translation_excerpt}",
        max_tokens=1024
    )

    return result.strip()


# ─── Validation ──────────────────────────────────────────────────────────────

def validate_data(data):
    """Validate and auto-fix extracted data. Returns True if no unfixable issues remain."""
    warnings = []
    fixes = []

    # ── Meta ──
    if not data.get('meta', {}).get('issuer'):
        warnings.append("Missing company name (meta.issuer)")
    if not data.get('meta', {}).get('filing_date'):
        warnings.append("Missing filing date")

    # ── Company ──
    co = data.get('company', {})
    if not co.get('founded'):
        warnings.append("Missing founded date")
    if not co.get('controller', {}).get('name'):
        warnings.append("Missing controller/founder name")

    # ── Financials ──
    fin = data.get('financials', {})
    if not fin.get('income_statement'):
        warnings.append("No income statement (may be expected for draft HKEX filings)")
    else:
        for row in fin['income_statement']:
            period = row.get('period', '?')
            rev = row.get('revenue')
            gm = row.get('gross_margin_pct')

            # Fix: negative revenue
            if rev is not None and rev < 0:
                fixes.append(f"Negative revenue {rev} in {period} → set to None")
                row['revenue'] = None

            # Fix: 100% gross margin on near-zero revenue (meaningless)
            if gm is not None and gm >= 100 and rev is not None and rev < 1:
                fixes.append(f"100% gross margin on near-zero revenue in {period} → set to None")
                row['gross_margin_pct'] = None

            # Fix: extreme gross margins
            if gm is not None and (gm > 99 or gm < -500):
                fixes.append(f"Extreme gross margin {gm}% in {period} → set to None")
                row['gross_margin_pct'] = None

    if not fin.get('balance_sheet'):
        warnings.append("No balance sheet data")

    # ── Shareholders ──
    sh = data.get('shareholders_pre_ipo', [])
    if not sh:
        warnings.append("No shareholders extracted")
    else:
        sh_with_pct = [s for s in sh if s.get('pct')]
        total_pct = sum(s['pct'] for s in sh_with_pct)

        # Fix: remove catch-all "Other" entries that cause double counting
        if total_pct > 110:
            before = len(sh)
            data['shareholders_pre_ipo'] = [
                s for s in sh
                if not any(kw in s.get('name', '').lower() for kw in ['other pre-ipo', 'other investors', 'other holders', 'public', 'other shareholders'])
            ]
            removed = before - len(data['shareholders_pre_ipo'])
            if removed:
                new_total = sum(s.get('pct', 0) for s in data['shareholders_pre_ipo'] if s.get('pct'))
                fixes.append(f"Removed {removed} catch-all shareholder entries (total was {total_pct:.0f}%, now {new_total:.0f}%)")
            else:
                warnings.append(f"Shareholder total {total_pct:.0f}% > 110% — possible double counting")

        # Fix: remove duplicate shareholders
        seen_names = set()
        deduped = []
        for s in data.get('shareholders_pre_ipo', []):
            name_key = s.get('name', '').lower().strip()
            if name_key and name_key in seen_names:
                fixes.append(f"Removed duplicate shareholder: {s.get('name', '')[:30]}")
            else:
                seen_names.add(name_key)
                deduped.append(s)
        data['shareholders_pre_ipo'] = deduped

        # Fix: shareholder with > 80% likely wrong
        for s in data.get('shareholders_pre_ipo', []):
            if s.get('pct') and s['pct'] > 80:
                warnings.append(f"Shareholder '{s.get('name', '?')[:25]}' at {s['pct']}% — verify")

    # ── Risks ──
    if not data.get('key_risks'):
        warnings.append("No key risks extracted")
    elif len(data.get('key_risks', [])) < 3:
        warnings.append(f"Only {len(data['key_risks'])} risks — most prospectuses have many more")

    # Fix: remove very short risks (likely extraction artifacts)
    if data.get('key_risks'):
        before = len(data['key_risks'])
        data['key_risks'] = [r for r in data['key_risks'] if len(r) >= 20]
        removed = before - len(data['key_risks'])
        if removed:
            fixes.append(f"Removed {removed} suspiciously short risk entries")

    # ── Products ──
    if not data.get('products'):
        warnings.append("No products extracted")

    # ── Report ──
    for f in fixes:
        print(f"  ✓ Auto-fixed: {f}")
    for w in warnings:
        print(f"  ⚠ {w}")

    return len(warnings) == 0


def validate_full_text(full_text_data):
    """Validate full translation output."""
    issues = []
    sections = full_text_data.get('sections', [])

    if len(sections) == 0:
        issues.append("No sections translated")
    else:
        total_chars = sum(len(s.get('content', '')) for s in sections)
        if total_chars < 1000:
            issues.append(f"Translation suspiciously short ({total_chars} chars)")

        for s in sections:
            content = s.get('content', '')
            # Check for untranslated Chinese remaining (more than 20% Chinese chars = likely untranslated)
            chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', content))
            total_chars_sec = len(content)
            if total_chars_sec > 0 and chinese_chars / total_chars_sec > 0.2:
                issues.append(f"Section '{s['id']}' may be partially untranslated ({chinese_chars}/{total_chars_sec} Chinese chars)")

    for issue in issues:
        print(f"  ⚠ {issue}")

    return len(issues) == 0


# ─── Deterministic HTML Template ────────────────────────────────────────────

def _extract_title(html_content, index):
    """Extract a meaningful title from an HTML chunk for TOC display.
    Skips repeated company name headers and page markers."""
    headings = re.findall(r'<h[23][^>]*>(.*?)</h[23]>', html_content, re.IGNORECASE)
    for h in headings:
        title = re.sub(r'<[^>]+>', '', h).strip()
        if not title or len(title) > 120:
            continue
        # Skip repeated headers: company names, page numbers, "Prospectus"
        lower = title.lower()
        if any(skip in lower for skip in ['prospectus', '招股', 'co., ltd', 'corporation', 'limited', '股份有限公司', '— prospectus']):
            continue
        if re.match(r'^[\d\s\-–\.]+$', title):  # page numbers
            continue
        if re.match(r'^section\s+\d+$', title, re.IGNORECASE):  # "Section 5"
            continue
        return title
    # Fallback: try first substantial paragraph as title
    paras = re.findall(r'<p[^>]*>(.*?)</p>', html_content, re.IGNORECASE)
    for p in paras[:3]:
        text = re.sub(r'<[^>]+>', '', p).strip()
        if len(text) > 10 and len(text) < 80 and not text.startswith('1-'):
            return text
    return f'Part {index + 1}'


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


def _get_unit_multiplier(data):
    """Get multiplier to convert financial values to full RMB.
    Returns (multiplier, is_usd) — if already USD, multiplier converts to full USD."""
    fin = data.get('financials', {})
    unit = (fin.get('unit', '') + ' ' + fin.get('currency', '')).lower()
    currency = fin.get('currency', 'RMB').lower()

    if 'usd' in currency or 'us$' in unit or 'us dollar' in currency:
        # Already in USD
        if 'thousand' in unit:
            return 1000, True
        elif '万' in unit or '10k' in unit:
            return 10000, True
        return 1, True

    if 'hkd' in currency or 'hk$' in unit:
        # HKD — convert to USD at 0.128
        if 'thousand' in unit:
            return 1000 * 0.128, True
        elif '万' in unit or '10k' in unit:
            return 10000 * 0.128, True
        return 0.128, True

    # RMB
    if '万' in unit or '10k' in unit:
        return 10000, False
    elif '千' in unit or 'thousand' in unit:
        return 1000, False
    elif '亿' in unit or '100m' in unit:
        return 100000000, False
    return 1, False


def render_html(data, full_text_data):
    """Generate complete HTML from structured data + full translation. Deterministic — no LLM call."""

    company_en = data.get('meta', {}).get('issuer', 'Unknown Company')
    company_cn = data.get('company', {}).get('name_cn', '')
    exchange = data.get('meta', {}).get('exchange', '')
    board = data.get('meta', {}).get('board', '')
    filing_date = data.get('meta', {}).get('filing_date', '')
    industry = data.get('company', {}).get('industry', '')

    # Determine financial units for currency conversion
    unit_mult, is_usd = _get_unit_multiplier(data)

    # ── Executive summary ──
    exec_summary = data.get('executive_summary', '')
    if exec_summary:
        paragraphs = exec_summary.strip().split('\n\n')
        exec_summary_html = '<div class="exec-summary">\n<div class="exec-label">AI-Generated Summary</div>\n'
        for p in paragraphs:
            p = p.strip()
            if p:
                exec_summary_html += f'<p>{esc(p)}</p>\n'
        exec_summary_html += '</div>'
    else:
        exec_summary_html = ''

    def fmt_money(val):
        """Format a monetary value as a readable USD string for display."""
        if val is None:
            return 'N/A', False
        try:
            raw = float(val)
        except (ValueError, TypeError):
            return str(val), False
        # Convert to full base currency
        full = raw * unit_mult
        if is_usd:
            usd = full
        else:
            usd = full / 7.25  # RMB to USD
        # Format readably
        if abs(usd) >= 1e9:
            return f'${usd/1e9:.1f}B', True
        elif abs(usd) >= 1e6:
            return f'${usd/1e6:.0f}M', True
        elif abs(usd) >= 1e3:
            return f'${usd/1e3:.0f}K', True
        else:
            return f'${usd:,.0f}', True

    # ── KPI cards ──
    kpis = []  # (label, value, css_class)
    fin = data.get('financials', {})
    if fin.get('income_statement'):
        latest = fin['income_statement'][-1]
        if latest.get('revenue'):
            val, _ = fmt_money(latest['revenue'])
            kpis.append(('Revenue (' + str(latest.get('period', '')) + ')', val, 'blue'))
        if latest.get('net_profit'):
            val, _ = fmt_money(latest['net_profit'])
            kpis.append(('Net Profit (' + str(latest.get('period', '')) + ')', val, 'green'))
        if latest.get('gross_margin_pct'):
            kpis.append(('Gross Margin', str(latest['gross_margin_pct']) + '%', 'green'))
    emp = data.get('company', {}).get('employees', {})
    if emp and emp.get('total'):
        kpis.append(('Employees', fmt_num(emp['total']), 'blue'))
    offering = data.get('offering', {})
    if offering.get('offering_pct_approx'):
        kpis.append(('Offering %', str(offering['offering_pct_approx']) + '%', 'blue'))

    kpi_html = ''
    for label, value, cls in kpis:
        kpi_html += f'<div class="kpi"><div class="label">{esc(label)}</div><div class="value {cls}">{esc(value)}</div></div>\n'

    # ── Risks ──
    risks = data.get('key_risks', [])
    risks_html = '<ul>' + ''.join(f'<li>{esc(r)}</li>' for r in risks) + '</ul>' if risks else '<p>No risk factors extracted from this filing.</p>'

    # ── Financials tables ──
    financials_html = ''
    if fin.get('income_statement'):
        financials_html += '<h3>Income Statement (USD)</h3>\n<table><thead><tr><th>Period</th><th>Revenue</th><th>Net Profit</th><th>Gross Margin</th></tr></thead><tbody>'
        for row in fin['income_statement']:
            rev, _ = fmt_money(row.get('revenue'))
            np_val, _ = fmt_money(row.get('net_profit'))
            gm = row.get('gross_margin_pct')
            gm_str = f'{gm}%' if gm is not None else 'N/A'
            financials_html += f'<tr><td>{esc(row.get("period",""))}</td><td>{rev}</td><td>{np_val}</td><td>{gm_str}</td></tr>'
        financials_html += '</tbody></table>'
    if fin.get('balance_sheet'):
        financials_html += '<h3>Balance Sheet (USD)</h3>\n<table><thead><tr><th>Date</th><th>Total Assets</th><th>Total Liabilities</th><th>Equity</th></tr></thead><tbody>'
        for row in fin['balance_sheet']:
            ta, _ = fmt_money(row.get('total_assets'))
            tl, _ = fmt_money(row.get('total_liabilities'))
            eq, _ = fmt_money(row.get('equity'))
            financials_html += f'<tr><td>{esc(row.get("date",""))}</td><td>{ta}</td><td>{tl}</td><td>{eq}</td></tr>'
        financials_html += '</tbody></table>'
    if not financials_html:
        financials_html = '<p>Financial data not disclosed in this draft filing.</p>'

    # ── Shareholders ──
    shareholders = data.get('shareholders_pre_ipo', [])
    if shareholders:
        shareholders_html = '<table><thead><tr><th>Name</th><th>Shares (万)</th><th>%</th><th>Type</th></tr></thead><tbody>'
        for sh in shareholders:
            shareholders_html += f'<tr><td>{esc(sh.get("name",""))}</td><td>{fmt_num(sh.get("shares_10k"))}</td><td>{sh.get("pct","N/A")}%</td><td>{esc(sh.get("type",""))}</td></tr>'
        shareholders_html += '</tbody></table>'
    else:
        shareholders_html = '<p>Shareholder data not disclosed in this draft filing.</p>'

    # ── Use of Proceeds ──
    proceeds = data.get('use_of_proceeds', {})
    projects = proceeds.get('projects', [])
    if projects:
        proceeds_html = '<table><thead><tr><th>Project</th><th>Amount (USD)</th><th>Focus</th></tr></thead><tbody>'
        for p in projects:
            amt, _ = fmt_money(p.get('amount_rmb_10k'))
            proceeds_html += f'<tr><td>{esc(p.get("name",""))}</td><td>{amt}</td><td>{esc(p.get("focus",""))}</td></tr>'
        proceeds_html += '</tbody></table>'
    else:
        proceeds_html = '<p>Use of proceeds not disclosed in this draft filing.</p>'

    # ── Full translation sections ──
    full_translation_html = ''
    if full_text_data and full_text_data.get('sections'):
        # First pass: render all content
        raw_content = ''
        for sec_i, sec in enumerate(full_text_data['sections']):
            content = sec.get('content', '')
            is_html = bool(re.search(r'<(?:p|h[1-6]|table|div|ul|ol|em|strong)\b', content))
            if is_html:
                raw_content += content + '\n'
            else:
                paragraphs = content.split('\n\n')
                for para in paragraphs:
                    para = para.strip()
                    if not para:
                        continue
                    if len(para) < 100 and not para.endswith('.') and not para.endswith('。'):
                        if para.startswith('#'):
                            level = len(para) - len(para.lstrip('#'))
                            text = para.lstrip('#').strip()
                            raw_content += f'<h{min(level+1,4)}>{esc(text)}</h{min(level+1,4)}>\n'
                        else:
                            raw_content += f'<h3>{esc(para)}</h3>\n'
                    else:
                        raw_content += f'<p>{esc(para)}</p>\n'

        # Second pass: find all <h2> headings, give them IDs, build TOC
        toc_entries = []
        heading_counter = 0
        skip_patterns = ['prospectus', '招股', '股份有限公司', 'co., ltd', 'corporation limited', '— prospectus',
                         'table of contents', 'issuer\'s declaration', 'declaration', '目录', '目錄', '声明']

        def _is_top_level_heading(text):
            """Check if a heading is a top-level section (not a sub-section)."""
            # Skip sub-section numbering patterns like "III.", "VIII.", "(IV)", "3.2"
            if re.match(r'^[\(\（]?[IVXivx]+[\.\)）]', text):  # Roman numeral sub-sections
                return False
            if re.match(r'^[\(\（]\d+[\)）]', text):  # (1), (2), etc.
                return False
            if re.match(r'^\d+\.\d+', text):  # 3.2, 5.1, etc.
                return False
            # Keep: "Section 1", "Section II", "第X节", or plain title headings
            return True

        def _heading_replacer(match):
            nonlocal heading_counter
            tag = match.group(1)  # h2 or h3
            attrs = match.group(2)
            text_html = match.group(3)
            text = re.sub(r'<[^>]+>', '', text_html).strip()
            # Clean up nbsp
            text = re.sub(r'&nbsp;|&amp;nbsp;|\xa0', ' ', text)
            text = re.sub(r'\s{2,}', ' ', text).strip()
            # Only index h2 headings that are top-level sections
            if tag == 'h2' and text and len(text) < 150:
                lower = text.lower()
                if (not any(skip in lower for skip in skip_patterns)
                    and not re.match(r'^[\d\s\-–\.]+$', text)
                    and _is_top_level_heading(text)):
                    anchor = f'ft-{heading_counter}'
                    heading_counter += 1
                    toc_entries.append((anchor, text))
                    return f'<{tag}{attrs} id="{anchor}">{text_html}</{tag}>'
            return match.group(0)

        def _sanitize_tables(content):
            """Fix broken table HTML in translation content.
            If tables are balanced, keep them. If not, convert to simple divs."""
            opens = len(re.findall(r'<table[\s>]', content, re.IGNORECASE))
            closes = len(re.findall(r'</table>', content, re.IGNORECASE))
            if opens == closes:
                return content  # Tables are balanced, keep as-is
            # Unbalanced — convert all table markup to divs to prevent DOM nesting issues
            content = re.sub(r'<table[^>]*>', '<div class="table-fallback">', content, flags=re.IGNORECASE)
            content = re.sub(r'</table>', '</div>', content, flags=re.IGNORECASE)
            content = re.sub(r'<thead[^>]*>', '', content, flags=re.IGNORECASE)
            content = re.sub(r'</thead>', '', content, flags=re.IGNORECASE)
            content = re.sub(r'<tbody[^>]*>', '', content, flags=re.IGNORECASE)
            content = re.sub(r'</tbody>', '', content, flags=re.IGNORECASE)
            content = re.sub(r'<tr[^>]*>', '<div class="table-row">', content, flags=re.IGNORECASE)
            content = re.sub(r'</tr>', '</div>', content, flags=re.IGNORECASE)
            content = re.sub(r'<t[hd][^>]*>', '<span class="table-cell">', content, flags=re.IGNORECASE)
            content = re.sub(r'</t[hd]>', '</span> ', content, flags=re.IGNORECASE)
            return content

        # Balance HTML tags in raw content before splitting into accordions
        # This prevents unclosed <table>/<td>/<tr> from swallowing subsequent sections
        def _balance_at_splits(html_content, split_markers):
            """Insert closing tags before each split marker to prevent DOM nesting issues."""
            result = html_content
            for marker in split_markers:
                pos = result.find(marker)
                if pos < 0:
                    continue
                before = result[:pos]
                # Count unclosed table-related tags in content before this marker
                closers = ''
                for tag in ['td', 'tr', 'table']:
                    opens = len(re.findall(f'<{tag}[\\s>]', before, re.IGNORECASE))
                    closes = len(re.findall(f'</{tag}>', before, re.IGNORECASE))
                    if opens > closes:
                        closers += f'</{tag}>' * (opens - closes)
                if closers:
                    result = result[:pos] + closers + result[pos:]
            return result

        # First: tag each TOC-level h2 with an ID and collect positions
        toc_positions = []  # (char_position, anchor, title)
        heading_offset = [0]  # track offset changes from replacements

        def _heading_replacer_with_pos(match):
            nonlocal heading_counter
            tag = match.group(1)
            attrs = match.group(2)
            text_html = match.group(3)
            text = re.sub(r'<[^>]+>', '', text_html).strip()
            text = re.sub(r'&nbsp;|&amp;nbsp;|\xa0', ' ', text)
            text = re.sub(r'\s{2,}', ' ', text).strip()
            if tag == 'h2' and text and len(text) < 150:
                lower = text.lower()
                if (not any(skip in lower for skip in skip_patterns)
                    and not re.match(r'^[\d\s\-–\.]+$', text)
                    and _is_top_level_heading(text)):
                    anchor = f'ft-{heading_counter}'
                    heading_counter += 1
                    toc_entries.append((anchor, text))
                    return f'<!--TOC:{anchor}--><{tag}{attrs} id="{anchor}">{text_html}</{tag}>'
            return match.group(0)

        # Pre-sanitize: close unclosed table tags before each <h2> to prevent DOM nesting
        raw_content = re.sub(r'</?section[^>]*>', '', raw_content)
        h2_positions = [m.start() for m in re.finditer(r'<h2[\s>]', raw_content, re.IGNORECASE)]
        if h2_positions:
            parts_raw = []
            prev = 0
            for pos in h2_positions:
                chunk = raw_content[prev:pos]
                # Close any unclosed table tags in this chunk
                for tag in ['td', 'tr', 'table']:
                    o = len(re.findall(f'<{tag}[\\s>]', chunk, re.IGNORECASE))
                    c = len(re.findall(f'</{tag}>', chunk, re.IGNORECASE))
                    if o > c:
                        chunk += f'</{tag}>' * (o - c)
                parts_raw.append(chunk)
                prev = pos
            parts_raw.append(raw_content[prev:])
            raw_content = ''.join(parts_raw)

        tagged_body = re.sub(r'<(h[23])([^>]*)>(.*?)</\1>', _heading_replacer_with_pos, raw_content, flags=re.IGNORECASE)

        if toc_entries:
            # Balance unclosed table tags before each split point
            markers = [f'<!--TOC:{anchor}-->' for anchor, _ in toc_entries]
            tagged_body = _balance_at_splits(tagged_body, markers)

            # Split at TOC markers
            marker_pattern = r'<!--TOC:(ft-\d+)-->'
            pieces = re.split(marker_pattern, tagged_body)
            # pieces = [preamble, 'ft-0', content0, 'ft-1', content1, ...]

            accordion_html = ''

            # Preamble
            preamble = pieces[0].strip()
            if preamble and len(preamble) > 200:
                preamble = re.sub(r'</?section[^>]*>', '', preamble)
                accordion_html += f'<section class="ft-section">\n<div class="section-header" onclick="toggleSection(this)"><h2>Preamble</h2><span class="chevron">&#9654;</span></div>\n<div class="section-body"><div class="translation-content">{preamble}</div></div>\n</section>\n'

            # Build anchor→title map
            anchor_titles = dict(toc_entries)

            # Each section
            i = 1
            while i < len(pieces) - 1:
                anchor = pieces[i]
                content = pieces[i + 1] if i + 1 < len(pieces) else ''
                title = anchor_titles.get(anchor, anchor)
                # Remove the <h2> tag from content since it's now the accordion header
                content = re.sub(r'^\s*<h2[^>]*>.*?</h2>\s*', '', content, count=1, flags=re.DOTALL)
                # Sanitize translation content
                content = re.sub(r'</?section[^>]*>', '', content)
                content = _sanitize_tables(content)
                accordion_html += f'<section class="ft-section collapsed" id="{anchor}">\n<div class="section-header" onclick="toggleSection(this)"><h2>{esc(title)}</h2><span class="chevron">&#9654;</span></div>\n<div class="section-body"><div class="translation-content">{content}</div></div>\n</section>\n'
                i += 2

            # Build TOC
            toc_html = '<nav class="translation-toc"><h3>Table of Contents</h3><ol>\n'
            for anchor, title in toc_entries:
                toc_html += f'  <li><a href="#{anchor}" onclick="expandAndScroll(\'{anchor}\'); return false;">{esc(title)}</a></li>\n'
            toc_html += '</ol></nav>\n'

            full_translation_html = toc_html + accordion_html
        else:
            full_translation_html = '<div class="translation-content-body">\n' + tagged_body + '\n</div>'
    else:
        full_translation_html = '<p>Full translation not available.</p>'

    # ── Data for JS (embedded JSON) ──
    data_json_str = json.dumps(data, ensure_ascii=False)

    # ── Build complete HTML ──
    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(company_en)} IPO Prospectus — Full English Translation</title>
<meta name="description" content="Complete English translation of {esc(company_en)} ({esc(company_cn)}) IPO Prospectus">
<style>
:root {{
  --bg: #0d1117; --surface: #161b22; --border: #30363d;
  --text: #e6edf3; --text-muted: #8b949e;
  --accent: #58a6ff; --accent2: #3fb950; --accent3: #d29922; --red: #f85149;
}}
[data-theme="light"] {{
  --bg: #ffffff; --surface: #f6f8fa; --border: #d0d7de;
  --text: #1f2328; --text-muted: #656d76;
  --accent: #0969da; --accent2: #1a7f37; --accent3: #9a6700; --red: #cf222e;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; background: var(--bg); color: var(--text); line-height: 1.6; }}
a {{ color: var(--accent); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.container {{ max-width: 1200px; margin: 0 auto; padding: 0 24px; }}

header {{ background: var(--surface); border-bottom: 1px solid var(--border); padding: 16px 0; position: sticky; top: 0; z-index: 100; }}
header .container {{ display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
.logo {{ font-size: 1.05rem; font-weight: 700; white-space: nowrap; }}
.logo span {{ color: var(--accent); }}
#search {{ background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 8px 14px; border-radius: 6px; flex: 1; min-width: 200px; max-width: 400px; font-size: 0.9rem; }}
#search:focus {{ outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(88,166,255,0.15); }}
.header-actions {{ display: flex; gap: 6px; margin-left: auto; }}
.toggle-btn {{ background: var(--bg); border: 1px solid var(--border); color: var(--text-muted); padding: 5px 10px; border-radius: 6px; font-size: 0.75rem; cursor: pointer; white-space: nowrap; font-weight: 600; transition: all 0.2s; }}
.toggle-btn:hover {{ border-color: var(--accent); color: var(--text); }}
.toggle-btn.active {{ border-color: var(--accent2); color: var(--accent2); }}

.hero {{ padding: 40px 0 24px; border-bottom: 1px solid var(--border); }}
.hero h1 {{ font-size: 1.8rem; margin-bottom: 6px; }}
.hero .subtitle {{ color: var(--text-muted); margin-bottom: 16px; }}
.quick-links {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }}
.quick-links a {{ display: inline-block; padding: 4px 12px; border-radius: 6px; font-size: 0.78rem; font-weight: 500; background: var(--surface); border: 1px solid var(--border); color: var(--text-muted); transition: all 0.2s; }}
.quick-links a:hover {{ border-color: var(--accent); color: var(--accent); text-decoration: none; }}

.tab-bar {{ display: flex; gap: 0; border-bottom: 2px solid var(--border); margin: 24px 0 0; }}
.tab-bar button {{ background: none; border: none; color: var(--text-muted); padding: 10px 20px; font-size: 0.9rem; cursor: pointer; border-bottom: 2px solid transparent; margin-bottom: -2px; font-weight: 500; transition: all 0.2s; }}
.tab-bar button:hover {{ color: var(--text); }}
.tab-bar button.active {{ color: var(--accent); border-bottom-color: var(--accent); }}
.tab-content {{ display: none; padding: 24px 0; }}
.tab-content.active {{ display: block; }}

section {{ margin: 16px 0; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }}
.section-header {{ display: flex; align-items: center; justify-content: space-between; padding: 14px 20px; cursor: pointer; user-select: none; }}
.section-header:hover {{ background: rgba(88,166,255,0.04); }}
.section-header h2 {{ font-size: 1.15rem; }}
.section-header .chevron {{ color: var(--text-muted); transition: transform 0.2s; font-size: 1.2rem; }}
.section-header .chevron.open {{ transform: rotate(90deg); }}
.section-body {{ padding: 0 20px 20px; }}
section.collapsed .section-body {{ display: none; }}

.exec-summary {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 20px 24px; margin-bottom: 20px; font-size: 0.9rem; line-height: 1.7; }}
.exec-summary p {{ margin: 8px 0; }}
.exec-label {{ font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-muted); margin-bottom: 10px; }}
.exec-summary p:first-of-type {{ margin-top: 0; }}
.exec-summary p:last-child {{ margin-bottom: 0; }}
.kpi-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin: 16px 0; }}
.kpi {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }}
.kpi .label {{ font-size: 0.75rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; }}
.kpi .value {{ font-size: 1.5rem; font-weight: 700; margin-top: 4px; }}
.kpi .value.green {{ color: var(--accent2); }}
.kpi .value.blue {{ color: var(--accent); }}

table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; margin: 12px 0; }}
th {{ text-align: left; padding: 10px 12px; border-bottom: 2px solid var(--border); color: var(--text-muted); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; }}
td {{ padding: 10px 12px; border-bottom: 1px solid var(--border); }}
tr:hover td {{ background: rgba(88,166,255,0.04); }}

.translation-content {{ font-size: 0.95rem; line-height: 1.8; }}
.translation-content h2 {{ font-size: 1.4rem; margin: 32px 0 12px; padding-bottom: 8px; border-bottom: 1px solid var(--border); color: var(--accent); }}
.translation-content h3 {{ font-size: 1.15rem; margin: 24px 0 8px; }}
.translation-content h4 {{ font-size: 1rem; margin: 16px 0 6px; }}
.translation-content p {{ margin: 8px 0; }}
.translation-content ul, .translation-content ol {{ margin: 8px 0 8px 24px; }}
.translation-content li {{ margin: 4px 0; }}
.translation-content table {{ margin: 16px 0; }}
.translation-chunk {{ margin-bottom: 32px; padding-bottom: 32px; border-bottom: 1px dashed var(--border); }}
.chunk-title {{ font-size: 1.3rem; color: var(--accent); margin: 0 0 16px; padding-bottom: 8px; border-bottom: 2px solid var(--accent); }}
.translation-toc {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 20px 24px; margin-bottom: 24px; }}
.translation-toc h3 {{ font-size: 0.9rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px; }}
.translation-toc ol {{ margin: 0; padding-left: 20px; columns: 2; column-gap: 24px; }}
.translation-toc li {{ font-size: 0.85rem; margin: 4px 0; break-inside: avoid; }}
.translation-toc a {{ color: var(--accent); }}
.translation-toc a:hover {{ text-decoration: underline; }}
.ft-section {{ margin: 8px 0; }}
.ft-section .section-header h2 {{ font-size: 1.05rem; }}
.ft-section .section-body {{ max-height: none; }}
.ft-section .translation-content {{ font-size: 0.92rem; line-height: 1.75; }}
.table-fallback {{ margin: 12px 0; padding: 8px; border: 1px solid var(--border); border-radius: 4px; overflow-x: auto; }}
.table-row {{ display: flex; flex-wrap: wrap; gap: 8px; padding: 4px 0; border-bottom: 1px solid var(--border); }}
.table-cell {{ flex: 1; min-width: 80px; font-size: 0.85rem; }}

footer {{ padding: 32px 0; margin-top: 48px; border-top: 1px solid var(--border); color: var(--text-muted); font-size: 0.75rem; text-align: center; }}

/* Search */
#search-results {{ position: absolute; top: 100%; left: 0; right: 0; background: var(--surface); border: 1px solid var(--border); border-radius: 0 0 8px 8px; max-height: 400px; overflow-y: auto; z-index: 200; display: none; }}
.search-hit {{ padding: 10px 16px; border-bottom: 1px solid var(--border); cursor: pointer; }}
.search-hit:hover {{ background: rgba(88,166,255,0.08); }}
.search-hit .hit-title {{ font-weight: 600; font-size: 0.85rem; }}
.search-hit .hit-snippet {{ font-size: 0.8rem; color: var(--text-muted); margin-top: 2px; }}
mark {{ background: rgba(88,166,255,0.25); color: var(--text); border-radius: 2px; padding: 0 2px; }}
.ai-answer {{ background: var(--surface); border: 1px solid var(--accent); border-radius: 8px; padding: 16px; margin: 12px 24px; }}
.ai-label {{ font-size: 0.7rem; color: var(--accent); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; }}
.ai-body {{ font-size: 0.9rem; line-height: 1.7; }}
.ai-body h3 {{ font-size: 1rem; margin: 12px 0 6px; color: var(--accent); }}
.ai-body h4 {{ font-size: 0.9rem; margin: 10px 0 4px; }}
.ai-body p {{ margin: 6px 0; }}
.ai-body ul {{ margin: 6px 0 6px 20px; }}
.ai-body li {{ margin: 3px 0; }}
.ai-body strong {{ color: var(--text); }}
.ai-loading {{ padding: 16px 24px; color: var(--text-muted); }}
.ai-setup {{ padding: 16px 24px; }}
.ai-setup input {{ background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 6px 10px; border-radius: 4px; width: 300px; font-size: 0.8rem; }}

@media (max-width: 700px) {{
  .hero h1 {{ font-size: 1.3rem; }}
  .kpi-grid {{ grid-template-columns: 1fr 1fr; }}
  #search {{ max-width: 100%; order: 10; }}
  .header-actions {{ order: 9; }}
}}
</style>
</head>
<body>
<header>
  <div class="container" style="position:relative;">
    <a class="logo" href="../" style="text-decoration:none;"><span>{esc(company_en)}</span></a>
    <input type="text" id="search" placeholder="Search prospectus... (Enter for AI answer)">
    <div class="header-actions">
      <button class="toggle-btn" id="themeToggle" onclick="toggleTheme()">Light</button>
    </div>
    <div id="search-results"></div>
  </div>
</header>

<div class="container">
  <div class="hero">
    <h1>{esc(company_en)} ({esc(company_cn)}) IPO Prospectus</h1>
    <p class="subtitle">{esc(exchange)} {esc(board)} &middot; Filed {esc(filing_date)} &middot; Full English Translation</p>
    <div class="quick-links" id="quickLinks"></div>
  </div>

  <div class="tab-bar">
    <button class="active" onclick="switchTab('summary')">Summary</button>
    <button onclick="switchTab('full')">Full Translation</button>
  </div>

  <div id="tab-summary" class="tab-content active">
    {exec_summary_html}
    <div class="kpi-grid">{kpi_html}</div>

    <section id="sec-risks">
      <div class="section-header" onclick="toggleSection(this)"><h2>Key Risk Factors</h2><span class="chevron open">&#9654;</span></div>
      <div class="section-body">{risks_html}</div>
    </section>

    <section id="sec-financials">
      <div class="section-header" onclick="toggleSection(this)"><h2>Financial Highlights</h2><span class="chevron open">&#9654;</span></div>
      <div class="section-body">{financials_html}</div>
    </section>

    <section id="sec-shareholders">
      <div class="section-header" onclick="toggleSection(this)"><h2>Shareholders</h2><span class="chevron open">&#9654;</span></div>
      <div class="section-body">{shareholders_html}</div>
    </section>

    <section id="sec-proceeds">
      <div class="section-header" onclick="toggleSection(this)"><h2>Use of Proceeds</h2><span class="chevron open">&#9654;</span></div>
      <div class="section-body">{proceeds_html}</div>
    </section>
  </div>

  <div id="tab-full" class="tab-content">
    {full_translation_html}
  </div>
</div>

<footer>
  <div class="container">
    <p>AI-translated summary for reference only. Not investment advice. For official filings, refer to the exchange website.</p>
    <p style="margin-top:8px;">Built by <a href="https://github.com/saranormous">Sarah Guo</a> and <a href="https://claude.ai">Claude</a> &middot; <a href="https://github.com/saranormous/filing-websites">Source</a></p>
  </div>
</footer>

<script>
// Tab switching
function switchTab(tab) {{
  document.querySelectorAll('.tab-bar button').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
  event.target.classList.add('active');
}}

// Collapsible sections
function toggleSection(header) {{
  const section = header.parentElement;
  section.classList.toggle('collapsed');
  header.querySelector('.chevron').classList.toggle('open');
}}

// Expand accordion section and scroll to it (used by TOC links)
function expandAndScroll(anchorId) {{
  // Switch to full translation tab if needed
  var fullTab = document.getElementById('tab-full');
  if (!fullTab.classList.contains('active')) {{
    document.querySelectorAll('.tab-bar button').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    fullTab.classList.add('active');
    document.querySelectorAll('.tab-bar button')[1].classList.add('active');
  }}
  // Find and expand the section
  var section = document.getElementById(anchorId);
  if (section) {{
    section.classList.remove('collapsed');
    var chevron = section.querySelector('.chevron');
    if (chevron) chevron.classList.add('open');
    // Use requestAnimationFrame to ensure DOM has updated before scrolling
    requestAnimationFrame(function() {{
      requestAnimationFrame(function() {{
        section.scrollIntoView({{ block: 'start', behavior: 'smooth' }});
      }});
    }});
  }}
}}

// Add IDs to full translation headings for deep linking
const fullH2s = document.querySelectorAll('#tab-full .translation-content h2, #tab-full .translation-content h3');
fullH2s.forEach((h, i) => {{
  const slug = h.textContent.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').substring(0, 60);
  h.id = 'full-' + slug;
}});

// Cross-link: summary sections → full translation sections
function jumpToFull(keyword) {{
  document.querySelectorAll('.tab-bar button').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-full').classList.add('active');
  document.querySelectorAll('.tab-bar button')[1].classList.add('active');
  const target = Array.from(fullH2s).find(h => {{
    const t = h.textContent.toLowerCase();
    return keyword.split('|').some(k => t.includes(k));
  }});
  if (target) setTimeout(() => {{ target.scrollIntoView({{ block: 'start' }}); }}, 300);
}}

// Add "Read in full →" links to summary sections
const crossLinks = {{
  'sec-risks': 'risk factor|risk factors',
  'sec-financials': 'financial|accounting',
  'sec-shareholders': 'shareholder|basic information|issuer',
  'sec-proceeds': 'use of raised funds|use of proceeds|fundraising'
}};
Object.entries(crossLinks).forEach(([secId, keyword]) => {{
  const sec = document.getElementById(secId);
  if (!sec) return;
  const body = sec.querySelector('.section-body');
  if (!body) return;
  const link = document.createElement('a');
  link.href = '#';
  link.textContent = 'Read in full translation →';
  link.style.cssText = 'display:inline-block;margin-top:12px;font-size:0.85rem;font-weight:500;';
  link.onclick = (e) => {{ e.preventDefault(); jumpToFull(keyword); }};
  body.appendChild(link);
}});

// Quick links
const sections = document.querySelectorAll('#tab-summary section[id]');
const quickLinks = document.getElementById('quickLinks');
sections.forEach(s => {{
  const title = s.querySelector('h2').textContent;
  const a = document.createElement('a');
  a.href = '#' + s.id;
  a.textContent = title;
  quickLinks.appendChild(a);
}});
const ftLink = document.createElement('a');
ftLink.href = '#';
ftLink.textContent = 'Full Translation';
ftLink.onclick = (e) => {{ e.preventDefault(); document.querySelector('.tab-bar button:nth-child(2)').click(); window.scrollTo(0, 0); }};
quickLinks.appendChild(ftLink);

// Theme toggle
function toggleTheme() {{
  const current = document.documentElement.getAttribute('data-theme');
  const next = current === 'light' ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', next);
  document.getElementById('themeToggle').textContent = next === 'light' ? 'Dark' : 'Light';
  localStorage.setItem('theme', next);
}}
if (localStorage.getItem('theme') === 'light') {{
  document.documentElement.setAttribute('data-theme', 'light');
  document.getElementById('themeToggle').textContent = 'Dark';
}}

// Search
const data = {data_json_str};
const searchInput = document.getElementById('search');
const searchResults = document.getElementById('search-results');

function getSearchableText() {{
  const els = document.querySelectorAll('.tab-content h2, .tab-content h3, .tab-content p, .tab-content td, .tab-content li');
  return Array.from(els).map(el => ({{ text: el.textContent, el }}));
}}

function keywordSearch(q) {{
  const items = getSearchableText();
  const terms = q.toLowerCase().split(/\\s+/);
  return items.filter(item => terms.every(t => item.text.toLowerCase().includes(t))).slice(0, 15);
}}

searchInput.addEventListener('input', () => {{
  const q = searchInput.value.trim();
  if (!q) {{ searchResults.style.display = 'none'; return; }}
  const hits = keywordSearch(q);
  if (hits.length === 0) {{ searchResults.innerHTML = '<div class="search-hit"><div class="hit-snippet">No results. Press Enter for AI answer.</div></div>'; searchResults.style.display = 'block'; return; }}
  searchResults.innerHTML = hits.map(h => {{
    const snippet = h.text.substring(0, 120);
    return '<div class="search-hit"><div class="hit-snippet">' + snippet + '</div></div>';
  }}).join('');
  searchResults.style.display = 'block';
}});

function renderMd(text) {{
  return text
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/^### (.+)$/gm, '<h4>$1</h4>')
    .replace(/^## (.+)$/gm, '<h3>$1</h3>')
    .replace(/^# (.+)$/gm, '<h3>$1</h3>')
    .replace(/\\*\\*(.+?)\\*\\*/g, '<strong>$1</strong>')
    .replace(/^- (.+)$/gm, '<li>$1</li>')
    .replace(/(<li>.*<\\/li>\\n?)+/g, '<ul>$&</ul>')
    .replace(/\\n\\n/g, '</p><p>')
    .replace(/\\n/g, '<br>')
    .replace(/^/, '<p>').replace(/$/, '</p>')
    .replace(/<p><h([34])>/g, '<h$1>').replace(/<\\/h([34])><\\/p>/g, '</h$1>')
    .replace(/<p><ul>/g, '<ul>').replace(/<\\/ul><\\/p>/g, '</ul>')
    .replace(/<p><\\/p>/g, '');
}}

let lastLLMCall = 0;
const LLM_COOLDOWN = 3000;

function getProvider() {{
  const ak = localStorage.getItem('anthropic_api_key');
  const ok = localStorage.getItem('openai_api_key');
  if (ak) return {{ type: 'anthropic', key: ak }};
  if (ok) return {{ type: 'openai', key: ok }};
  return null;
}}

async function callLLM(provider, query, context) {{
  const sysPrompt = 'You answer questions about an IPO prospectus. Be specific and cite data.';
  const userMsg = 'Based on this prospectus data, answer: ' + query + '\\n\\n' + JSON.stringify(context);
  if (provider.type === 'anthropic') {{
    const resp = await fetch('https://api.anthropic.com/v1/messages', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json', 'x-api-key': provider.key, 'anthropic-version': '2023-06-01', 'anthropic-dangerous-direct-browser-access': 'true' }},
      body: JSON.stringify({{ model: 'claude-haiku-4-5-20251001', max_tokens: 1024, system: sysPrompt, messages: [{{ role: 'user', content: userMsg }}] }})
    }});
    const result = await resp.json();
    if (result.error) throw new Error(result.error.message);
    return result.content?.[0]?.text || 'No answer available.';
  }} else {{
    const resp = await fetch('https://api.openai.com/v1/chat/completions', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + provider.key }},
      body: JSON.stringify({{ model: 'gpt-4o-mini', max_tokens: 1024, messages: [{{ role: 'system', content: sysPrompt }}, {{ role: 'user', content: userMsg }}] }})
    }});
    const result = await resp.json();
    if (result.error) throw new Error(result.error.message);
    return result.choices?.[0]?.message?.content || 'No answer available.';
  }}
}}

searchInput.addEventListener('keydown', async (e) => {{
  if (e.key === 'Enter') {{
    const q = searchInput.value.trim();
    if (!q) return;
    const now = Date.now();
    if (now - lastLLMCall < LLM_COOLDOWN) {{
      searchResults.innerHTML = '<div class="ai-answer"><div class="ai-label">Rate limited</div><p>Please wait a few seconds between queries.</p></div>';
      searchResults.style.display = 'block';
      return;
    }}
    const provider = getProvider();
    if (!provider) {{
      searchResults.innerHTML = '<div class="ai-setup"><p>Enter an API key for AI-powered search:</p><div style="display:flex;gap:8px;margin:8px 0;flex-wrap:wrap;"><input type="password" id="apiKeyInput" placeholder="sk-ant-... or sk-..." style="flex:1;min-width:200px;padding:6px 10px;"><button id="saveAnthropicBtn" style="padding:4px 12px;cursor:pointer;">Save as Anthropic</button><button id="saveOpenAIBtn" style="padding:4px 12px;cursor:pointer;">Save as OpenAI</button></div><p style="font-size:0.75rem;color:var(--text-muted);">Key is stored only in your browser\\'s localStorage. Never sent anywhere except the API.</p></div>';
      setTimeout(() => {{
        document.getElementById('saveAnthropicBtn')?.addEventListener('click', () => {{ localStorage.setItem('anthropic_api_key', document.getElementById('apiKeyInput').value); searchInput.dispatchEvent(new KeyboardEvent('keydown', {{key:'Enter'}})); }});
        document.getElementById('saveOpenAIBtn')?.addEventListener('click', () => {{ localStorage.setItem('openai_api_key', document.getElementById('apiKeyInput').value); searchInput.dispatchEvent(new KeyboardEvent('keydown', {{key:'Enter'}})); }});
      }}, 0);
      searchResults.style.display = 'block';
      return;
    }}
    lastLLMCall = now;
    const label = provider.type === 'anthropic' ? 'Claude' : 'GPT-4o-mini';
    searchResults.innerHTML = '<div class="ai-loading">Asking ' + label + '<span class="dots">...</span></div>';
    searchResults.style.display = 'block';
    try {{
      const answer = await callLLM(provider, q, data);
      searchResults.innerHTML = '<div class="ai-answer"><div class="ai-label">' + label + ' Answer</div><div class="ai-body">' + renderMd(answer) + '</div></div>';
    }} catch (err) {{
      searchResults.innerHTML = '<div class="ai-answer"><div class="ai-label">Error</div><p>' + err.message + '</p></div>';
    }}
  }}
  if (e.key === 'Escape') {{ searchResults.style.display = 'none'; searchInput.value = ''; }}
}});

document.addEventListener('click', (e) => {{
  if (!e.target.closest('#search') && !e.target.closest('#search-results')) searchResults.style.display = 'none';
}});

</script>
</body>
</html>'''

    return html


# ─── Site Generation ─────────────────────────────────────────────────────────

def generate_site(data, full_text_data, output_dir):
    """Generate static site from structured data + full translation using deterministic template."""
    os.makedirs(output_dir, exist_ok=True)

    # Write data.json
    with open(os.path.join(output_dir, 'data.json'), 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    # Generate index.html from template
    print("Generating HTML site (deterministic template)...")
    html = render_html(data, full_text_data)

    with open(os.path.join(output_dir, 'index.html'), 'w') as f:
        f.write(html)

    # Validate output
    if not html.endswith('</html>'):
        print("  ⚠ HTML may be truncated — missing closing tag")
    else:
        print(f"  ✓ HTML complete ({len(html):,} bytes)")

    print(f"Site generated at {output_dir}/")


# ─── Main ────────────────────────────────────────────────────────────────────

# ─── Index Page Generator ────────────────────────────────────────────────────

def generate_index(repo_root='.'):
    """Generate top-level index.html from filings.json manifest with sector filters."""
    manifest_path = os.path.join(repo_root, 'filings.json')
    if not os.path.exists(manifest_path):
        print(f"Error: {manifest_path} not found")
        return

    with open(manifest_path) as f:
        filings = json.load(f)

    # Sort by filed date, newest first
    filings.sort(key=lambda f: f.get('filed', ''), reverse=True)

    # Collect unique sectors for filter chips
    sectors = sorted(set(f.get('sector', 'Other') for f in filings))

    # Build cards with data-sector attribute
    cards_html = ''
    for filing in filings:
        stats_html = ''
        for stat in filing.get('stats', []):
            color = stat.get('color', '')
            cls = f' class="stat-value {color}"' if color else ' class="stat-value"'
            stats_html += f'''      <div class="stat">
        <div class="stat-label">{esc(stat["label"])}</div>
        <div{cls}>{esc(stat["value"])}</div>
      </div>\n'''

        sector = esc(filing.get('sector', 'Other'))
        cards_html += f'''
  <a class="filing-card" href="{esc(filing["slug"])}/" data-sector="{sector}">
    <h2>{esc(filing["name_en"])}</h2>
    <div class="cn">{esc(filing["name_cn"])}</div>
    <div class="date">Filed {esc(filing["filed"])}</div>
    <div class="meta">{esc(filing.get("status", "Draft"))} &middot; {sector}</div>
    <div class="stats">
{stats_html}    </div>
  </a>
'''

    # Build filter chips
    filters_html = '<button class="filter-chip active" data-filter="all">All <span class="count">' + str(len(filings)) + '</span></button>\n'
    for sector in sectors:
        count = sum(1 for f in filings if f.get('sector', 'Other') == sector)
        filters_html += f'    <button class="filter-chip" data-filter="{esc(sector)}">{esc(sector)} <span class="count">{count}</span></button>\n'

    index_html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Filing Websites — IPO Prospectus Explorer</title>
<style>
:root {{
  --bg: #0d1117; --surface: #161b22; --border: #30363d;
  --text: #e6edf3; --text-muted: #8b949e;
  --accent: #58a6ff; --accent2: #3fb950; --accent3: #d29922;
}}
body.light {{
  --bg: #ffffff; --surface: #f6f8fa; --border: #d0d7de;
  --text: #1f2328; --text-muted: #656d76;
  --accent: #0969da; --accent2: #1a7f37; --accent3: #9a6700;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; background: var(--bg); color: var(--text); line-height: 1.6; min-height: 100vh; }}
a {{ color: var(--accent); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.container {{ max-width: 1100px; margin: 0 auto; padding: 0 24px; }}
header {{ background: var(--surface); border-bottom: 1px solid var(--border); padding: 16px 0; }}
header .container {{ display: flex; align-items: center; gap: 16px; }}
.logo {{ font-size: 1.1rem; font-weight: 700; }}
.logo span {{ color: var(--accent); }}
.header-actions {{ display: flex; gap: 8px; margin-left: auto; }}
.theme-toggle {{ background: var(--bg); border: 1px solid var(--border); color: var(--text-muted); padding: 5px 10px; border-radius: 6px; font-size: 0.75rem; cursor: pointer; }}
.theme-toggle:hover {{ border-color: var(--accent); color: var(--text); }}
.hero {{ padding: 48px 0 24px; text-align: center; }}
.hero h1 {{ font-size: 2rem; margin-bottom: 8px; }}
.hero p {{ color: var(--text-muted); font-size: 1rem; max-width: 600px; margin: 0 auto; }}
.filters {{ display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; padding: 20px 0; }}
.filter-chip {{ background: var(--surface); border: 1px solid var(--border); color: var(--text-muted); padding: 6px 14px; border-radius: 20px; font-size: 0.8rem; cursor: pointer; transition: all 0.2s; font-weight: 500; }}
.filter-chip:hover {{ border-color: var(--accent); color: var(--text); }}
.filter-chip.active {{ background: var(--accent); border-color: var(--accent); color: #fff; }}
.filter-chip .count {{ font-size: 0.7rem; opacity: 0.7; margin-left: 2px; }}
.filings {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; padding: 0 0 64px; }}
.filing-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 22px; transition: border-color 0.2s, transform 0.2s, opacity 0.3s; display: block; }}
.filing-card:hover {{ border-color: var(--accent); transform: translateY(-2px); text-decoration: none; }}
.filing-card.hidden {{ display: none; }}
.filing-card .exchange {{ font-size: 0.65rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; color: var(--accent); margin-bottom: 6px; }}
.filing-card h2 {{ font-size: 1.15rem; color: var(--text); margin-bottom: 3px; }}
.filing-card .cn {{ font-size: 0.85rem; color: var(--text-muted); margin-bottom: 10px; }}
.filing-card .date {{ font-size: 0.8rem; font-weight: 600; color: var(--text); margin-bottom: 4px; }}
.filing-card .meta {{ font-size: 0.7rem; color: var(--text-muted); margin-bottom: 14px; }}
.filing-card .stats {{ display: flex; gap: 16px; flex-wrap: wrap; }}
.filing-card .stat-label {{ font-size: 0.6rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-muted); }}
.filing-card .stat-value {{ font-size: 1rem; font-weight: 700; }}
.filing-card .stat-value.green {{ color: var(--accent2); }}
.filing-card .stat-value.yellow {{ color: var(--accent3); }}
.no-results {{ text-align: center; padding: 40px; color: var(--text-muted); display: none; }}
footer {{ padding: 32px 0; text-align: center; color: var(--text-muted); font-size: 0.75rem; border-top: 1px solid var(--border); }}
@media (max-width: 700px) {{
  .filings {{ grid-template-columns: 1fr; }}
  .hero h1 {{ font-size: 1.5rem; }}
}}
</style>
</head>
<body>
<header>
  <div class="container">
    <div class="logo"><span>Filing</span> Websites</div>
    <div class="header-actions">
      <button class="theme-toggle" id="themeToggle">Light</button>
    </div>
  </div>
</header>
<div class="container">
<div class="hero">
  <h1>IPO Prospectus Explorer</h1>
  <p>Translated, structured, and searchable Chinese tech IPO filings with LLM-powered Q&amp;A and machine-readable data.</p>
</div>
<div class="filters" id="filters">
    {filters_html}
</div>
<div class="filings" id="filings">
{cards_html}
</div>
<div class="no-results" id="noResults">No filings match this filter.</div>
</div>
<footer>
  <div class="container">
    <p>Translated summaries for reference only. For official filings, refer to the <a href="https://www.sse.com.cn">SSE</a> or <a href="https://www.hkex.com.hk">HKEX</a> websites. Not investment advice.</p>
    <p style="margin-top:8px;">Built by <a href="https://github.com/saranormous">Sarah Guo</a> and <a href="https://claude.ai">Claude</a> &middot; <a href="https://github.com/saranormous/filing-websites">Source</a></p>
  </div>
</footer>
<script>
(function() {{
  // Theme toggle
  var btn = document.getElementById('themeToggle');
  if (localStorage.getItem('theme') === 'light') {{ document.body.classList.add('light'); btn.textContent = 'Dark'; }}
  btn.addEventListener('click', function() {{
    document.body.classList.toggle('light');
    var isLight = document.body.classList.contains('light');
    btn.textContent = isLight ? 'Dark' : 'Light';
    localStorage.setItem('theme', isLight ? 'light' : 'dark');
  }});

  // Sector filters
  var chips = document.querySelectorAll('.filter-chip');
  var cards = document.querySelectorAll('.filing-card');
  var noResults = document.getElementById('noResults');
  chips.forEach(function(chip) {{
    chip.addEventListener('click', function() {{
      chips.forEach(function(c) {{ c.classList.remove('active'); }});
      chip.classList.add('active');
      var filter = chip.getAttribute('data-filter');
      var visible = 0;
      cards.forEach(function(card) {{
        if (filter === 'all' || card.getAttribute('data-sector') === filter) {{
          card.classList.remove('hidden');
          visible++;
        }} else {{
          card.classList.add('hidden');
        }}
      }});
      noResults.style.display = visible === 0 ? 'block' : 'none';
    }});
  }});
}})();
</script>
</body>
</html>'''

    index_path = os.path.join(repo_root, 'index.html')
    with open(index_path, 'w') as f:
        f.write(index_html)
    print(f"✓ Generated {index_path} with {len(filings)} filing cards, {len(sectors)} sector filters")


def estimate_cost(pdf_path):
    """Estimate API cost and time for processing a PDF."""
    full_text = extract_text(pdf_path)
    total_pages = get_page_count(pdf_path)
    sections = detect_sections(full_text)
    total_chars = sum(len(s['text']) for s in sections)

    # Estimate chunks (6K chars each)
    total_chunks = sum(max(1, len(s['text']) // 6000) for s in sections if len(s['text']) > 200)
    # Add 1 for structured data extraction
    total_api_calls = total_chunks + 1

    # Cost estimate (Sonnet: ~$3/1M input, ~$15/1M output)
    input_tokens_est = total_chars // 3  # rough chars-to-tokens
    output_tokens_est = input_tokens_est * 0.8  # translation is slightly shorter
    cost_est = (input_tokens_est * 3 + output_tokens_est * 15) / 1_000_000

    # Time estimate (~5s per API call + 1s delay between)
    time_est_min = total_api_calls * 6 / 60

    print(f"\n{'─' * 50}")
    print(f"  PDF: {os.path.basename(pdf_path)}")
    print(f"  Pages: {total_pages}")
    print(f"  Sections: {len(sections)}")
    print(f"  Total text: {total_chars:,} chars")
    print(f"  Estimated API calls: {total_api_calls}")
    print(f"  Estimated cost: ~${cost_est:.2f}")
    print(f"  Estimated time: ~{time_est_min:.0f} minutes")
    print(f"{'─' * 50}\n")

    return total_api_calls, cost_est, time_est_min


# ─── Main ────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 pipeline.py <pdf_path_or_url> <output_dir>   # Full pipeline")
        print("  python3 pipeline.py --translate-only <pdf> <dir>     # Translation only")
        print("  python3 pipeline.py --rebuild-index                  # Regenerate index.html from filings.json")
        print("  python3 pipeline.py --estimate <pdf>                 # Cost/time estimate")
        print("  python3 pipeline.py --render <dir>                   # Re-render site from existing data")
        print("  python3 pipeline.py --vision <pdf> <dir>             # Full pipeline with vision extraction")
        print("  python3 pipeline.py --eval <pdf> <dir>               # Compare text vs vision extraction")
        print()
        print("Examples:")
        print("  python3 pipeline.py prospectus.pdf mycompany")
        print("  python3 pipeline.py https://example.com/prospectus.pdf mycompany")
        print("  python3 pipeline.py --rebuild-index")
        sys.exit(1)

    # ── Rebuild index from manifest ──
    if '--rebuild-index' in sys.argv:
        generate_index('.')
        sys.exit(0)

    # ── Re-render from existing data ──
    if '--render' in sys.argv:
        args = [a for a in sys.argv[1:] if not a.startswith('--')]
        if not args:
            print("Usage: python3 pipeline.py --render <output_dir>")
            sys.exit(1)
        output_dir = args[0]
        data_path = os.path.join(output_dir, 'data.json')
        if not os.path.exists(data_path):
            print(f"Error: {data_path} not found")
            sys.exit(1)
        with open(data_path) as f:
            data = json.load(f)
        full_text = None
        for fname in ['full_text.json', 'translations.json']:
            fpath = os.path.join(output_dir, fname)
            if os.path.exists(fpath):
                with open(fpath) as f:
                    raw = json.load(f)
                if isinstance(raw, list):
                    full_text = {'sections': [{'id': f'chunk-{i}', 'title_en': _extract_title(c, i), 'content': c} for i, c in enumerate(raw)]}
                else:
                    full_text = raw
                break
        generate_site(data, full_text, output_dir)
        sys.exit(0)

    # ── Estimate cost ──
    if '--estimate' in sys.argv:
        args = [a for a in sys.argv[1:] if not a.startswith('--')]
        if not args:
            print("Usage: python3 pipeline.py --estimate <pdf_path_or_url>")
            sys.exit(1)
        pdf_path = resolve_input(args[0])
        estimate_cost(pdf_path)
        sys.exit(0)

    # ── Full pipeline or translate-only ──
    translate_only = '--translate-only' in sys.argv
    use_vision = '--vision' in sys.argv
    run_eval = '--eval' in sys.argv
    confirm = '--yes' not in sys.argv  # skip confirmation with --yes
    args = [a for a in sys.argv[1:] if not a.startswith('--')]

    input_path = args[0]
    output_dir = args[1] if len(args) > 1 else 'output-site'

    if not ANTHROPIC_API_KEY:
        print("Error: ANTHROPIC_API_KEY environment variable not set")
        print("  export ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)

    # Resolve URL → local file if needed
    pdf_path = resolve_input(input_path)

    # Verify PDF exists and is readable
    if not os.path.exists(pdf_path):
        print(f"Error: File not found: {pdf_path}")
        sys.exit(1)

    page_count = get_page_count(pdf_path)
    if page_count == 0:
        print("Error: Could not read PDF (0 pages). Is pdftotext/poppler installed?")
        print("  brew install poppler")
        sys.exit(1)

    print(f"Processing: {pdf_path} ({page_count} pages)")

    # Show cost estimate and confirm
    if confirm:
        total_calls, cost, time_min = estimate_cost(pdf_path)
        resp = input("Proceed? [Y/n] ").strip().lower()
        if resp and resp != 'y':
            print("Aborted.")
            sys.exit(0)

    if translate_only:
        os.makedirs(output_dir, exist_ok=True)
        full_text_data = translate_full_text(pdf_path, output_dir)
        with open(os.path.join(output_dir, 'full_text.json'), 'w') as f:
            json.dump(full_text_data, f, indent=2, ensure_ascii=False)
        total_chars = sum(len(s['content']) for s in full_text_data['sections'])
        print(f"\n✓ Full text translation complete: {len(full_text_data['sections'])} sections, {total_chars:,} chars")
    elif run_eval:
        # ── Eval mode: run all extraction methods and compare ──
        print("\n─── EVAL MODE: Text vs Vision vs Reducto ───")
        os.makedirs(output_dir, exist_ok=True)

        print("\n─── 1/3: Text extraction (pdftotext + LLM) ───")
        text_data = extract_structured_data(pdf_path)
        validate_data(text_data)
        with open(os.path.join(output_dir, 'data_text.json'), 'w') as f:
            json.dump(text_data, f, indent=2, ensure_ascii=False)

        print("\n─── 2/3: Vision extraction (PDF pages → Claude) ───")
        vision_data = extract_structured_data_vision(pdf_path)
        validate_data(vision_data)
        with open(os.path.join(output_dir, 'data_vision.json'), 'w') as f:
            json.dump(vision_data, f, indent=2, ensure_ascii=False)

        reducto_data = None
        if os.environ.get('REDUCTO_API_KEY'):
            print("\n─── 3/3: Reducto extraction (Reducto parse → LLM) ───")
            reducto_data = extract_structured_data_reducto(pdf_path)
            validate_data(reducto_data)
            with open(os.path.join(output_dir, 'data_reducto.json'), 'w') as f:
                json.dump(reducto_data, f, indent=2, ensure_ascii=False)
        else:
            print("\n─── 3/3: Reducto extraction — SKIPPED (no REDUCTO_API_KEY) ───")

        # Compare text vs vision
        print("\n─── Text vs Vision ───")
        report_tv = compare_extractions(text_data, vision_data)
        print_eval_report(report_tv)

        # Compare vision vs reducto if available
        if reducto_data:
            print("\n─── Vision vs Reducto ───")
            report_vr = compare_extractions(vision_data, reducto_data)
            # Relabel for clarity
            for d in report_vr['dimensions']:
                if d['winner'] == 'text':
                    d['winner'] = 'vision'
                elif d['winner'] == 'vision':
                    d['winner'] = 'reducto'
            report_vr['summary']['vision_wins'] = report_vr['summary'].pop('text_wins', 0)
            report_vr['summary']['reducto_wins'] = report_vr['summary'].pop('vision_wins', 0)
            print_eval_report(report_vr)

        # Save combined report
        combined_report = {
            'text_vs_vision': report_tv,
            'vision_vs_reducto': report_vr if reducto_data else None,
        }
        with open(os.path.join(output_dir, 'eval_report.json'), 'w') as f:
            json.dump(combined_report, f, indent=2, ensure_ascii=False)

        print(f"\nSaved: {output_dir}/data_text.json, data_vision.json" + (", data_reducto.json" if reducto_data else "") + ", eval_report.json")

    else:
        # Step 1: Extract structured data
        print("\n─── Step 1: Extracting structured data ───")
        os.makedirs(output_dir, exist_ok=True)
        data_path = os.path.join(output_dir, 'data.json')

        # Load existing data.json if present (don't clobber previous enrichment)
        existing_data = {}
        if os.path.exists(data_path):
            with open(data_path) as f:
                existing_data = json.load(f)

        # Use vision or text extraction
        if use_vision:
            data = extract_structured_data_vision(pdf_path)
        else:
            data = extract_structured_data(pdf_path)
        validate_data(data)
        print(f"  Extracted: {len(data.get('key_risks', []))} risks, {len(data.get('shareholders_pre_ipo', []))} shareholders")

        # Merge: new extraction wins for non-empty fields, preserve existing for fields the new extraction missed
        for key in existing_data:
            if key not in data or not data[key]:
                data[key] = existing_data[key]
            elif isinstance(data[key], list) and len(data[key]) == 0 and existing_data[key]:
                data[key] = existing_data[key]
            elif isinstance(data[key], dict):
                for subkey in existing_data[key]:
                    if subkey not in data[key] or not data[key][subkey]:
                        data[key][subkey] = existing_data[key][subkey]

        # Save data.json
        with open(data_path, 'w') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        # Step 2: Full translation (resumable)
        print("\n─── Step 2: Full text translation ───")
        full_text_data = translate_full_text(pdf_path, output_dir)
        validate_full_text(full_text_data)
        total_chars = sum(len(s['content']) for s in full_text_data['sections'])
        print(f"  Translated: {len(full_text_data['sections'])} sections, {total_chars:,} chars")

        # Save final full_text.json (overwrites checkpoint)
        with open(os.path.join(output_dir, 'full_text.json'), 'w') as f:
            json.dump(full_text_data, f, indent=2, ensure_ascii=False)

        # Step 3: Executive summary
        print("\n─── Step 3: Executive summary ───")
        if not data.get('executive_summary'):
            data['executive_summary'] = generate_executive_summary(data, full_text_data)
        else:
            print("  Already exists, skipping")

        # Save final data.json (with summary)
        with open(data_path, 'w') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        # Step 4: Generate site (deterministic template)
        print("\n─── Step 4: Generating site ───")
        generate_site(data, full_text_data, output_dir)

        print(f"\n✓ Pipeline complete! Site at {output_dir}/")
        print(f"  - index.html: summary + full translation + search")
        print(f"  - data.json: structured data for agents")
        print(f"  - full_text.json: raw translation data")

    print("\nDone!")
