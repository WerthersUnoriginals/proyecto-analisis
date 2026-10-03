# I — Institutional Sponsorship Design (v1)

**Status:** Approved by the human on 2026-10-03, including the I Score v1
weights and the classic rule with a 1% tolerance (§8). Implemented; live run
in `docs/audits/2026-10-03-i-live-26-companies.md`.

**Date:** 2026-10-03

**Builds on:** the evidence model of fundamentals v3, the S&P 500 universe of
L (SPY holdings with official CUSIPs), and the share supply of S.

## 1. Human decisions (2026-10-03)

- **H1 — Scope.** Scored: number of institutional holders and its trend, and
  institutional ownership as a share of the company's shares. Fund quality
  (top-performing managers) is deferred.
- **H2 — CUSIP.** S&P 500 members use the official CUSIP of the SPY holdings
  file. Other companies use a CUSIP found from their SEC name in the 13F
  issuer names, accepted only when clearly dominant and always flagged
  `CUSIP_FROM_NAME_MATCH`; ambiguity leaves I unavailable (review).

## 2. Verified source (2026-10-03)

SEC "Form 13F data sets": one zip per three-month filing window
(`01jun2026-31aug2026_form13f.zip`, 100 MB, 9 s to download). Tables:
`SUBMISSION` (accession, filing date, type, filer CIK, period),
`COVERPAGE` (amendment flag and type), `INFOTABLE` (3.8 M rows: CUSIP, issuer
name, title of class, value, shares, share type, put/call). Q2 2026: 9,501
13F-HR, 399 13F-HR/A (262 restatements, 144 new holdings); AAPL held by
6,134 distinct filers, NVDA 5,928, RDDT 901.

Name matching probe against the official CUSIPs of the S&P 500: 474 correct,
1 disagreement (OKE: SPY file 30609A109, 13F filers 682680103), 26 not
matched (several share classes, renamed issuers). Hence the review rules in
§5.

## 3. Storage (migration `2026-10-03_institutional_i_v1.sql`)

- `sec_13f_datasets(id, file_name, sha256, size_bytes, observed_at)`: one row
  per distinct downloaded file.
- `sec_13f_filings(dataset_id, accession, filer_cik, filing_date,
  submission_type, period_of_report, amendment_type)`: every 13F-HR and
  13F-HR/A submission (needed to apply restatements, which can drop a
  holding).
- `sec_13f_holdings(dataset_id, accession, infotable_sk, cusip,
  name_of_issuer, title_of_class, value, shares, shares_type, put_call)`:
  literal rows for tracked CUSIPs (S&P 500 members and the companies in
  `companies`).
- `company_cusips(company_id, cusip, source, evidence, observed_at)`: the
  CUSIP used per company and how it was obtained.
- All append-only; an accession's rows are stored once (first observation).

## 4. Ingestion `python -m database.institutional_v1 [--quarters 5]`

Downloads the data sets whose filing windows cover the last N complete
quarters (and later windows, for late filers), skips files already stored
(same sha256), resolves CUSIPs, and stores filings and tracked rows in one
transaction per data set.

## 5. CUSIP resolution `cusip-resolution-v1`

- Issuer names are normalized (upper case, punctuation and state suffixes such
  as `/DE/` removed, trailing legal words such as INC, CORP, CL A removed).
- Name match: among 13F rows (shares, not options) whose normalized issuer
  equals the company's normalized SEC name, the top CUSIP must have at least
  50 rows and 90% of them.
- S&P 500 members: the SPY file CUSIP; if a name match exists and disagrees,
  `CUSIP_SOURCES_DISAGREE` → review.

## 6. Calculations `i-institutional-v1`

- **Complete quarters only:** a quarter counts once its 13F deadline (quarter
  end + 45 days) is on or before `as_of`; filings with `filing_date ≤ as_of`
  and data sets observed by `as_of`.
- **Effective holdings per filer and quarter:** the latest of the original
  13F-HR and its 13F-HR/A restatements, plus every later "new holdings"
  amendment. A restatement without the CUSIP removes the holding.
- **Holders:** distinct filer CIKs with a share position (`SH`, no put/call)
  in the CUSIP. `holders_latest`, `holders_change_qoq_pct` (latest vs
  previous quarter), `holders_change_yoy_pct` (latest vs four quarters
  before), `quarters_increasing` (consecutive quarter-on-quarter increases
  ending at the latest, 0–4).
- **Ownership:** sum of those shares, converted to the `as_of` split basis
  from the quarter end, ÷ the latest weighted shares of S
  (`institutional_ownership_pct`, labelled approximate). Above 100% is
  possible (several managers report the same shares) and flagged
  `OWNERSHIP_ABOVE_100_PCT`.

## 7. Integrity and contract `i-input-contract-v1`

Foreign filer, CUSIP missing/ambiguous/disagreeing, fewer than five complete
quarters, or split status needing review → `REVIEW_REQUIRED`; no share
count for the ownership → `VERIFIED_WITH_PARTIAL_CORE_DATA`. Fields:
`cusip`, `cusip_source`, `latest_quarter`, `holders_latest`,
`holders_change_qoq_pct`, `holders_change_yoy_pct`, `quarters_increasing`,
`holders_by_quarter`, `institutional_shares`, `institutional_ownership_pct`,
`i_data_integrity`, `split_integrity_status`.

## 8. I Score v1 (approved 2026-10-03)

| Component | Max | Curve (input → points) |
|---|---|---|
| Holders change, quarter on quarter (%) | 30 | −5→0, 0→12, 2→20, 5→26, 10→30 |
| Holders change, year on year (%) | 25 | −10→0, 0→10, 5→17, 10→22, 20→25 |
| Consecutive quarters of increase | 15 | 0→0, 1→5, 2→9, 3→12, 4→15 |
| Institutional ownership (%) | 30 | 0→0, 20→10, 40→22, 50→30, 85→30, 95→24, 110→12 |

**Classic O'Neil result:** `INSUFFICIENT_DATA` without both quarter-on-quarter
and year-on-year changes; `FAIL_DECLINING_SPONSORSHIP` when holders fell
more than 1% quarter on quarter (smaller moves are noise: AAPL −0.02%); `PASS`
otherwise. Status and usability as in A, N, S, L.

## 9. Implementation notes from real data (2026-10-03)

- **Two archive layouts.** Data sets up to 2025 keep their tables inside a
  folder (`01JUN2025-31AUG2025_form13f/COVERPAGE.tsv`); recent ones at the
  root. Tables are found by name at either depth; an unreadable data set is
  recorded as failed and the others continue.
- **Holder discontinuity.** A CUSIP can change with a reorganization (XOM,
  July 2026) or a reverse split (LCID, September 2025). A count that doubles
  or halves between consecutive quarters, with at least 20 holders, is
  `HOLDERS_DISCONTINUITY` → review. Linking old and new CUSIPs is deferred.
