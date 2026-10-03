"""SEC Form 13F data sets and CUSIP resolution for I (spec 2026-10-03-institutional-i-v1).

The data sets are read literally; only rows of tracked CUSIPs are kept, plus
every 13F-HR/13F-HR/A submission (restatements can remove holdings). A CUSIP
found from a company name is accepted only when clearly dominant and is
always flagged; the official SPY CUSIP wins and a disagreement is reported.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import BinaryIO, Iterable, Mapping, Sequence

from database.sponsorship_v1 import Filing13F, Holding13F

CUSIP_RESOLUTION_VERSION = "cusip-resolution-v1"
DATASETS_PAGE_URL = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"
SEC_BASE_URL = "https://www.sec.gov"
FILING_TYPES = frozenset({"13F-HR", "13F-HR/A"})
NAME_MATCH_MIN_ROWS = 50
NAME_MATCH_MIN_SHARE = Decimal("0.9")
_LEGAL_WORDS = frozenset({
    "INC", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LIMITED", "PLC", "NV", "N", "V", "SA", "AG", "HOLDINGS",
    "HLDGS", "HOLDING", "GROUP", "LP", "LLC", "TRUST", "COM", "CL", "CLASS", "A", "B", "C", "SHS", "ORD", "NEW",
    "DEL", "ADR", "SPONSORED", "SPON", "ADS", "REIT", "&", "AND",
})
_STATE_SUFFIX = re.compile(r"[/\\]\s*[A-Z]{1,4}\s*(?:[/\\]|$)")
_WINDOW = re.compile(r"(\d{2}[a-z]{3}\d{4})-(\d{2}[a-z]{3}\d{4})_form13f\.zip$")


def normalize_issuer(name: str) -> str:
    text = _STATE_SUFFIX.sub(" ", (name or "").upper())
    tokens = re.sub(r"[^A-Z0-9& ]", " ", text.replace(".", "")).split()
    if tokens and tokens[0] == "THE":
        tokens = tokens[1:]
    while tokens and tokens[-1] in _LEGAL_WORDS:
        tokens.pop()
    return " ".join(tokens)


def resolve_cusip(company_name: str, official: str | None, name_counts: Mapping[str, Counter]) -> tuple:
    """Return (cusip, status, evidence) per spec §5."""

    counts = name_counts.get(normalize_issuer(company_name), Counter())
    matched, evidence = None, {"normalized_name": normalize_issuer(company_name),
                               "candidates": dict(counts.most_common(3))}
    if counts:
        cusip, top = counts.most_common(1)[0]
        if top >= NAME_MATCH_MIN_ROWS and Decimal(top) / sum(counts.values()) >= NAME_MATCH_MIN_SHARE:
            matched = cusip
    if official:
        official = official.upper()
        if matched is not None and matched != official:
            return official, "CUSIP_SOURCES_DISAGREE", {**evidence, "official": official, "name_match": matched}
        return official, "CUSIP_OFFICIAL", {**evidence, "official": official}
    if matched is not None:
        return matched, "CUSIP_FROM_NAME_MATCH", evidence
    return None, "CUSIP_AMBIGUOUS" if counts else "CUSIP_NOT_FOUND", evidence


def dataset_window(file_name: str) -> tuple[date, date] | None:
    """Filing window of a data set file name such as ``01jun2026-31aug2026_form13f.zip``."""

    match = _WINDOW.search(file_name)
    if not match:
        return None
    return tuple(datetime.strptime(part, "%d%b%Y").date() for part in match.groups())


def _date(text: str) -> date | None:
    return datetime.strptime(text, "%d-%b-%Y").date() if text else None


def _decimal(text: str) -> Decimal:
    try:
        return Decimal(text or "0")
    except InvalidOperation:
        return Decimal(0)


def _member(archive: zipfile.ZipFile, name: str) -> str:
    """The archive member for a table: at the root (recent files) or inside one folder (older files)."""

    matches = [item for item in archive.namelist() if item == name or item.endswith("/" + name)]
    if len(matches) != 1:
        raise ValueError(f"13F data set table {name}: {len(matches)} members")
    return matches[0]


def _tsv(archive: zipfile.ZipFile, name: str) -> Iterable[dict]:
    with archive.open(_member(archive, name)) as handle:
        yield from csv.DictReader(io.TextIOWrapper(handle, "utf-8", errors="replace"), delimiter="\t",
                                  quoting=csv.QUOTE_NONE)


def parse_13f_dataset(source: BinaryIO | str, *, tracked: set[str], wanted_names: set[str]):
    """Return (filings, tracked holdings, {normalized name: Counter(cusip)}) from one data set."""

    with zipfile.ZipFile(source) as archive:
        return _parse_archive(archive, tracked=tracked, wanted_names=wanted_names)


def _parse_archive(archive: zipfile.ZipFile, *, tracked: set[str], wanted_names: set[str]):
    amendments = {row["ACCESSION_NUMBER"]: (row.get("AMENDMENTTYPE") or None) for row in _tsv(archive, "COVERPAGE.tsv")}
    filings = [
        Filing13F(
            accession=row["ACCESSION_NUMBER"], filer_cik=row["CIK"].zfill(10), filing_date=_date(row["FILING_DATE"]),
            submission_type=row["SUBMISSIONTYPE"], period_of_report=_date(row["PERIODOFREPORT"]),
            amendment_type=amendments.get(row["ACCESSION_NUMBER"]),
        )
        for row in _tsv(archive, "SUBMISSION.tsv") if row["SUBMISSIONTYPE"] in FILING_TYPES
    ]
    holdings: list[Holding13F] = []
    names: dict[str, Counter] = defaultdict(Counter)
    for row in _tsv(archive, "INFOTABLE.tsv"):
        cusip = row["CUSIP"].strip().upper()
        put_call = row.get("PUTCALL") or None
        if wanted_names and not put_call and row["SSHPRNAMTTYPE"] == "SH":
            normalized = normalize_issuer(row["NAMEOFISSUER"])
            if normalized in wanted_names:
                names[normalized][cusip] += 1
        if cusip in tracked:
            holdings.append(Holding13F(
                accession=row["ACCESSION_NUMBER"], infotable_sk=row["INFOTABLE_SK"], cusip=cusip,
                name_of_issuer=row["NAMEOFISSUER"], title_of_class=row["TITLEOFCLASS"],
                value=_decimal(row["VALUE"]), shares=_decimal(row["SSHPRNAMT"]),
                shares_type=row["SSHPRNAMTTYPE"], put_call=put_call,
            ))
    return filings, holdings, dict(names)


def list_datasets(*, page_getter=None) -> list[tuple[str, str, tuple[date, date]]]:
    """(file name, absolute URL, filing window) of the data sets in the new window format."""

    if page_getter is None:
        import requests

        from database.providers_v3 import sec_headers

        headers = {**sec_headers(), "Accept": "text/html"}
        html = requests.get(DATASETS_PAGE_URL, headers=headers, timeout=60).text
    else:
        html = page_getter(DATASETS_PAGE_URL)
    found = {}
    for href in re.findall(r'href="([^"]+_form13f\.zip)"', html):
        name = href.rsplit("/", 1)[-1]
        window = dataset_window(name)
        if window:
            found[name] = (name, href if href.startswith("http") else SEC_BASE_URL + href, window)
    return sorted(found.values(), key=lambda item: item[2])


def needed_datasets(datasets, quarter_ends: Sequence[date]) -> list:
    """Data sets whose filing window can hold filings for the oldest quarter onwards."""

    oldest = min(quarter_ends)
    return [item for item in datasets if item[2][1] > oldest]


def _download(url: str) -> tuple[str, int, str]:
    """Stream a data set to a temporary file; returns (path, size, sha256)."""

    import hashlib
    import tempfile

    import requests

    from database.providers_v3 import ProviderError, sec_headers

    digest, size = hashlib.sha256(), 0
    handle = tempfile.NamedTemporaryFile(suffix="_form13f.zip", delete=False)
    try:
        with requests.get(url, headers={**sec_headers(), "Accept": "*/*"}, timeout=600, stream=True) as response:
            response.raise_for_status()
            for chunk in response.iter_content(1 << 20):
                handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)
    except requests.RequestException:
        raise ProviderError("SEC_13F_DOWNLOAD_ERROR") from None
    finally:
        handle.close()
    return handle.name, size, digest.hexdigest()


def ingest_institutional(*, quarters: int = 5, clock=None, connection_factory=None, page_getter=None,
                         downloader=None) -> dict:
    """Resolve CUSIPs from the latest data set, then store filings and tracked holdings of every needed one."""

    import os
    from datetime import timezone

    from database import evidence_v3
    from database.ingest_v3 import _connect
    from database.sponsorship_v1 import complete_quarters
    from database.universe_v1 import UNIVERSE_NAME, yahoo_ticker

    clock = clock or (lambda: datetime.now(timezone.utc))
    downloader = downloader or _download
    now = clock()
    periods = complete_quarters(now.date(), quarters)
    datasets = needed_datasets(list_datasets(page_getter=page_getter), periods)
    summary = {"periods": [item.isoformat() for item in periods], "datasets": [], "cusips": {}}
    if not datasets:
        summary["error"] = "NO_13F_DATASETS"
        return summary

    with _connect(connection_factory) as connection, connection.cursor() as cursor:
        companies = cursor.execute("SELECT id, ticker, company_name FROM public.companies;").fetchall()
        snapshot = cursor.execute(
            """SELECT m.source_ticker, m.identifier FROM public.universe_members AS m
               WHERE m.snapshot_id = (SELECT id FROM public.universe_snapshots WHERE universe = %s
                                      ORDER BY holdings_as_of DESC LIMIT 1);""", (UNIVERSE_NAME,)).fetchall()
    official = {yahoo_ticker(ticker): (identifier or "").upper() for ticker, identifier in snapshot
                if yahoo_ticker(ticker) and re.fullmatch(r"[0-9A-Z]{9}", (identifier or "").upper())}
    wanted = {normalize_issuer(name) for _, _, name in companies if name}

    downloaded = {}
    latest_name, latest_url, _ = datasets[-1]
    downloaded[latest_name] = downloader(latest_url)
    _, _, name_counts = parse_13f_dataset(downloaded[latest_name][0], tracked=set(), wanted_names=wanted)
    tracked: set[str] = set()
    with _connect(connection_factory) as connection, connection.cursor() as cursor:
        for company_id, ticker, name in companies:
            cusip, source, evidence = resolve_cusip(name or "", official.get(ticker), name_counts)
            evidence_v3.insert_company_cusip(cursor, company_id=company_id, cusip=cusip, source=source,
                                             evidence={**evidence, "dataset": latest_name}, observed_at=now)
            summary["cusips"][ticker] = {"cusip": cusip, "source": source}
            if cusip:
                tracked.add(cusip)

    for name, url, _ in datasets:
        path, size, sha = downloaded.get(name) or downloader(url)
        try:
            with _connect(connection_factory) as connection, connection.cursor() as cursor:
                if evidence_v3.dataset_13f_stored(cursor, name, sha):
                    summary["datasets"].append({"file": name, "status": "ALREADY_STORED"})
                    continue
                filings, holdings, _ = parse_13f_dataset(path, tracked=tracked, wanted_names=set())
                dataset_id = evidence_v3.insert_13f_dataset(cursor, file_name=name, sha256=sha, size_bytes=size,
                                                            observed_at=clock())
                new_filings = evidence_v3.insert_13f_filings(cursor, dataset_id=dataset_id, filings=filings)
                new_rows = evidence_v3.insert_13f_holdings(cursor, dataset_id=dataset_id, holdings=holdings)
                summary["datasets"].append({"file": name, "status": "STORED", "filings": new_filings,
                                            "holdings": new_rows})
        except (ValueError, zipfile.BadZipFile) as error:
            # One unreadable data set never stops the others; its quarter stays incomplete.
            summary["datasets"].append({"file": name, "status": "FAILED", "error": str(error)[:200]})
        finally:
            if downloader is _download:
                try:
                    os.remove(path)
                except OSError:
                    pass
    return summary


def main(argv=None):
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Ingest SEC Form 13F data sets for I")
    parser.add_argument("--quarters", type=int, default=5)
    args = parser.parse_args(argv)
    summary = ingest_institutional(quarters=args.quarters)
    print(json.dumps(summary, indent=2, default=str))
    return summary


if __name__ == "__main__":
    main()
