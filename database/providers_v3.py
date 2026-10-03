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
    sic: str | None = None
    sic_description: str | None = None


@dataclass(frozen=True)
class FilingRecord:
    accession: str
    form: str
    filing_date: date
    acceptance_at: datetime | None
    report_date: date | None
    items: tuple[str, ...] = ()


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
    return CompanyIdentity(
        ticker=wanted, cik=cik, name=str(payload.get("name", "")), exchange=exchange,
        sic=str(payload["sic"]) if payload.get("sic") else None,
        sic_description=payload.get("sicDescription") or None,
    )


def fetch_sec_tickers(*, getter: Callable | None = None) -> Mapping:
    return _sec_get_json(SEC_TICKERS_URL, getter)


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
    items = arrays.get("items", [""] * len(accessions))
    if any(len(values) != len(accessions) for values in (*columns.values(), items)):
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
            items=tuple(item.strip() for item in (items[index] or "").split(",") if item.strip()),
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


SEC_ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}"


def _sec_get_bytes(url: str, getter: Callable | None = None) -> bytes:
    global _last_sec_request
    if getter is not None:
        return getter(url)
    import requests

    wait = SEC_MIN_INTERVAL_SECONDS - (time.monotonic() - _last_sec_request)
    if wait > 0:
        time.sleep(wait)
    try:
        response = requests.get(url, headers=sec_headers(), timeout=60)
        _last_sec_request = time.monotonic()
        response.raise_for_status()
        return response.content
    except requests.HTTPError as exc:
        status = getattr(exc.response, "status_code", None)
        codes = {403: "SEC_ACCESS_DENIED", 404: "SEC_NOT_FOUND", 429: "SEC_RATE_LIMITED"}
        raise ProviderError(codes.get(status, "SEC_HTTP_ERROR")) from None
    except requests.RequestException:
        raise ProviderError("SEC_TRANSPORT_ERROR") from None


def fetch_filing_instance(cik: str, accession: str, *, getter: Callable | None = None,
                          bytes_getter: Callable | None = None) -> bytes:
    """Download the XBRL instance of one filing from EDGAR archives."""

    from database.sec_xbrl_instance import choose_instance_file

    base = SEC_ARCHIVES_URL.format(cik=int(cik), folder=accession.replace("-", ""))
    listing = _sec_get_json(f"{base}/index.json", getter)
    names = [item["name"] for item in listing.get("directory", {}).get("item", [])]
    instance = choose_instance_file(names)
    if instance is None:
        raise ProviderError("SEC_INSTANCE_NOT_FOUND")
    return _sec_get_bytes(f"{base}/{instance}", bytes_getter)


def fetch_filing_filers(cik: str, accession: str, *, bytes_getter: Callable | None = None) -> list[tuple[str, str]]:
    """Every registrant listed in the EDGAR header of one filing: (cik, name)."""

    from database.registrant_v3 import parse_filers_header

    base = SEC_ARCHIVES_URL.format(cik=int(cik), folder=accession.replace("-", ""))
    text = _sec_get_bytes(f"{base}/{accession}-index-headers.html", bytes_getter).decode("utf-8", "replace")
    filers = parse_filers_header(text)
    if not filers:
        raise ProviderError("SEC_HEADER_WITHOUT_FILERS")
    return filers


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


YAHOO_PRICE_FIELDS = ("Open", "High", "Low", "Close", "Adj Close", "Volume")


def fetch_yahoo_daily_bars(ticker: str, *, start: date, stock_factory: Callable | None = None) -> dict:
    """Literal daily bars since ``start`` as ``{"rows": [(date, values)], "currency", "exchange_timezone"}``.

    ``auto_adjust=False``: Close is not dividend-adjusted. Yahoo still returns
    every bar on the split basis of the request date (spec 2026-10-03 §2).
    """

    try:
        if stock_factory is None:
            import yfinance as yf

            stock_factory = yf.Ticker
        stock = stock_factory(ticker)
        frame = stock.history(start=start.isoformat(), auto_adjust=False, actions=False)
        metadata = stock.history_metadata or {}
    except Exception:  # yfinance raises heterogeneous transport/parsing errors.
        raise ProviderError("YFINANCE_ERROR") from None
    if frame is None or frame.empty:
        raise ProviderError("YFINANCE_EMPTY_RESPONSE")
    if not set(YAHOO_PRICE_FIELDS) <= set(frame.columns):
        raise ProviderError("YAHOO_PRICE_COLUMNS_MISSING")
    currency, timezone_name = metadata.get("currency"), metadata.get("exchangeTimezoneName")
    if not currency or not timezone_name:
        raise ProviderError("YAHOO_PRICE_METADATA_MISSING")
    rows = [
        (stamp.date(), {name: values[name] for name in YAHOO_PRICE_FIELDS})
        for stamp, values in frame.iterrows()
    ]
    return {"rows": rows, "currency": currency, "exchange_timezone": timezone_name}


def fetch_spy_holdings(*, getter: Callable | None = None) -> bytes:
    """The official daily SPY holdings xlsx from State Street (spec L §2)."""

    from database.universe_v1 import UNIVERSE_SOURCE_URL

    if getter is not None:
        return getter(UNIVERSE_SOURCE_URL)
    import requests

    try:
        response = requests.get(UNIVERSE_SOURCE_URL, headers={"User-Agent": "Mozilla/5.0 (CAN SLIM Plus research)"},
                                timeout=60)
        response.raise_for_status()
    except requests.HTTPError:
        raise ProviderError("SSGA_HTTP_ERROR") from None
    except requests.RequestException:
        raise ProviderError("SSGA_TRANSPORT_ERROR") from None
    if not response.content.startswith(b"PK"):
        raise ProviderError("SSGA_NOT_A_SPREADSHEET")
    return response.content
