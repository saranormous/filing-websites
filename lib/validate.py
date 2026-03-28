"""Validation functions for extracted data and translations."""

import re


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
