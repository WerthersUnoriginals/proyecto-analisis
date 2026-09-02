"""
CAN SLIM+ — Módulo C: Current Earnings v2.6 experimental

Mejoras v2.5.3:
- SEC Company Facts como histórico principal y Yahoo/yfinance como complemento.
- YoY por trimestre comparable real, no por posición i-4.
- Resolver ticker→CIK con caché local y mapping externo cargado una sola vez.
- Detección de splits con yfinance.
- Verificación de EPS SEC mediante Net Income / weighted-average shares.
- Clasificación explícita de la calidad de shares: diluted exactas, basic+diluted,
  basic fallback o no disponibles.
- Un fallback BASIC nunca certifica por sí solo que un split esté ajustado.
- Control explícito de discrepancias SEC↔Yahoo por métrica.
- Estado agregado DATA_INTEGRITY para desacoplar calidad del dato y score CAN SLIM.

Integra C_CLASSIC y C Score v1 experimental; no es todavía un score definitivo.

Requiere:
    pip install yfinance pandas requests
"""

from __future__ import annotations

import math
import re
import time
from typing import Optional

import pandas as pd
import requests
import yfinance as yf

from c_score_v1 import build_c_score


CIK_CACHE = {
    "NVDA": "0001045810",
    "MSFT": "0000789019",
    "COST": "0000909832",
    "XOM": "0000034088",
    "AMZN": "0001018724",
}
CIK_MAPPER_CACHE: Optional[dict] = None

SEC_HEADERS = {
    "User-Agent": "CANSLIMResearch/0.6 educational-research",
    "Accept-Encoding": "gzip, deflate",
    "Accept": "application/json",
}

SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SEC_TICKER_URLS = [
    "https://www.sec.gov/files/company_tickers.json",
    "https://www.sec.gov/files/company_tickers_exchange.json",
]
SEC_CIK_MAPPER_URL = (
    "https://raw.githubusercontent.com/jadchaar/sec-cik-mapper/main/"
    "mappings/stocks/ticker_to_cik.json"
)

SEC_TAG_CANDIDATES = {
    "EPS_DILUTED": ["EarningsPerShareDiluted"],
    "REVENUE": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ],
    "NET_INCOME": ["NetIncomeLoss", "ProfitLoss"],
    "DILUTED_SHARES": [
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfShareOutstandingBasicAndDiluted",
    ],
}

SEC_UNIT_PREFERENCE = {
    "EPS_DILUTED": ["USD/shares", "USD / shares"],
    "REVENUE": ["USD"],
    "NET_INCOME": ["USD"],
    "DILUTED_SHARES": ["shares"],
}

SPLIT_INTEGRITY_TOLERANCE_PCT = 5.0
SOURCE_OK_TOLERANCE_PCT = 1.0
SOURCE_ACCOUNTING_TOLERANCE_PCT = 5.0


def _clean_number(value) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _to_timestamp(value) -> Optional[pd.Timestamp]:
    try:
        ts = pd.Timestamp(value)
        if pd.isna(ts):
            return None
        if ts.tzinfo is not None:
            ts = ts.tz_localize(None)
        return ts
    except Exception:
        return None


def _now_naive() -> pd.Timestamp:
    return pd.Timestamp.now("UTC").tz_localize(None)


def _extract_row(df: Optional[pd.DataFrame], possible_names: list[str]) -> Optional[pd.Series]:
    if df is None or df.empty:
        return None
    normalized = {str(index).strip().lower(): index for index in df.index}
    for name in possible_names:
        key = name.strip().lower()
        if key in normalized:
            return df.loc[normalized[key]]
    return None


def _series_to_quarters(row: Optional[pd.Series]) -> pd.Series:
    if row is None:
        return pd.Series(dtype="float64")
    values = []
    for date, value in row.items():
        number = _clean_number(value)
        ts = _to_timestamp(date)
        if number is not None and ts is not None:
            values.append((ts, number))
    if not values:
        return pd.Series(dtype="float64")
    return pd.Series(dict(sorted(values)), dtype="float64")


def _last_values(series: pd.Series, n: int = 12) -> list[dict]:
    return [
        {"date": pd.Timestamp(date).strftime("%Y-%m-%d"), "value": float(value)}
        for date, value in series.dropna().tail(n).items()
    ]


def _latest_valid(series: pd.Series) -> Optional[float]:
    valid = series.dropna()
    return None if valid.empty else float(valid.iloc[-1])


