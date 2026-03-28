# PDF Extraction Eval: Text vs Vision vs Reducto

Tested on Unitree Technology IPO Prospectus (363 pages, SSE STAR Market, Chinese).

## Results

| Dimension | Text (pdftotext + LLM) | Vision (Claude PDF) | Reducto + LLM |
|-----------|:---:|:---:|:---:|
| Shareholders extracted | 41 | **46** | 49 |
| Shareholders with % | 4 | **46** | 0 |
| Income periods | 2 | **5** | 1 |
| Periods with revenue | 1 | **5** | 1 |
| Balance sheet periods | 0 | **5** | 0 |
| Risk factors | 10 | **14** | 12 |
| Products | 8 | **12** | 8 |
| Metadata completeness | 4/5 | **5/5** | 5/5 |

**Winner: Vision**, across all financial data dimensions.

## How Each Method Works

**Text** (`pdftotext` → regex section detection → 4-pass LLM extraction on text chunks): Cheapest (~$8-10/filing). Loses all table structure — financial tables become garbled text columns. LLM can extract risks and company overview but struggles to reconstruct tables from mangled text.

**Vision** (targeted page finding via `pdftotext` keywords → send PDF pages directly to Claude as document blocks): Best quality (~$13-15/filing). Claude sees the actual rendered pages — tables, charts, footnotes, layout. Targeted page finding keeps cost manageable (sends ~300 pages across 4 passes, not all 500).

**Reducto** (Reducto API parses PDF → structured markdown with tables → LLM extraction on parsed output): Reducto itself is excellent — found 365 perfectly formatted markdown tables with row/column structure preserved. But the LLM pass on top failed because it received all 365 tables and couldn't identify which ones were financial statements vs definitions vs TOC entries. The shareholder extraction pass failed entirely — the LLM refused to output JSON and wrote an explanation instead.

## Why Reducto Underperformed

Reducto's parsing quality is high. The problem is pipeline design:

1. **Table selection.** 365 tables sent as a wall of markdown is too much noise. Vision avoids this by sending only the 50-100 pages where keywords appear.
2. **No block-level filtering.** Reducto provides block metadata (type, page number, bounding box) that could be used to filter to only the relevant tables. The pipeline didn't use this — it should filter blocks by proximity to financial/shareholder keywords before sending to the LLM.
3. **Different failure mode.** Text and vision fail gracefully (extract partial data). Reducto + naive LLM fails catastrophically (LLM refuses to output JSON when overwhelmed by irrelevant tables).

A fixed Reducto pipeline that pre-filters blocks by type and keyword proximity would likely match or approach vision quality at a lower per-page cost ($0.015/page for Reducto vs ~$0.005/page for vision tokens).

## Cost Comparison

| Method | Extraction Cost | Translation Cost | Total |
|--------|:-:|:-:|:-:|
| Text | ~$0.50 | ~$8 | ~$8-10 |
| Vision | ~$2.50 | ~$11 | ~$13-15 |
| Reducto | ~$5.50 + $2 LLM | ~$8 | ~$15-18 |

## Recommendation

Default to **vision** for extraction. It's the simplest, most accurate, and reasonably priced. Use text as a cheap fallback. Reducto is worth revisiting with block-level filtering for production scale.
