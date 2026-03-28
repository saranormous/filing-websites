"""HTML rendering and site generation functions."""

import json
import os
import re

from lib.common import esc, fmt_num


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


def _is_top_level_heading(text):
    """Check if a heading is a top-level section (not a sub-section)."""
    if re.match(r'^[\(\（]?[IVXivx]+[\.\)）]', text):
        return False
    if re.match(r'^[\(\（]\d+[\)）]', text):
        return False
    if re.match(r'^\d+\.\d+', text):
        return False
    return True


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


# ─── Index Page Generator ────────────────────────────────────────────────────

def update_filings_stats(repo_root='.'):
    """Update filings.json stats from each company's data.json."""
    manifest_path = os.path.join(repo_root, 'filings.json')
    if not os.path.exists(manifest_path):
        return

    with open(manifest_path) as f:
        filings = json.load(f)

    updated = 0
    for filing in filings:
        slug = filing.get('slug', '')
        data_path = os.path.join(repo_root, slug, 'data.json')
        if not os.path.exists(data_path):
            continue

        with open(data_path) as f:
            data = json.load(f)

        unit_mult, is_usd = _get_unit_multiplier(data)

        def _fmt_usd(val):
            if val is None:
                return None
            full = float(val) * unit_mult
            usd = full if is_usd else full / 7.25
            if abs(usd) >= 1e9:
                return f'${usd/1e9:.1f}B'
            elif abs(usd) >= 1e6:
                return f'${usd/1e6:.0f}M'
            elif abs(usd) >= 1e3:
                return f'${usd/1e3:.0f}K'
            return f'${usd:,.0f}'

        # Build stats from data.json
        new_stats = []
        fin = data.get('financials', {})
        inc = fin.get('income_statement', [])
        if inc:
            latest = inc[-1]
            if latest.get('revenue'):
                val = _fmt_usd(latest['revenue'])
                if val:
                    new_stats.append({'label': f'Revenue ({latest.get("period", "")})', 'value': val})
            if latest.get('gross_margin_pct'):
                new_stats.append({'label': 'Gross Margin', 'value': f'{latest["gross_margin_pct"]}%', 'color': 'green'})

        sh = data.get('shareholders_pre_ipo', [])
        if sh:
            sh_with_pct = sum(1 for s in sh if s.get('pct'))
            if sh_with_pct > 0:
                top = max(sh, key=lambda s: s.get('pct') or 0)
                new_stats.append({'label': 'Top Shareholder', 'value': top.get('name', '?')[:20], 'color': 'yellow'})

        if not new_stats:
            # Fallback: use existing stats
            continue

        filing['stats'] = new_stats
        # Also update filing date from data if available
        filing_date = data.get('meta', {}).get('filing_date')
        if filing_date:
            filing['filed'] = filing_date

        updated += 1

    with open(manifest_path, 'w') as f:
        json.dump(filings, f, indent=2, ensure_ascii=False)

    if updated:
        print(f"✓ Updated stats for {updated} filings in filings.json")


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
