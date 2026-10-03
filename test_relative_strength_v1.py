"""Offline tests for the S&P 500 universe file and L calculations (l-relative-strength-v1)."""

import io
from xml.sax.saxutils import escape
import unittest
import zipfile
from datetime import date, timedelta
from decimal import Decimal

from database.relative_strength_v1 import (
    RELATIVE_STRENGTH_VERSION,
    UniverseMember,
    compute_group_strength,
    compute_rs_line,
    rs_rating,
    sic_group,
    weighted_score,
)
from database.universe_v1 import UniverseFileError, parse_spy_holdings, yahoo_ticker
from test_new_highs_v1 import series


def xlsx(rows, strings_extra=()):
    """A minimal xlsx with inline shared strings, as SSGA publishes it."""

    strings, index = [], {}

    def cell(ref, value):
        if isinstance(value, (int, float)):
            return f'<c r="{ref}" t="n"><v>{value}</v></c>'
        if value not in index:
            index[value] = len(strings)
            strings.append(value)
        return f'<c r="{ref}" t="s"><v>{index[value]}</v></c>'

    body = []
    for number, values in rows:
        cells = "".join(cell(f"{chr(65 + col)}{number}", value) for col, value in enumerate(values) if value is not None)
        body.append(f'<row r="{number}">{cells}</row>')
    shared = "".join(f"<si><t>{escape(text)}</t></si>" for text in strings)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{shared}</sst>')
        archive.writestr("xl/worksheets/sheet1.xml",
                         '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                         + "".join(body) + "</sheetData></worksheet>")
    return buffer.getvalue()


HEADER = ["Name", "Ticker", "Identifier", "SEDOL", "Weight", "Sector", "Shares Held", "Local Currency"]


def spy_file(members, as_of="As of 01-Oct-2026"):
    rows = [(1, ["Fund Name:", "SPDR S&P 500 ETF Trust"]), (2, ["Ticker Symbol:", "SPY"]), (3, ["Holdings:", as_of]),
            (5, HEADER)]
    rows += [(6 + offset, values) for offset, values in enumerate(members)]
    return xlsx(rows)


class UniverseFileTests(unittest.TestCase):
    def test_members_and_date(self):
        payload = spy_file([
            ["NVIDIA CORP", "NVDA", "67066G104", "2379504", 8.454497, "Information Technology", 296384992, "USD"],
            ["BERKSHIRE HATHAWAY INC CL B", "BRK.B", "084670702", "2073390", 1.6, "Financials", 12000000, "USD"],
            ["US DOLLAR", "-", "CASH_USD", None, 0.1, "-", 5000, "USD"],
        ])
        holdings_as_of, members = parse_spy_holdings(payload)
        self.assertEqual(holdings_as_of, date(2026, 10, 1))
        self.assertEqual([member.source_ticker for member in members], ["NVDA", "BRK.B", "-"])
        self.assertEqual(members[0].weight, Decimal("8.454497"))
        self.assertEqual(members[0].shares_held, Decimal("296384992"))
        self.assertEqual(members[1].sector, "Financials")
        self.assertEqual(members[2].sedol, None)

    def test_missing_header_or_date_fails(self):
        with self.assertRaises(UniverseFileError):
            parse_spy_holdings(xlsx([(1, ["Holdings:", "As of 01-Oct-2026"]), (5, ["Name", "Ticker"])]))
        with self.assertRaises(UniverseFileError):
            parse_spy_holdings(spy_file([], as_of="unknown"))

    def test_yahoo_ticker(self):
        self.assertEqual(yahoo_ticker("BRK.B"), "BRK-B")
        self.assertEqual(yahoo_ticker("NVDA"), "NVDA")
        self.assertIsNone(yahoo_ticker("-"))
        self.assertIsNone(yahoo_ticker("CASH_USD"))


def trend(start, daily, count=260, end=date(2026, 10, 2)):
    return series([start * (1 + daily) ** day for day in range(count)], end=end)


class WeightedScoreTests(unittest.TestCase):
    def test_version(self):
        self.assertEqual(RELATIVE_STRENGTH_VERSION, "l-relative-strength-v1")

    def test_ibd_weighting(self):
        closes = [100] * 7 + [50] * 63 + [60] * 63 + [75] * 63 + [80] * 63 + [120]
        score = weighted_score(series(closes), date(2026, 10, 2))
        # C=120; C63=80, C126=75, C189=60, C252=50.
        expected = Decimal("0.4") * 120 / 80 + Decimal("0.2") * 120 / 75 + Decimal("0.2") * 120 / 60 + Decimal("0.2") * 120 / 50
        self.assertEqual(score.status, "OK")
        self.assertEqual(score.value, expected)

    def test_needs_253_sessions_and_fresh_prices(self):
        self.assertEqual(weighted_score(series([1] * 252), date(2026, 10, 2)).status, "INSUFFICIENT_HISTORY")
        self.assertEqual(weighted_score(series([1] * 300, end=date(2026, 9, 18)), date(2026, 10, 2)).status,
                         "STALE_PRICES")


class RatingTests(unittest.TestCase):
    def test_percentile_rating(self):
        universe = [Decimal(value) for value in range(1, 501)]
        self.assertEqual(rs_rating(Decimal("500.5"), universe), 99)
        self.assertEqual(rs_rating(Decimal("0"), universe), 1)
        self.assertEqual(rs_rating(Decimal("250.5"), universe), 50)

    def test_ties_count_half(self):
        self.assertEqual(rs_rating(Decimal(1), [Decimal(1)] * 400), 50)

    def test_member_is_excluded_from_its_own_distribution(self):
        universe = [Decimal(value) for value in range(1, 401)]
        self.assertEqual(rs_rating(Decimal(400), universe, exclude_self=True), 99)


class GroupTests(unittest.TestCase):
    def members(self):
        rows = []
        for index in range(10):
            rows.append(UniverseMember(f"S{index}", "3571", Decimal(index + 10)))   # strong group 35
        for index in range(10):
            rows.append(UniverseMember(f"W{index}", "2834", Decimal(index)))        # weak group 28
        rows.append(UniverseMember("TINY", "1000", Decimal(50)))                    # single-member group
        return rows

    def test_sic_group(self):
        self.assertEqual(sic_group("3571"), "35")
        self.assertIsNone(sic_group(None))
        self.assertIsNone(sic_group(""))

    def test_group_rank_and_rank_in_group(self):
        result = compute_group_strength("3674", Decimal(19), self.members(), subject_ticker="X")
        self.assertEqual(result.group_key, "36")
        self.assertEqual(result.status, "GROUP_NOT_RANKED")  # no members in 36
        result = compute_group_strength("3571", Decimal(19), self.members(), subject_ticker="S9")
        self.assertEqual((result.group_key, result.group_size), ("35", 10))
        self.assertEqual(result.group_rank_pct, Decimal(100))
        self.assertEqual(result.rank_in_group_pct, Decimal(100))
        weak = compute_group_strength("2834", Decimal(0), self.members(), subject_ticker="W0")
        self.assertEqual(weak.group_rank_pct, Decimal(0))
        self.assertEqual(weak.rank_in_group_pct, Decimal(0))

    def test_small_groups_are_not_ranked(self):
        result = compute_group_strength("1000", Decimal(50), self.members(), subject_ticker="TINY")
        self.assertEqual(result.status, "GROUP_NOT_RANKED")
        self.assertIsNone(result.group_rank_pct)

    def test_missing_sic(self):
        self.assertEqual(compute_group_strength(None, Decimal(1), self.members(), subject_ticker="X").status, "NO_SIC")


class RsLineTests(unittest.TestCase):
    def test_line_at_new_high(self):
        stock = trend(100, 0.002)
        market = trend(100, 0.001)
        result = compute_rs_line(stock, market, date(2026, 10, 2))
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.pct_below_high_52w, Decimal(0))
        self.assertTrue(result.new_high_recent)

    def test_line_below_high(self):
        stock = series([100] * 230 + [80] * 30)
        market = series([100] * 260)
        result = compute_rs_line(stock, market, date(2026, 10, 2))
        self.assertEqual(result.pct_below_high_52w, Decimal(20))
        self.assertFalse(result.new_high_recent)

    def test_only_common_sessions(self):
        stock = trend(100, 0.001)
        market = trend(100, 0.001)[:-10]
        self.assertEqual(compute_rs_line(stock, market, date(2026, 10, 2)).sessions, 250)

    def test_insufficient(self):
        result = compute_rs_line(trend(1, 0, count=100), trend(1, 0, count=100), date(2026, 10, 2))
        self.assertEqual(result.status, "INSUFFICIENT_HISTORY")


