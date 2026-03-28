# PDF Extraction Eval: Text vs Vision vs Reducto

Tested on Unitree Technology IPO Prospectus (363 pages, SSE STAR Market, Chinese).

## Results

| Dimension | Text (pdftotext + LLM) | Vision (Claude PDF) | Reducto + LLM |
|-----------|:---:|:---:|:---:|
| Shareholders extracted | 41 | **46** | 32 |
| Shareholders with % | 4 | **46** | 32 |
| Income periods with revenue | 1 | **5** | **5** |
| Balance sheet periods | 0 | **5** | **5** |
| Risk factors | 10 | **14** | **14** |
| Products | 8 | **12** | 8 |
| Metadata completeness | 4/5 | **5/5** | **5/5** |

**Winner: Vision**, particularly for shareholder data. Reducto ties on financials and risks after fixing table selection.

## How Each Method Works

**Text** (`pdftotext` → regex section detection → 4-pass LLM extraction on text chunks): Cheapest (~$8-10/filing). Loses all table structure — financial tables become garbled text columns. LLM can extract risks and company overview but struggles to reconstruct tables from mangled text.

**Vision** (targeted page finding via `pdftotext` keywords → send PDF pages directly to Claude as document blocks): Best quality (~$13-15/filing). Claude sees the actual rendered pages — tables, charts, footnotes, layout. Targeted page finding keeps cost manageable (sends ~300 pages across 4 passes, not all 500).

**Reducto** (Reducto API parses PDF → structured markdown with table preservation → block-level filtering → LLM extraction): Good quality, competitive on financials. Reducto found 365 tables with clean markdown formatting. Block-level filtering narrows to the relevant tables before sending to the LLM.

## Reducto: What Worked and What Didn't

Reducto's PDF parsing is excellent — 365 tables, 4,103 blocks, each with type and page metadata. The challenge was *table selection*: getting the right tables to the right LLM prompt.

**What worked (after fixing):**
- **Financial tables:** Block-level filtering by keywords + proximity found 182 tables near financial sections. LLM extracted 5 income periods and 5 balance sheet periods — matching vision.
- **Risks:** Text keyword search works well since risks are paragraph text, not tables.

**What was hard:**
- **Shareholder cap table:** The prospectus has dozens of tables with "持股比例" (shareholding %) — most are fund composition tables listing LPs in VC funds, not the company's actual cap table. Required filtering by controller name and company references to isolate the real cap table. Still extracted 32 vs vision's 46.
- **Table disambiguation in general:** A Chinese prospectus reuses the same terms (股东, 持股) in many contexts. Vision handles this naturally because Claude sees the full page context. Reducto's block-level extraction loses inter-block context.

## Iterations

| Version | Shareholders with % | Income periods | What changed |
|---------|:---:|:---:|---|
| Reducto v1 (naive) | 0 | 0 | Sent all 365 tables, LLM overwhelmed |
| Reducto v2 (keyword proximity) | 0 | 5 | Block-level filtering by page proximity. Fixed financials, shareholders still failing |
| Reducto v3 (cap table filter) | 0 | 5 | Filtered for tables with 3+ % signs. Still too many fund composition tables |
| Reducto v4 (company-specific) | 32 | 5 | Filter by controller/company name in table content. Finds the real cap table |

## Cost Comparison

| Method | Extraction Cost | Notes |
|--------|:-:|---|
| Text | ~$0.50 | Cheapest, lowest quality |
| Vision | ~$2.50 | Best quality, simple pipeline |
| Reducto | ~$5.50 + $2 LLM | Most complex, requires careful table selection |

## Recommendation

**Vision** is the default for extraction — simplest pipeline, best quality, reasonable cost. The LLM sees the actual document and disambiguates table types naturally.

**Reducto** is worth using when: (1) you need the structured text output for other purposes (not just extraction), (2) you're processing at scale and want to cache parsed results, or (3) the PDF has OCR issues that Reducto's multi-pass pipeline handles better.

**Text** is a cheap fallback for quick estimates or when vision API is unavailable.
