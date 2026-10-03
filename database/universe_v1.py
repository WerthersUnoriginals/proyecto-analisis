"""S&P 500 universe for L from the official SPY holdings file (spec 2026-10-03-leader-laggard-l-v1).

The SSGA file is an xlsx, read here with the standard library (an xlsx is a
zip of XML) so that no spreadsheet dependency is needed. Members are stored
literally; mapping a source ticker to a SEC/Yahoo ticker is explicit and an
unresolvable member is reported, never guessed.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree

UNIVERSE_NAME = "SP500_SPY"
UNIVERSE_SOURCE_URL = (
    "https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx"
)
HEADER = ("Name", "Ticker", "Identifier", "SEDOL", "Weight", "Sector", "Shares Held", "Local Currency")
_NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_TICKER = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")


class UniverseFileError(ValueError):
    """The holdings file does not have the expected shape."""


@dataclass(frozen=True)
class HoldingRow:
    position: int
    source_ticker: str
    name: str
    identifier: str | None
    sedol: str | None
    weight: Decimal | None
    sector: str | None
    shares_held: Decimal | None
    currency: str | None


def _column(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref).group()
    index = 0
    for letter in letters:
        index = index * 26 + ord(letter) - 64
    return index - 1


def _rows(payload: bytes) -> list[list[str | None]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
        strings = [
            "".join(node.text or "" for node in item.iter(f"{{{_NS['s']}}}t"))
            for item in ElementTree.fromstring(archive.read("xl/sharedStrings.xml")).findall("s:si", _NS)
        ]
        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as error:
        raise UniverseFileError(f"unreadable holdings file: {error}") from None
    rows = []
    for row in sheet.iter(f"{{{_NS['s']}}}row"):
        values: dict[int, str] = {}
        for cell in row.findall("s:c", _NS):
            raw = cell.find("s:v", _NS)
            if raw is None or raw.text is None:
                continue
            value = strings[int(raw.text)] if cell.get("t") == "s" else raw.text
            values[_column(cell.get("r"))] = value.strip()
        width = max(values) + 1 if values else 0
        rows.append([values.get(index) for index in range(width)])
    return rows


def _decimal(value: str | None) -> Decimal | None:
    if value in (None, "", "-"):
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def parse_spy_holdings(payload: bytes) -> tuple[date, list[HoldingRow]]:
    """Return the holdings date and every line below the header, literally."""

    rows = _rows(payload)
    holdings_as_of = None
    for row in rows:
        for value in row:
            match = re.match(r"As of (\d{2}-[A-Za-z]{3}-\d{4})$", value or "")
            if match:
                holdings_as_of = datetime.strptime(match.group(1), "%d-%b-%Y").date()
    header_index = next((index for index, row in enumerate(rows) if tuple(row[:len(HEADER)]) == HEADER), None)
    if header_index is None:
        raise UniverseFileError("holdings header not found")
    if holdings_as_of is None:
        raise UniverseFileError("holdings date not found")
    members = []
    for row in rows[header_index + 1:]:
        row = row + [None] * (len(HEADER) - len(row))
        name, ticker = row[0], row[1]
        if not name or not ticker:
            continue
        members.append(HoldingRow(
            position=len(members) + 1, source_ticker=ticker, name=name, identifier=row[2], sedol=row[3],
            weight=_decimal(row[4]), sector=row[5], shares_held=_decimal(row[6]), currency=row[7],
        ))
    return holdings_as_of, members


def yahoo_ticker(source_ticker: str) -> str | None:
    """SEC/Yahoo ticker of an equity line (``BRK.B`` -> ``BRK-B``); None for cash or futures lines."""

    if not _TICKER.match(source_ticker or ""):
        return None
    return source_ticker.replace(".", "-")


UNIVERSE_CONTRACT = "ssga-spy-holdings-v1"


def ingest_universe(*, clock=None, connection_factory=None, sec_getter=None, ssga_getter=None,
                    stock_factory=None, limit: int | None = None) -> dict:
    """Store today's SPY holdings, then profiles and daily bars of every equity member and of SPY."""

    from datetime import timezone

    from database import evidence_v3, providers_v3
    from database.ingest_v3 import _ingest_daily_bars, _run_operation, ensure_company, ingest_profile
    from database.providers_v3 import ProviderError

    clock = clock or (lambda: datetime.now(timezone.utc))
    spy = providers_v3.resolve_company("SPY", getter=sec_getter)
    spy_id = ensure_company(spy, connection_factory=connection_factory)
    parsed: dict = {}

    def persist(cursor, payload, started, completed):
        holdings_as_of, members = parse_spy_holdings(payload)
        parsed.update(holdings_as_of=holdings_as_of, members=members)
        run_id = evidence_v3.record_run(
            cursor, company_id=spy_id, provider="SSGA", operation="ssga.spy_holdings",
            contract_version=UNIVERSE_CONTRACT, started_at=started, completed_at=completed,
            status="SUCCESS", error_code=None, item_count=len(members),
            metadata={"holdings_as_of": holdings_as_of.isoformat()},
        )
        snapshot = evidence_v3.insert_universe_snapshot(
            cursor, company_id=spy_id, universe=UNIVERSE_NAME, holdings_as_of=holdings_as_of, members=members,
            observed_at=completed, run_id=run_id,
        )
        return {"holdings_as_of": holdings_as_of.isoformat(), "members": len(members), "new_snapshot": snapshot is not None}

    snapshot_step = _run_operation(
        spy_id, "SSGA", "ssga.spy_holdings", UNIVERSE_CONTRACT,
        lambda: providers_v3.fetch_spy_holdings(getter=ssga_getter), persist,
        clock=clock, connection_factory=connection_factory,
    )
    summary = {"snapshot": snapshot_step, "ingested": 0, "unresolved": [], "skipped_lines": 0, "bars_failed": []}
    if snapshot_step["status"] != "SUCCESS":
        return summary
    tickers_payload = providers_v3.fetch_sec_tickers(getter=sec_getter)
    tickers = []
    for member in parsed["members"]:
        ticker = yahoo_ticker(member.source_ticker)
        if ticker is None:
            summary["skipped_lines"] += 1
        elif ticker not in tickers:
            tickers.append(ticker)
    summary["share_classes"] = []
    for ticker in (tickers[:limit] if limit else tickers) + ["SPY"]:
        try:
            cik = providers_v3.parse_company_tickers(tickers_payload, ticker).cik
            # One member per issuer: a second share class (GOOG next to GOOGL)
            # would count the same company twice in the distribution.
            existing = evidence_v3.load_ticker_for_cik(cik, connection_factory=connection_factory)
            if existing is not None and existing != ticker:
                summary["share_classes"].append({"ticker": ticker, "error": f"SHARE_CLASS_OF:{existing}"})
                continue
            identity = providers_v3.resolve_company(ticker, known_cik=cik, getter=sec_getter)
            company_id = ensure_company(identity, connection_factory=connection_factory)
            ingest_profile(company_id, identity, clock, connection_factory)
            bars = _ingest_daily_bars(company_id, ticker, clock, connection_factory, stock_factory)
        except ProviderError as error:
            summary["unresolved"].append({"ticker": ticker, "error": error.code})
            continue
        except Exception as error:  # one member never stops the universe
            summary["unresolved"].append({"ticker": ticker, "error": type(error).__name__})
            continue
        if bars["status"] != "SUCCESS":
            summary["bars_failed"].append({"ticker": ticker, "error": bars.get("error_code")})
        summary["ingested"] += 1
    return summary


def main(argv=None):
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Ingest the S&P 500 universe (SPY holdings) for L")
    parser.add_argument("--limit", type=int, default=None, help="only the first N members (testing)")
    args = parser.parse_args(argv)
    summary = ingest_universe(limit=args.limit)
    print(json.dumps(summary, indent=2, default=str))
    return summary


if __name__ == "__main__":
    main()
