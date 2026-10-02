"""Provider access for fundamentals v3 ingestion (SEC EDGAR and Yahoo).

Network calls are isolated in small functions so that parsing stays pure and
testable offline. Every function raises ``ProviderError`` with a symbolic code
on failure; callers record the failed attempt instead of silently continuing.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable, Mapping

# www.sec.gov rejects agents without a contact address; data.sec.gov accepts
# the default. CANSLIM_SEC_USER_AGENT="Name contact@example.com" (in .env)
# enables automatic ticker-to-CIK lookup.
DEFAULT_SEC_USER_AGENT = "CANSLIMResearch/0.6 educational-research"


def sec_headers() -> dict:
    """Read the agent at request time, after the project's .env is loaded."""

    try:
        from pathlib import Path

        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    except ImportError:
        pass
    return {
        "User-Agent": os.getenv("CANSLIM_SEC_USER_AGENT") or DEFAULT_SEC_USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
        "Accept": "application/json",
    }
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_SUBMISSIONS_PAGE_URL = "https://data.sec.gov/submissions/{name}"
SEC_MIN_INTERVAL_SECONDS = 0.15  # SEC fair-access limit is 10 requests per second.

YAHOO_TIMESERIES_TYPES = ("quarterlyDilutedEPS", "quarterlyTotalRevenue", "quarterlyNetIncome")


class ProviderError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CompanyIdentity:
    ticker: str
    cik: str
    name: str
    exchange: str


@dataclass(frozen=True)
class FilingRecord:
    accession: str
    form: str
    filing_date: date
    acceptance_at: datetime | None
    report_date: date | None


_last_sec_request = 0.0


def _sec_get_json(url: str, getter: Callable | None = None) -> Mapping:
    global _last_sec_request
    if getter is not None:
        return getter(url)
    import requests

    wait = SEC_MIN_INTERVAL_SECONDS - (time.monotonic() - _last_sec_request)
    if wait > 0:
        time.sleep(wait)
    try:
        response = requests.get(url, headers=sec_headers(), timeout=30)
        _last_sec_request = time.monotonic()
        response.raise_for_status()
        return response.json()
    except requests.HTTPError as exc:
        status = getattr(exc.response, "status_code", None)
        codes = {403: "SEC_ACCESS_DENIED", 404: "SEC_NOT_FOUND", 429: "SEC_RATE_LIMITED"}
        raise ProviderError(codes.get(status, "SEC_HTTP_ERROR")) from None
    except (requests.RequestException, ValueError):
        raise ProviderError("SEC_TRANSPORT_ERROR") from None


def parse_company_tickers(payload: Mapping, ticker: str) -> CompanyIdentity:
    fields = payload.get("fields", [])
    try:
        cik_i, name_i, ticker_i, exchange_i = (
            fields.index("cik"), fields.index("name"), fields.index("ticker"), fields.index("exchange"),
        )
    except ValueError:
        raise ProviderError("SEC_TICKERS_SCHEMA_CHANGED") from None
    wanted = ticker.strip().upper()
    matches = [row for row in payload.get("data", []) if str(row[ticker_i]).upper() == wanted]
    if len(matches) != 1:
        raise ProviderError("CIK_NOT_FOUND" if not matches else "CIK_AMBIGUOUS")
    row = matches[0]
    return CompanyIdentity(
        ticker=wanted,
        cik=str(row[cik_i]).zfill(10),
        name=str(row[name_i]),
        exchange=str(row[exchange_i] or "").upper(),
    )


def identity_from_submissions(payload: Mapping, ticker: str, cik: str) -> CompanyIdentity:
    """Verify that ``cik`` currently lists ``ticker`` and read its identity."""

    if str(payload.get("cik", "")).zfill(10) != cik:
        raise ProviderError("SEC_SUBMISSIONS_CIK_MISMATCH")
    tickers = [str(item).upper() for item in payload.get("tickers", [])]
    wanted = ticker.strip().upper()
    if wanted not in tickers:
        raise ProviderError("TICKER_NOT_LISTED_FOR_CIK")
    exchanges = payload.get("exchanges", [])
    index = tickers.index(wanted)
    exchange = str(exchanges[index]).upper() if index < len(exchanges) and exchanges[index] else ""
    return CompanyIdentity(ticker=wanted, cik=cik, name=str(payload.get("name", "")), exchange=exchange)


