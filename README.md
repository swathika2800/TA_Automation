# SCOUT - Candidate Sourcing Pipeline

Automated candidate sourcing and AI screening for the 10D TA team.
Built per the SCOUT specification (supersedes the original `TA_Automation_Proposal.docx`).

## Pipeline (per JD)

```
Excel JD
  -> Claude boolean query generator  (TA hint + max 2 regenerations)
  -> Playwright: Naukri login -> Resdex search
  -> Apply filters: exp + location + CTC + freshness (45d) + NP
  -> Paginate, dedup by (name + current_company)
  -> Hard cap: 50 resumes per JD
  -> Parse PDFs (PyMuPDF + pdfplumber)
  -> 6-Layer Quality Gate (Claude Haiku)         <-- Phase 2
  -> Write to outputs/scout_<JD_ID>_<timestamp>.xlsx
  -> Two tabs: Candidates (with QG scores), JD_Summary
```

## Key SCOUT Spec Decisions

| # | Decision | Choice |
|---|---|---|
| 1 | Quality Gate | **Sourcing + 6-Layer scoring** in v1 |
| 2 | Output | **Local Excel** (Google Sheets deferred — needs service-account JSON) |
| 3 | CTC source | **CTC_Ceiling_Lacs** column in JD Excel |
| 4 | Hard cap | **50 resumes per JD** |
| 5 | Dedup | (name + current_company), case-insensitive |
| 6 | Boolean gen | Claude first, static fallback on error |
| 7 | Regenerations | max 2 (`--regenerate` flag) |
| 8 | TA hint | `--hint "text"` CLI flag |
| 9 | Cron | Off for v1 — TA-initiated |
| 10 | Rejected PII | **No PII** — no log, no PDF, no profile JSON |

## 6-Layer Quality Gate

| Layer | Gate | Max Points | Hard Reject? |
|---|---|---|---|
| L1 | Project R&R Match | 25 | YES if 0 |
| L2 | Role Ownership | -10 (penalty) | No (flag) |
| L3 | Environment Fit | 10 | No (flag if 0) |
| L4 | Project Depth | 20 | YES if 0 (vague 5+yr) |
| L5 | (TBD per spec) | 15 | YES if 0 |
| L6 | (TBD per spec) | 20 | No (flag if 0) |

`Hard_Reject = YES` rows are NOT written to the Excel.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# 1) Place the company-maintained JD sheet at:
#      jd_input/jds.xlsx
#    (a reference sample with the full schema is at jd_input/jds_sample.xlsx)
#
# 2) Dry run (M1+M2+M3 prompt build only; no browser, no Excel):
python src/main.py --jd-id JD001 --dry-run

# 3) End-to-end on one JD (max 50 resumes):
python src/main.py --jd-id JD001

# 4) Refine the boolean with a TA hint, then regenerate (max 2x):
python src/main.py --jd-id JD001 --hint "must have FastAPI on AWS"
python src/main.py --jd-id JD001 --regenerate

# 5) All active JDs:
python src/main.py --all

# 6) Sourcing only, skip the Quality Gate:
python src/main.py --jd-id JD001 --no-score
```

## JD input schema (Excel)

Loader matches headers case-insensitively with an alias map. Required:

| Canonical | Accepted variants |
|---|---|
| `JD_ID` | `JD ID`, `JD`, `ID` |
| `Role` | `Title`, `Position`, `Job Role` |
| `Skills` | `Required Skills`, `Skills Required`, `Tech Stack` |
| `Experience_Min_Years` | `Min Years`, `Min Experience`, `Min Exp` |
| `Experience_Max_Years` | `Max Years`, `Max Experience`, `Max Exp` |
| `Location` | `City`, `Locations` |
| `CTC_Ceiling_Lacs` | `CTC Ceiling`, `CTC`, `Max CTC`, `Salary Cap` |
| `Freshness_Days` | `Freshness`, `Active in days`, `Recency days` |
| `Active` | `Status`, `Enabled` (TRUE / Open / Active) |
| `Notes` | `Comments`, `Remarks` (optional) |

Extra columns ignored. Header auto-detected (any row containing "JD" and
"Role"). Blank rows skipped. `.xlsx` and `.xls` supported.

## Configuration

Secrets in `config/.env` (gitignored). Copy `config/.env.example` for a clean
template. Per-JD rules in `screening_guidelines/<JD_ID>_*.md`.

## Open / Deferred Items

1. **Live Resdex selectors** in `naukri_bot.py:SELECTORS` are best-guess
   placeholders. First real run should be `HEADLESS=false` so the team can
   inspect the DOM and refine the map.
2. **L5 & L6 spec** — weights and reject rules are placeholders pending the
   full video spec.
3. **Google Sheets output** — to be enabled once the TA team shares the
   service-account JSON and target Sheet ID.
