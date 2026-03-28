"""Translation functions for IPO prospectus PDFs."""

import json
import os
import re
import time

from lib.common import (
    extract_text, get_page_count, call_claude,
    call_claude_vision, split_pdf_to_chunks,
)


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


def translate_full_text_vision(pdf_path, output_dir=None):
    """Translate full prospectus using vision — Claude reads actual PDF pages.

    Sends batches of 30 pages as PDF document blocks. Preserves table layout.
    Supports resuming from checkpoint.
    """
    total_pages = get_page_count(pdf_path)
    print(f"Vision translation: {total_pages} pages")

    pages_per_batch = 30
    chunks = split_pdf_to_chunks(pdf_path, pages_per_chunk=pages_per_batch)
    print(f"  Split into {len(chunks)} batches of ~{pages_per_batch} pages")

    # Load checkpoint if resuming
    checkpoint_path = os.path.join(output_dir, 'full_text.json') if output_dir else None
    translated_sections = []
    resume_from = 0
    if checkpoint_path and os.path.exists(checkpoint_path):
        try:
            with open(checkpoint_path) as f:
                existing = json.load(f)
            translated_sections = existing.get('sections', [])
            resume_from = len(translated_sections)
            if resume_from > 0:
                total_chars = sum(len(s.get('content', '')) for s in translated_sections)
                print(f"  Resuming from batch {resume_from + 1}/{len(chunks)} ({total_chars:,} chars already translated)")
        except (json.JSONDecodeError, KeyError):
            translated_sections = []
            resume_from = 0

    system = """You are a professional translator specializing in Chinese financial and legal documents.
Translate ALL visible text on these prospectus pages into clear, accurate English.

Rules:
- Translate EVERYTHING faithfully — do not summarize or skip content
- Preserve headings: output them as ## Heading or ### Subheading
- Preserve tables: output them as markdown tables with | column | separators |
- Keep Chinese company names in both English and Chinese (e.g. "Unitree Technology (宇树科技)")
- Keep financial figures in their original units with English equivalents where helpful
- Mark major section boundaries with: ===SECTION: Section Title===
- Output ONLY the translated text, no commentary"""

    for batch_idx, (b64, start_page, end_page) in enumerate(chunks):
        if batch_idx < resume_from:
            continue

        print(f"\n  Batch {batch_idx + 1}/{len(chunks)}: pages {start_page}-{end_page}...", end=' ', flush=True)

        translated = call_claude_vision(
            system,
            b64,
            f"Translate all visible text on these prospectus pages (pages {start_page}-{end_page}) from Chinese to English. Preserve all table formatting.",
            max_tokens=16000
        )
        print(f"→ {len(translated):,} chars")

        translated_sections.append({
            'id': f'pages-{start_page}-{end_page}',
            'title_en': f'Pages {start_page}–{end_page}',
            'content': translated
        })

        # Checkpoint
        if checkpoint_path:
            os.makedirs(output_dir, exist_ok=True)
            _save_checkpoint(checkpoint_path, translated_sections, total_pages)

        time.sleep(2)  # Rate limit for vision calls

    # Post-process: try to split by ===SECTION=== markers for better structure
    final_sections = []
    for sec in translated_sections:
        content = sec['content']
        parts = re.split(r'===SECTION:\s*(.*?)===', content)
        if len(parts) > 1:
            # Preamble before first marker
            if parts[0].strip():
                final_sections.append({
                    'id': sec['id'] + '-pre',
                    'title_en': sec['title_en'],
                    'content': parts[0].strip()
                })
            # Section marker pairs
            i = 1
            while i < len(parts) - 1:
                title = parts[i].strip()
                body = parts[i + 1].strip() if i + 1 < len(parts) else ''
                if body:
                    final_sections.append({
                        'id': re.sub(r'[^a-z0-9]+', '-', title.lower())[:40],
                        'title_en': title,
                        'content': body
                    })
                i += 2
        else:
            final_sections.append(sec)

    result = {
        'sections': final_sections,
        'total_pages': total_pages,
        'translation_model': 'claude-sonnet-4-6',
        'translation_method': 'vision',
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
