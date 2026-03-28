# CLAUDE.md — Filing Websites

## Project Overview
Static GitHub Pages site hosting translated, searchable Chinese tech IPO prospectuses. Each filing gets its own subdirectory with `index.html`, `data.json`, and `full_text.json`.

## Architecture
- **Zero dependencies** — pure HTML/CSS/JS, no build step
- **GitHub Pages** deploys from `main` branch root
- All filing sites share the same feature set (see below)
- `pipeline.py` generates new sites from Chinese PDF prospectuses via Claude API
- **Deterministic HTML template** — `render_html()` in pipeline.py uses Python string formatting, not LLM-generated HTML. Every site gets identical CSS/JS/layout.
- **`filings.json`** — single source of truth for all filings. Top-level `index.html` is auto-generated from it via `--rebuild-index`, sorted newest-first with sector filter chips.
- **Multi-pass extraction** — structured data is extracted in 4 targeted passes (overview, shareholders, financials, risks) to maximize data coverage.

## Required Features — ALL Filing Pages Must Include
Every `{company}/index.html` MUST have all of these. When modifying one site, update the template in `render_html()` and re-render ALL sites via `make render-all`:

1. **AI-generated executive summary** — narrative summary at top of summary tab, labeled "AI-Generated Summary", with all monetary figures in USD
2. **Dark/light theme toggle** — CSS variables via `[data-theme="light"]` + localStorage persistence
3. **All financials in USD** — pre-rendered at build time via `_get_unit_multiplier()`. Handles 万元, thousands USD, HKD. Displayed as $B/$M/$K format. No client-side currency toggle.
4. **LLM-powered search** — dual provider support (Anthropic Claude Haiku + OpenAI GPT-4o-mini)
   - Keyword search on input (instant, no API key needed)
   - LLM answer on Enter with markdown rendering (requires API key stored in localStorage)
   - 3-second rate limit between LLM calls
5. **Collapsible sections** with chevron indicators
6. **Quick links** bar at top for section navigation
7. **Summary + Full Translation tabs** with cross-links via `jumpToFull()`
8. **Full translation accordion** — content split at major `<h2>` headings into collapsible sections (all collapsed by default). TOC at top with `expandAndScroll()` links. Unclosed table tags auto-sanitized before splitting.
9. **KPI cards** with key financial highlights in USD
10. **`data.json`** — machine-readable structured data including `executive_summary` field
11. **Sticky header** with search, company name (links back to index), toggles
12. **Footer** with disclaimer
13. **Responsive** — works on mobile

## Filing Sites
- `unitree/` — Unitree Technology (SSE STAR Market, robotics)
- `zhipu/` — Zhipu AI (HKEX Ch.18C, foundation models/AGI)
- `minimax/` — MiniMax (HKEX Ch.18C, foundation models/agents)
- `biren/` — Biren Technology (HKEX Ch.18C, AI chips)
- `cambricon/` — Cambricon Technologies (SSE STAR Market, AI chips)
- `horizon/` — Horizon Robotics (HKEX, autonomous driving)
- `blacksesame/` — Black Sesame Technologies (HKEX Ch.18C, auto AI SoCs)
- `catl/` — CATL (HKEX, batteries/energy storage)
- `eve-energy/` — EVE Energy (HKEX, batteries)
- `sunwoda/` — Sunwoda Electronic (HKEX, batteries)

## Consistency Rules
- When modifying a feature: update `render_html()` in pipeline.py, then `make render-all`
- When adding a new filing: add entry to `filings.json`, run the pipeline, then `make index`
- Search JS, CSS, and toggles are all in the template — guaranteed consistent
- `data.json` schema should follow the same structure across filings
- All monetary values displayed as USD by default; `rmb-val` class required for conversion

## Pipeline Usage

### Makefile (preferred)
```bash
make add PDF=prospectus.pdf SLUG=mycompany    # Full pipeline + rebuild index
make estimate PDF=prospectus.pdf              # Cost/time estimate before running
make render SLUG=mycompany                    # Re-render one site from existing data
make render-all                               # Re-render all sites + rebuild index
make index                                    # Rebuild index.html from filings.json
make push MSG="Add mycompany"                 # Commit and push
```

### Direct CLI
```bash
export ANTHROPIC_API_KEY=sk-ant-...

python3 pipeline.py <pdf_or_url> <slug>          # Full pipeline (with cost confirmation)
python3 pipeline.py --yes <pdf_or_url> <slug>     # Skip confirmation
python3 pipeline.py --translate-only <pdf> <dir>  # Translation only
python3 pipeline.py --estimate <pdf_or_url>       # Cost/time estimate
python3 pipeline.py --render <slug>               # Re-render from existing data
python3 pipeline.py --rebuild-index               # Generate index.html from filings.json
```

### Pipeline steps
1. Resolves input (downloads PDF if URL)
2. Shows cost estimate and asks for confirmation (skip with `--yes`)
3. Multi-pass structured data extraction (4 targeted API calls for overview, shareholders, financials, risks)
4. Translates full document section-by-section → `full_text.json` (with checkpointing)
5. Generates executive summary (1 API call, stored in `data.json`)
6. Renders `index.html` from deterministic Python template
7. Validates: JSON integrity, translation completeness, HTML completeness

### Resumable translations
Translations checkpoint `full_text.json` after each section. If the pipeline crashes mid-translation, restarting it resumes from the last completed section.

### Section detection
- **SSE filings**: matches `第X节 ...` patterns (very specific)
- **HKEX filings**: matches standalone ALL-CAPS headings like `RISK FACTORS` on their own line. Requires exact line match to avoid mid-paragraph false positives. Consecutive same-ID sections are merged.

## File Formats
- `data.json` — structured financials, shareholders, products, metadata, executive_summary
- `translations.json` — old format: list of HTML string chunks (unitree, cxmt)
- `full_text.json` — new format: `{sections: [{id, title_en, content}, ...]}` (pipeline output)
- `filings.json` — manifest of all filings (generates index.html with sector filters)
- The template's `render_html()` handles both translation formats (detects HTML vs plain text)

## Common Gotchas
- HKEX Chapter 18C draft filings often redact financial data and shareholder percentages
- API keys in search are per-visitor (localStorage), never in the repo
- `index.html` at root is auto-generated — edit `filings.json` instead
- Translation files can be either HTML chunks (old) or plain text sections (new) — template auto-detects
- Requires `poppler` for PDF text extraction (`brew install poppler`)
- Python f-strings in pipeline.py: use `\\n` for JS regex `\n`, `{{` for JS `{`
- Currency conversion: all `rmb-val` values are treated as 万 (10K RMB) units
