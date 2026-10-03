"""Benchmarks for M (spec 2026-10-03-market-direction-m-v1 §2).

SEC submissions list no ticker for the Invesco QQQ Trust, so benchmarks are an
explicit registry verified against SEC by CIK and name instead of the ticker
lookup used for companies.

Usage::

    python -m database.market_v1
"""

from __future__ import annotations

from datetime import datetime, timezone

from database.providers_v3 import CompanyIdentity, ProviderError

BENCHMARKS = {
    "SPY": ("0000884394", "SPDR S&P 500 ETF TRUST", "NYSE ARCA"),
    "QQQ": ("0001067839", "INVESCO QQQ TRUST, SERIES 1", "NASDAQ"),
}


def benchmark_identity(ticker: str, *, getter=None) -> CompanyIdentity:
    """Identity of a registered benchmark, its CIK and name checked against SEC submissions."""

    from database import providers_v3

    cik, name, exchange = BENCHMARKS[ticker]
    payload = providers_v3._sec_get_json(providers_v3.SEC_SUBMISSIONS_URL.format(cik=cik), getter)
    if str(payload.get("cik", "")).zfill(10) != cik:
        raise ProviderError("SEC_SUBMISSIONS_CIK_MISMATCH")
    if str(payload.get("name", "")).upper() != name:
        raise ProviderError("BENCHMARK_NAME_MISMATCH")
    return CompanyIdentity(ticker=ticker, cik=cik, name=name, exchange=exchange,
                           sic=str(payload["sic"]) if payload.get("sic") else None,
                           sic_description=payload.get("sicDescription") or None)


def ingest_benchmarks(*, clock=None, connection_factory=None, sec_getter=None, stock_factory=None) -> dict:
    from database.ingest_v3 import _ingest_daily_bars, ensure_company

    clock = clock or (lambda: datetime.now(timezone.utc))
    result = {}
    for ticker in BENCHMARKS:
        try:
            identity = benchmark_identity(ticker, getter=sec_getter)
        except ProviderError as error:
            result[ticker] = {"status": "FAILED", "error_code": error.code}
            continue
        company_id = ensure_company(identity, connection_factory=connection_factory)
        result[ticker] = _ingest_daily_bars(company_id, ticker, clock, connection_factory, stock_factory)
    return result


def main(argv=None):
    import json

    summary = ingest_benchmarks()
    print(json.dumps(summary, indent=2, default=str))
    return summary


if __name__ == "__main__":
    main()