def resolve_company(
    ticker: str, *, known_cik: str | None = None, getter: Callable | None = None,
) -> CompanyIdentity:
    """Resolve a ticker; a known CIK is verified against SEC submissions."""

    if known_cik is None:
        known_cik = parse_company_tickers(_sec_get_json(SEC_TICKERS_URL, getter), ticker).cik
    known_cik = str(known_cik).zfill(10)
    return identity_from_submissions(_sec_get_json(SEC_SUBMISSIONS_URL.format(cik=known_cik), getter), ticker, known_cik)


def fetch_companyfacts(cik: str, *, getter: Callable | None = None) -> Mapping:
    payload = _sec_get_json(SEC_COMPANYFACTS_URL.format(cik=cik), getter)
    if str(payload.get("cik", "")).zfill(10) != cik:
        raise ProviderError("SEC_COMPANYFACTS_CIK_MISMATCH")
    return payload


def _parse_acceptance(text: str | None) -> datetime | None:
    # Verified 2026-10-02 against 1,000 NVDA filings: the "Z" suffix is real UTC
    # (10-Qs at 20:36Z = 16:36 New York; pre-06:00Z acceptances carry the
    # previous filing date).
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def parse_submission_arrays(arrays: Mapping) -> list[FilingRecord]:
    accessions = arrays.get("accessionNumber", [])
    columns = {
        name: arrays.get(name, [None] * len(accessions))
        for name in ("filingDate", "reportDate", "acceptanceDateTime", "form")
    }
    if any(len(values) != len(accessions) for values in columns.values()):
        raise ProviderError("SEC_SUBMISSIONS_SHAPE_INVALID")
    records = []
    for index, accession in enumerate(accessions):
        report = columns["reportDate"][index]
        records.append(FilingRecord(
            accession=str(accession),
            form=str(columns["form"][index] or ""),
            filing_date=date.fromisoformat(columns["filingDate"][index]),
            acceptance_at=_parse_acceptance(columns["acceptanceDateTime"][index]),
            report_date=date.fromisoformat(report) if report else None,
        ))
    return records


def fetch_filings(cik: str, *, getter: Callable | None = None) -> list[FilingRecord]:
    payload = _sec_get_json(SEC_SUBMISSIONS_URL.format(cik=cik), getter)
    filings = payload.get("filings", {})
    records = parse_submission_arrays(filings.get("recent", {}))
    for page in filings.get("files", []):
        records += parse_submission_arrays(_sec_get_json(SEC_SUBMISSIONS_PAGE_URL.format(name=page["name"]), getter))
    unique = {record.accession: record for record in records}
    if len(unique) != len(records):
        raise ProviderError("SEC_SUBMISSIONS_DUPLICATE_ACCESSION")
    return sorted(unique.values(), key=lambda record: (record.filing_date, record.accession))


def fetch_yahoo_timeseries(ticker: str, *, fetcher: Callable | None = None) -> dict:
    """Return {series_type: pandas.Series}; fail if any requested type fails."""

    if fetcher is None:
        from fundamental_c import _fetch_yahoo_timeseries as fetcher
    series, errors = fetcher(ticker, list(YAHOO_TIMESERIES_TYPES), years=5)
    if errors or set(series) != set(YAHOO_TIMESERIES_TYPES):
        raise ProviderError("YAHOO_TIMESERIES_INCOMPLETE")
    return series


def fetch_yfinance_income(ticker: str, *, stock_factory: Callable | None = None):
    try:
        if stock_factory is None:
            import yfinance as yf

            stock_factory = yf.Ticker
        frame = stock_factory(ticker).quarterly_income_stmt
    except Exception:  # yfinance raises heterogeneous transport/parsing errors.
        raise ProviderError("YFINANCE_ERROR") from None
    if frame is None or frame.empty:
        raise ProviderError("YFINANCE_EMPTY_RESPONSE")
    return frame