class UniverseIngestionTests(unittest.TestCase):
    def test_orchestration(self):
        from datetime import datetime, timezone
        from unittest import mock

        from database import evidence_v3, ingest_v3, universe_v1
        from database.providers_v3 import CompanyIdentity

        payload = spy_file([
            ["NVIDIA CORP", "NVDA", "67066G104", "2379504", 8.4, "Information Technology", 100, "USD"],
            ["BERKSHIRE HATHAWAY INC CL B", "BRK.B", "084670702", "2073390", 1.6, "Financials", 10, "USD"],
            ["UNKNOWN CO", "ZZZZ", "000000000", "0", 0.1, "Financials", 1, "USD"],
            ["ALPHABET INC CL C", "GOOG", "02079K107", "0", 1.0, "Communication", 1, "USD"],
            ["ALPHABET INC CL A", "GOOGL", "02079K305", "0", 1.2, "Communication", 1, "USD"],
            ["US DOLLAR", "-", "CASH_USD", None, 0.1, "-", 5000, "USD"],
        ])
        tickers = {"fields": ["cik", "name", "ticker", "exchange"],
                   "data": [[1045810, "NVIDIA", "NVDA", "Nasdaq"], [1067983, "BERKSHIRE", "BRK-B", "NYSE"],
                            [884394, "SPDR S&P 500 ETF TRUST", "SPY", "NYSE"],
                            [1652044, "Alphabet", "GOOG", "Nasdaq"], [1652044, "Alphabet", "GOOGL", "Nasdaq"]]}
        stored_tickers = {}

        def ticker_for_cik(cik, **kwargs):
            return stored_tickers.get(cik)

        def bars(company_id, ticker, *args):
            bars_for.append(ticker)
            stored_tickers.setdefault("0001652044" if ticker.startswith("GOOG") else ticker, ticker)
            return {"status": "SUCCESS"}

        def resolve(ticker, known_cik=None, getter=None):
            return CompanyIdentity(ticker, known_cik or "0000884394", ticker, "NYSE", "3674", "Semis")

        def run_operation(company_id, provider, operation, contract, fetch, persist, *, clock, connection_factory):
            now = clock()
            return {"status": "SUCCESS", **persist(mock.MagicMock(), fetch(), now, now)}

        ids = iter(range(1, 100))
        bars_for = []
        with (
            mock.patch("database.providers_v3.resolve_company", side_effect=resolve),
            mock.patch("database.providers_v3.fetch_sec_tickers", return_value=tickers),
            mock.patch.object(ingest_v3, "ensure_company", side_effect=lambda identity, **kw: next(ids)),
            mock.patch.object(ingest_v3, "_run_operation", side_effect=run_operation),
            mock.patch.object(ingest_v3, "ingest_profile", return_value={"status": "SUCCESS"}),
            mock.patch.object(ingest_v3, "_ingest_daily_bars", side_effect=bars),
            mock.patch.object(evidence_v3, "record_run", return_value=1),
            mock.patch.object(evidence_v3, "load_ticker_for_cik", side_effect=ticker_for_cik),
            mock.patch.object(evidence_v3, "insert_universe_snapshot", return_value=10),
        ):
            summary = universe_v1.ingest_universe(clock=lambda: datetime(2026, 10, 3, tzinfo=timezone.utc),
                                                  ssga_getter=lambda url: payload)
        self.assertEqual(summary["snapshot"]["holdings_as_of"], "2026-10-01")
        self.assertEqual(bars_for, ["NVDA", "BRK-B", "GOOG", "SPY"])
        self.assertEqual(summary["share_classes"], [{"ticker": "GOOGL", "error": "SHARE_CLASS_OF:GOOG"}])
        self.assertEqual(summary["unresolved"], [{"ticker": "ZZZZ", "error": "CIK_NOT_FOUND"}])
        self.assertEqual(summary["skipped_lines"], 1)


if __name__ == "__main__":
    unittest.main()