def _nearest_index(series: pd.Series, target: pd.Timestamp, max_days: int) -> Optional[pd.Timestamp]:
    if series.empty:
        return None
    candidates = []
    for date in series.index:
        delta = abs((pd.Timestamp(date) - target).days)
        if delta <= max_days:
            candidates.append((delta, pd.Timestamp(date)))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


def _same_quarter_match(series: pd.Series, date: pd.Timestamp, max_days: int = 35) -> Optional[pd.Timestamp]:
    return _nearest_index(series, pd.Timestamp(date), max_days)


def _comparable_previous_date(series: pd.Series, current_date: pd.Timestamp, max_days: int = 45) -> Optional[pd.Timestamp]:
    return _nearest_index(series, pd.Timestamp(current_date) - pd.DateOffset(years=1), max_days)


def _growth_yoy_by_date(series: pd.Series) -> pd.Series:
    if series.empty:
        return pd.Series(dtype="float64")
    result = pd.Series(index=series.index, dtype="float64")
    for date, value in series.items():
        prior_date = _comparable_previous_date(series, pd.Timestamp(date))
        if prior_date is None:
            result.loc[date] = float("nan")
            continue
        previous = _clean_number(series.loc[prior_date])
        current = _clean_number(value)
        if previous is None or current is None or previous <= 0:
            result.loc[date] = float("nan")
        else:
            result.loc[date] = (current / previous - 1.0) * 100.0
    return result.sort_index()


def _merge_primary_with_fallback(primary: pd.Series, fallback: pd.Series, max_days: int = 35) -> pd.Series:
    rows = [(pd.Timestamp(d), float(v)) for d, v in primary.dropna().items()]
    for date, value in fallback.dropna().items():
        date = pd.Timestamp(date)
        if _same_quarter_match(primary, date, max_days) is None:
            rows.append((date, float(value)))
    if not rows:
        return pd.Series(dtype="float64")
    rows.sort(key=lambda x: x[0])
    return pd.Series(dict(rows), dtype="float64").sort_index()


def _merge_growth_primary_with_fallback(primary_raw, primary_growth, fallback_raw, fallback_growth, max_days=35):
    rows = [(pd.Timestamp(d), float(v)) for d, v in primary_growth.dropna().items()]
    for date, value in fallback_growth.dropna().items():
        date = pd.Timestamp(date)
        if _same_quarter_match(primary_raw, date, max_days) is None:
            rows.append((date, float(value)))
    if not rows:
        return pd.Series(dtype="float64")
    rows.sort(key=lambda x: x[0])
    return pd.Series(dict(rows), dtype="float64").sort_index()


def _consecutive_previous(growth: pd.Series) -> Optional[float]:
    valid = growth.dropna().sort_index()
    if len(valid) < 2:
        return None
    latest_date = pd.Timestamp(valid.index[-1])
    previous_date = pd.Timestamp(valid.index[-2])
    if not 70 <= (latest_date - previous_date).days <= 120:
        return None
    return float(valid.iloc[-2])


def _growth_acceleration(growth: pd.Series) -> Optional[float]:
    latest = _latest_valid(growth)
    previous = _consecutive_previous(growth)
    return None if latest is None or previous is None else latest - previous


def _latest_comparable_value(sec: pd.Series, yahoo: pd.Series):
    hybrid = _merge_primary_with_fallback(sec, yahoo)
    if hybrid.empty:
        return None, None, None, None, None
    latest_date = pd.Timestamp(hybrid.index[-1])
    latest_value = float(hybrid.iloc[-1])
    if latest_date in sec.index:
        source = "SEC"
        source_series = sec
    else:
        source = "YAHOO"
        source_series = yahoo
    prior_date = _comparable_previous_date(source_series, latest_date)
    prior = float(source_series.loc[prior_date]) if prior_date is not None else None
    return latest_value, prior, source, latest_date, prior_date


