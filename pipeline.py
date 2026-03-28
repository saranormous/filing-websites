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
import sys
import os

# ─── Re-export everything from lib modules so existing imports work ──────────
from lib.common import (
    ANTHROPIC_API_KEY,
    resolve_input, extract_text, get_page_count,
    call_claude, strip_code_fences,
    call_claude_vision, _call_and_parse_json, _call_and_parse_json_vision,
    split_pdf_to_chunks, find_page_ranges_for_keywords, _make_pdf_chunk_for_ranges,
    esc, fmt_num,
)

from lib.extract import (
    _find_sections_by_keywords,
    extract_structured_data,
    extract_structured_data_vision,
    extract_structured_data_reducto,
    compare_extractions, print_eval_report,
    generate_executive_summary,
)

from lib.translate import (
    SSE_SECTION_PATTERNS, HKEX_SECTION_PATTERNS, SECTION_PATTERNS,
    detect_sections,
    translate_chunk, translate_full_text,
    translate_full_text_vision,
    _save_checkpoint,
)

from lib.render import (
    _extract_title, _is_top_level_heading, _get_unit_multiplier,
    render_html,
    generate_site,
    generate_index, update_filings_stats,
)

from lib.validate import (
    validate_data,
    validate_full_text,
)


# ─── Cost Estimation (uses functions from multiple modules) ──────────────────

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
        update_filings_stats('.')
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
        if use_vision:
            full_text_data = translate_full_text_vision(pdf_path, output_dir)
        else:
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
