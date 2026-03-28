# Filing Websites

Static, searchable financial filing websites with LLM-powered Q&A. Translates Chinese IPO prospectuses into structured English sites.

**Live:** [saranormous.github.io/filing-websites](https://saranormous.github.io/filing-websites/)

## Filings

| Company | Sector | Exchange | Filed | Site |
|---------|--------|----------|-------|------|
| Unitree Technology (宇树科技) | Robotics | SSE STAR Market | 2026-03-20 | [/unitree/](https://saranormous.github.io/filing-websites/unitree/) |
| Sunwoda Electronic (欣旺达) | Energy / Batteries | HKEX | 2026-01-30 | [/sunwoda/](https://saranormous.github.io/filing-websites/sunwoda/) |
| EVE Energy (亿纬锂能) | Energy / Batteries | HKEX | 2026-01-02 | [/eve-energy/](https://saranormous.github.io/filing-websites/eve-energy/) |
| MiniMax (MiniMax集团) | Foundation Models | HKEX Ch.18C | 2025-12-31 | [/minimax/](https://saranormous.github.io/filing-websites/minimax/) |
| Zhipu AI (智谱华章) | Foundation Models | HKEX Ch.18C | 2025-12-30 | [/zhipu/](https://saranormous.github.io/filing-websites/zhipu/) |
| Biren Technology (壁仞科技) | AI Chips | HKEX Ch.18C | 2025-12-22 | [/biren/](https://saranormous.github.io/filing-websites/biren/) |
| CATL (宁德时代) | Energy / Batteries | HKEX | 2025-05-12 | [/catl/](https://saranormous.github.io/filing-websites/catl/) |
| Horizon Robotics (地平线) | Autonomous Driving | HKEX | 2024-10-16 | [/horizon/](https://saranormous.github.io/filing-websites/horizon/) |
| Black Sesame (黑芝麻智能) | Autonomous Driving | HKEX Ch.18C | 2024-07-31 | [/blacksesame/](https://saranormous.github.io/filing-websites/blacksesame/) |
| Cambricon (寒武纪) | AI Chips | SSE STAR Market | 2020-06-03 | [/cambricon/](https://saranormous.github.io/filing-websites/cambricon/) |

## Features

- **AI-generated executive summary** — narrative overview with USD figures, key investors, risks
- **LLM-powered search** — ask natural language questions, answered by Claude Haiku. Keyword fallback (no API key needed).
- **Currency toggle** — defaults to USD. Converts all 万元 values to readable $B/$M/$K format.
- **Light/dark mode** — toggle with preference saved to localStorage.
- **Summary + Full Translation** — summary tab with KPI cards; full translation tab with linked table of contents.
- **Machine-readable data** — `data.json` with structured financials, cap table, executive summary.
- **Sector filters** — index page filters by AI Chips, Autonomous Driving, Energy, Foundation Models, Robotics.
- **Zero dependencies** — pure HTML/CSS/JS, works on GitHub Pages with no build step.

## Pipeline

Deterministic template with multi-pass extraction. The LLM translates, structures, and summarizes — HTML generation is pure Python string formatting.

### Adding a new filing

```bash
export ANTHROPIC_API_KEY=sk-ant-...

# 1. Check cost estimate
make estimate PDF=https://hkexnews.hk/path/to/prospectus.pdf

# 2. Run the full pipeline (downloads PDF, extracts, translates, renders)
make add PDF=https://hkexnews.hk/path/to/prospectus.pdf SLUG=deepseek

# 3. Add the card to filings.json
#    Edit filings.json: add name_en, name_cn, exchange, sector, filed date, stats

# 4. Rebuild index and push
make index
make push MSG="Add DeepSeek"
```

If the pipeline crashes mid-translation, run the same command again — it resumes from the last checkpoint.

### All commands

```bash
make add PDF=<file_or_url> SLUG=<name>  # Full pipeline + rebuild index
make estimate PDF=<file_or_url>         # Cost/time estimate
make render SLUG=<name>                 # Re-render one site from existing data
make render-all                         # Re-render all sites + rebuild index
make index                              # Rebuild index.html from filings.json
make push MSG="commit message"          # Commit and push
```

### Pipeline steps

1. **Ingest** — download PDF (if URL) and extract text with `pdftotext`
2. **Estimate** — show cost/time, confirm
3. **Structure** — 4-pass targeted extraction (overview, shareholders, financials, risks) → `data.json`
4. **Translate** — full document section-by-section → `full_text.json` (resumable)
5. **Summarize** — AI-generated executive summary → `data.json`
6. **Render** — deterministic template → `index.html`
7. **Validate** — check completeness

### GitHub Action

Go to **Actions → Process IPO Filing → Run workflow** and provide PDF URL, company slug, name, exchange, and sector.

## LLM Search

Requires an [Anthropic API key](https://console.anthropic.com/settings/keys) or OpenAI key. Stored in browser localStorage only. Keyword search works without any key.