def _walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _walk_strings(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            yield from _walk_strings(value)


def _cik_from_yfinance_filings(stock) -> Optional[str]:
    try:
        filings = stock.sec_filings
    except Exception:
        return None
    for text in _walk_strings(filings):
        match = re.search(r"/Archives/edgar/data/(\d+)/", text)
        if match:
            return match.group(1).zfill(10)
    return None


def _cik_from_sec_catalog(symbol: str) -> Optional[str]:
    for url in SEC_TICKER_URLS:
        try:
            response = requests.get(url, headers=SEC_HEADERS, timeout=20)
            response.raise_for_status()
            payload = response.json()
        except Exception:
            continue
        if isinstance(payload, dict) and "data" in payload and "fields" in payload:
            fields = payload.get("fields", [])
            try:
                ticker_i = fields.index("ticker")
                cik_i = fields.index("cik")
            except ValueError:
                continue
            for row in payload.get("data", []):
                if str(row[ticker_i]).upper() == symbol:
                    return str(row[cik_i]).zfill(10)
        elif isinstance(payload, dict):
            for item in payload.values():
                if isinstance(item, dict) and str(item.get("ticker", "")).upper() == symbol:
                    cik = item.get("cik_str") or item.get("cik")
                    if cik is not None:
                        return str(cik).zfill(10)
    return None


def _load_cik_mapper() -> dict:
    global CIK_MAPPER_CACHE
    if CIK_MAPPER_CACHE is not None:
        return CIK_MAPPER_CACHE
    try:
        response = requests.get(
            SEC_CIK_MAPPER_URL,
            headers={"User-Agent": SEC_HEADERS["User-Agent"], "Accept": "application/json"},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        CIK_MAPPER_CACHE = payload if isinstance(payload, dict) else {}
    except Exception:
        CIK_MAPPER_CACHE = {}
    return CIK_MAPPER_CACHE


def _cik_from_sec_cik_mapper(symbol: str) -> Optional[str]:
    payload = _load_cik_mapper()
    cik = payload.get(symbol.upper())
    return str(cik).zfill(10) if cik is not None else None


def _resolve_cik(symbol: str, stock=None) -> tuple[Optional[str], str]:
    symbol = symbol.upper()
    if symbol in CIK_CACHE:
        return CIK_CACHE[symbol], "cache"
    if stock is not None:
        cik = _cik_from_yfinance_filings(stock)
        if cik:
            CIK_CACHE[symbol] = cik
            return cik, "yfinance_sec_filings"
    cik = _cik_from_sec_catalog(symbol)
    if cik:
        CIK_CACHE[symbol] = cik
        return cik, "sec_catalog"
    cik = _cik_from_sec_cik_mapper(symbol)
    if cik:
        CIK_CACHE[symbol] = cik
        return cik, "sec_cik_mapper"
    return None, "unresolved"


def _fetch_yahoo_timeseries(ticker: str, series_types: list[str], years: int = 5):
    now = int(time.time())
    period1 = now - years * 366 * 24 * 60 * 60
    period2 = now + 24 * 60 * 60
    result, errors = {}, []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/139.0 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
    }
    for series_type in series_types:
        url = f"https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{ticker}"
        params = {
            "symbol": ticker, "type": series_type, "period1": period1, "period2": period2,
            "padTimeSeries": "true", "lang": "en-US", "region": "US",
        }
        try:
            response = requests.get(url, params=params, headers=headers, timeout=15)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{series_type}: {exc}")
            continue
        rows = []
        for item in payload.get("timeseries", {}).get("result", []):
            for value in item.get(series_type, []):
                ts = _to_timestamp(value.get("asOfDate"))
                number = _clean_number(value.get("reportedValue", {}).get("raw"))
                if ts is not None and number is not None:
                    rows.append((ts, number))
        if rows:
            result[series_type] = pd.Series(dict(sorted(rows)), dtype="float64")
        else:
            errors.append(f"{series_type}: sin valores utilizables")
    return result, "; ".join(errors) if errors else None


def _get_yfinance_splits(stock, years: int = 6) -> tuple[pd.Series, Optional[str]]:
    try:
        splits = stock.splits
    except Exception as exc:
        return pd.Series(dtype="float64"), str(exc)
    if splits is None or len(splits) == 0:
        return pd.Series(dtype="float64"), None
    splits = splits.copy()
    idx = pd.to_datetime(splits.index)
    try:
        idx = idx.tz_localize(None)
    except TypeError:
        idx = idx.tz_convert(None)
    splits.index = idx
    cutoff = _now_naive() - pd.DateOffset(years=years)
    return splits[splits.index >= cutoff].sort_index().astype(float), None


def _days_between(start, end) -> Optional[int]:
    s, e = _to_timestamp(start), _to_timestamp(end)
    if s is None or e is None:
        return None
    delta = (e - s).days
    return int(delta) if pd.notna(delta) else None


def _quarter_like_fact(item: dict) -> bool:
    if item.get("form") not in {"10-Q", "10-K", "10-Q/A", "10-K/A"}:
        return False
    duration = _days_between(item.get("start"), item.get("end"))
    return duration is not None and 70 <= duration <= 110


def _series_from_sec_concept(concept: dict, metric: str, years: int) -> pd.Series:
    cutoff = _now_naive() - pd.DateOffset(years=years)
    units = concept.get("units", {})
    unit = next((u for u in SEC_UNIT_PREFERENCE[metric] if u in units), None)
    if unit is None:
        return pd.Series(dtype="float64")
    rows = []
    for item in units[unit]:
        if not _quarter_like_fact(item):
            continue
        end = _to_timestamp(item.get("end"))
        value = _clean_number(item.get("val"))
        filed = _to_timestamp(item.get("filed")) or pd.Timestamp.min
        if end is None or value is None or end < cutoff:
            continue
        rows.append((end, filed, value))
    if not rows:
        return pd.Series(dtype="float64")
    frame = pd.DataFrame(rows, columns=["end", "filed", "value"])
    frame = frame.sort_values(["end", "filed"]).drop_duplicates("end", keep="last")
    return pd.Series(frame.set_index("end")["value"].astype(float).to_dict(), dtype="float64").sort_index()


def _discover_diluted_shares_tags(facts: dict) -> list[str]:
    scored = []
    for tag, concept in facts.get("facts", {}).get("us-gaap", {}).items():
        units = concept.get("units", {})
        if "shares" not in units:
            continue
        text = f"{tag} {concept.get('label', '')} {concept.get('description', '')}".lower()
        if "weighted average" not in text and "weightedaverage" not in text:
            continue
        if "share" not in text:
            continue
        score = 0
        if "diluted" in text:
            score += 10
        if "basic and diluted" in text or "basicanddiluted" in text:
            score += 8
        if "outstanding" in text:
            score += 3
        if "basic" in text and "diluted" not in text:
            score -= 4
        if "adjustment" in text:
            score -= 8
        scored.append((score, tag))
    return [tag for _, tag in sorted(scored, key=lambda x: (-x[0], x[1]))]


def _classify_shares_tag(tag: Optional[str]) -> str:
    if not tag:
        return "NOT_AVAILABLE"
    text = tag.lower()
    if "basicanddiluted" in text or ("basic" in text and "diluted" in text):
        return "BASIC_AND_DILUTED"
    if "diluted" in text:
        return "DILUTED_EXACT"
    if "basic" in text:
        return "BASIC_FALLBACK"
    return "REVIEW_REQUIRED"


def _extract_sec_metric(facts: dict, metric: str, years: int) -> tuple[pd.Series, Optional[str]]:
    best_series = pd.Series(dtype="float64")
    best_tag = None
    candidates = list(SEC_TAG_CANDIDATES[metric])
    if metric == "DILUTED_SHARES":
        for tag in _discover_diluted_shares_tags(facts):
            if tag not in candidates:
                candidates.append(tag)
    for tag in candidates:
        concept = facts.get("facts", {}).get("us-gaap", {}).get(tag)
        if not concept:
            continue
        candidate = _series_from_sec_concept(concept, metric, years)
        if len(candidate) > len(best_series):
            best_series, best_tag = candidate, tag
    return best_series, best_tag


def _fetch_sec_companyfacts(symbol: str, stock=None, years: int = 6):
    cik, cik_source = _resolve_cik(symbol, stock)
    if not cik:
        return {}, {}, None, cik_source, "CIK no resuelto"
    try:
        response = requests.get(SEC_COMPANYFACTS_URL.format(cik=cik), headers=SEC_HEADERS, timeout=30)
        response.raise_for_status()
        facts = response.json()
    except (requests.RequestException, ValueError) as exc:
        return {}, {}, cik, cik_source, str(exc)
    out, tags = {}, {}
    for metric in SEC_TAG_CANDIDATES:
        series, tag = _extract_sec_metric(facts, metric, years)
        if not series.empty:
            out[metric] = series
            tags[metric] = tag or "N/D"
    return out, tags, cik, cik_source, None


def _implied_eps_check(eps: pd.Series, net_income: pd.Series, diluted_shares: pd.Series) -> list[dict]:
    checks = []
    for date, reported in eps.items():
        ni_date = _nearest_index(net_income, pd.Timestamp(date), 35)
        sh_date = _nearest_index(diluted_shares, pd.Timestamp(date), 35)
        if ni_date is None or sh_date is None:
            continue
        ni = _clean_number(net_income.loc[ni_date])
        shares = _clean_number(diluted_shares.loc[sh_date])
        if ni is None or shares in (None, 0):
            continue
        implied = ni / shares
        denominator = max(abs(float(reported)), 1e-9)
        diff = abs(float(reported) - implied) / denominator * 100.0
        checks.append({
            "date": pd.Timestamp(date), "reported": float(reported), "implied": float(implied),
            "diff_pct": float(diff), "pass": diff <= SPLIT_INTEGRITY_TOLERANCE_PCT,
        })
    return checks

# NOTE: remainder restored below via subsequent full-file update.
