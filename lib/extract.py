"""Structured data extraction from IPO prospectus PDFs."""

import json
import os
import re
import time
import urllib.request

from lib.common import (
    extract_text, get_page_count, call_claude,
    strip_code_fences, call_claude_vision,
    _call_and_parse_json, _call_and_parse_json_vision,
    split_pdf_to_chunks, find_page_ranges_for_keywords,
    _make_pdf_chunk_for_ranges,
)


def _cap_page_ranges(ranges, max_pages=40):
    """Cap page ranges to max total pages, keeping earliest ranges first."""
    capped = []
    total = 0
    for s, e in ranges:
        pages = e - s + 1
        if total + pages > max_pages:
            remaining = max_pages - total
            if remaining > 0:
                capped.append((s, s + remaining - 1))
            break
        capped.append((s, e))
        total += pages
    return capped


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
        # Cap to max 40 pages to avoid drowning the LLM
        sh_ranges = _cap_page_ranges(sh_ranges, max_pages=40)
        total_sh_pages = sum(e - s + 1 for s, e in sh_ranges)
        print(f"    Using {len(sh_ranges)} ranges ({total_sh_pages} pages)")
        sh_b64 = _make_pdf_chunk_for_ranges(pdf_path, sh_ranges)
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
        fin_ranges = _cap_page_ranges(fin_ranges, max_pages=40)
        total_fin_pages = sum(e - s + 1 for s, e in fin_ranges)
        print(f"    Using {len(fin_ranges)} ranges ({total_fin_pages} pages)")
        fin_b64 = _make_pdf_chunk_for_ranges(pdf_path, fin_ranges)
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
        risk_ranges = _cap_page_ranges(risk_ranges, max_pages=40)
        total_risk_pages = sum(e - s + 1 for s, e in risk_ranges)
        print(f"    Using {len(risk_ranges)} ranges ({total_risk_pages} pages)")
        risk_b64 = _make_pdf_chunk_for_ranges(pdf_path, risk_ranges)
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
