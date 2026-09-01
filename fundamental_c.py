"""
CAN SLIM+ — Módulo C: Current Earnings v2.5.2

Mejoras v2.5.2:
- SEC Company Facts como histórico principal y Yahoo/yfinance como complemento.
- YoY por trimestre comparable real, no por posición i-4.
- Resolver ticker→CIK con caché, yfinance/filings, catálogo SEC y mapping
  pre-generado desde datos SEC como fallback externo.
- Detección de splits con yfinance.
- Verificación de EPS SEC mediante Net Income / Diluted Shares para comprobar si
  los datos históricos ya están ajustados por splits antes de tocar nada.
- Discovery conservador de conceptos XBRL de diluted shares cuando los tags
  estándar no aparecen.
- Control explícito de discrepancias SEC↔Yahoo por métrica.

No aplica todavía un score CAN SLIM definitivo.

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


CIK_CACHE = {
    "NVDA": "0001045810",
    "MSFT": "0000789019",
    "COST": "0000909832",
    "XOM": "0000034088",
    "AMZN": "0001018724",
}

SEC_HEADERS = {
    "User-Agent": "CANSLIMResearch/0.5 educational-research",
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


def _cik_from_sec_cik_mapper(symbol: str) -> Optional[str]:
    try:
        response = requests.get(
            SEC_CIK_MAPPER_URL,
            headers={"User-Agent": SEC_HEADERS["User-Agent"], "Accept": "application/json"},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
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
    return None if s is None or e is None else int((e - s).days)


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
        if "adjustment" in text:
            score -= 8
        scored.append((score, tag))
    return [tag for _, tag in sorted(scored, key=lambda x: (-x[0], x[1]))]


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


def _check_for_date(checks: list[dict], date: Optional[pd.Timestamp], max_days: int = 45) -> Optional[dict]:
    if date is None or not checks:
        return None
    candidates = [(abs((item["date"] - date).days), item) for item in checks]
    candidates = [item for item in candidates if item[0] <= max_days]
    return None if not candidates else min(candidates, key=lambda x: x[0])[1]


def _assess_split_integrity(splits, eps, net_income, diluted_shares) -> list[dict]:
    if splits.empty:
        return []
    checks = _implied_eps_check(eps, net_income, diluted_shares)
    events = []
    for split_date, split_ratio in splits.items():
        split_date = pd.Timestamp(split_date)
        before_dates = [pd.Timestamp(d) for d in eps.index if pd.Timestamp(d) < split_date]
        after_dates = [pd.Timestamp(d) for d in eps.index if pd.Timestamp(d) >= split_date]
        before = max(before_dates) if before_dates else None
        after = min(after_dates) if after_dates else None
        before_check = _check_for_date(checks, before)
        after_check = _check_for_date(checks, after)
        sh_before_date = _nearest_index(diluted_shares, before, 35) if before is not None else None
        sh_after_date = _nearest_index(diluted_shares, after, 35) if after is not None else None
        sh_before = float(diluted_shares.loc[sh_before_date]) if sh_before_date is not None else None
        sh_after = float(diluted_shares.loc[sh_after_date]) if sh_after_date is not None else None
        observed_ratio = sh_after / sh_before if sh_before and sh_after else None
        both_eps_pass = bool(before_check and after_check and before_check["pass"] and after_check["pass"])
        if observed_ratio is not None:
            dist_to_one = abs(math.log(max(observed_ratio, 1e-12)))
            dist_to_split = abs(math.log(max(observed_ratio, 1e-12) / max(float(split_ratio), 1e-12)))
        else:
            dist_to_one = dist_to_split = None
        if both_eps_pass and observed_ratio is not None and dist_to_one < dist_to_split:
            status = "ALREADY_ADJUSTED"
        elif both_eps_pass and observed_ratio is not None and dist_to_split < dist_to_one:
            status = "UNADJUSTED"
        else:
            status = "REVIEW_REQUIRED"
        events.append({
            "date": split_date.strftime("%Y-%m-%d"),
            "ratio": float(split_ratio),
            "status": status,
            "before_eps_date": before.strftime("%Y-%m-%d") if before is not None else None,
            "after_eps_date": after.strftime("%Y-%m-%d") if after is not None else None,
            "before_eps_diff_pct": before_check["diff_pct"] if before_check else None,
            "after_eps_diff_pct": after_check["diff_pct"] if after_check else None,
            "diluted_shares_observed_ratio": observed_ratio,
        })
    return events


def _source_consistency(sec: pd.Series, yahoo: pd.Series) -> dict:
    diffs = []
    for y_date, y_value in yahoo.items():
        s_date = _same_quarter_match(sec, pd.Timestamp(y_date), 35)
        if s_date is None:
            continue
        s_value = float(sec.loc[s_date])
        y_value = float(y_value)
        denominator = max(abs(s_value), abs(y_value), 1e-9)
        diffs.append(abs(s_value - y_value) / denominator * 100.0)
    if not diffs:
        return {"status": "N/D", "overlaps": 0, "max_diff_pct": None, "avg_diff_pct": None}
    max_diff = max(diffs)
    avg_diff = sum(diffs) / len(diffs)
    if max_diff <= SOURCE_OK_TOLERANCE_PCT:
        status = "OK"
    elif max_diff <= SOURCE_ACCOUNTING_TOLERANCE_PCT:
        status = "DISCREPANCIA_CONTABLE"
    else:
        status = "DISCREPANCIA_ALTA"
    return {"status": status, "overlaps": len(diffs), "max_diff_pct": max_diff, "avg_diff_pct": avg_diff}


def _split_between(splits: pd.Series, prior_date, current_date) -> bool:
    if prior_date is None or current_date is None or splits.empty:
        return False
    return bool(((splits.index > pd.Timestamp(prior_date)) & (splits.index <= pd.Timestamp(current_date))).any())


def _history_records(history: pd.DataFrame, n: int = 8) -> list[dict]:
    if history is None or history.empty:
        return []
    frame = history.tail(n).copy().reset_index()
    records = []
    for _, row in frame.iterrows():
        record = {}
        for column, value in row.items():
            if isinstance(value, pd.Timestamp):
                record[column] = value.strftime("%Y-%m-%d")
            elif pd.isna(value):
                record[column] = None
            else:
                number = _clean_number(value)
                record[column] = number if number is not None else value
        records.append(record)
    return records


def _extract_estimate_table(table: Optional[pd.DataFrame], aliases: dict[str, list[str]]) -> dict:
    if table is None or table.empty:
        return {}
    normalized = {str(index).strip().lower(): index for index in table.index}
    result = {}
    for canonical, names in aliases.items():
        for name in names:
            key = name.strip().lower()
            if key in normalized:
                row = table.loc[normalized[key]]
                result[canonical] = {str(c): _clean_number(v) for c, v in row.items()}
                break
    return result


def analyze_current_earnings(ticker: str) -> dict:
    symbol = ticker.upper().strip()
    stock = yf.Ticker(symbol)
    try:
        income = stock.quarterly_income_stmt
        income_error = None
    except Exception as exc:
        income, income_error = pd.DataFrame(), str(exc)

    yf_eps = _series_to_quarters(_extract_row(income, ["Diluted EPS", "Basic EPS", "DilutedEPS", "BasicEPS"]))
    yf_revenue = _series_to_quarters(_extract_row(income, ["Total Revenue", "Operating Revenue"]))
    yf_net_income = _series_to_quarters(_extract_row(income, ["Net Income", "Net Income Common Stockholders"]))

    yahoo_ts, timeseries_error = _fetch_yahoo_timeseries(
        symbol, ["quarterlyDilutedEPS", "quarterlyTotalRevenue", "quarterlyNetIncome"], years=5
    )
    yahoo_eps = _merge_primary_with_fallback(yahoo_ts.get("quarterlyDilutedEPS", pd.Series(dtype="float64")), yf_eps)
    yahoo_revenue = _merge_primary_with_fallback(yahoo_ts.get("quarterlyTotalRevenue", pd.Series(dtype="float64")), yf_revenue)
    yahoo_net_income = _merge_primary_with_fallback(yahoo_ts.get("quarterlyNetIncome", pd.Series(dtype="float64")), yf_net_income)

    sec_data, sec_tags, cik, cik_source, sec_error = _fetch_sec_companyfacts(symbol, stock=stock, years=6)
    sec_eps = sec_data.get("EPS_DILUTED", pd.Series(dtype="float64"))
    sec_revenue = sec_data.get("REVENUE", pd.Series(dtype="float64"))
    sec_net_income = sec_data.get("NET_INCOME", pd.Series(dtype="float64"))
    sec_diluted_shares = sec_data.get("DILUTED_SHARES", pd.Series(dtype="float64"))

    eps = _merge_primary_with_fallback(sec_eps, yahoo_eps)
    revenue = _merge_primary_with_fallback(sec_revenue, yahoo_revenue)
    net_income = _merge_primary_with_fallback(sec_net_income, yahoo_net_income)

    sec_eps_growth, yahoo_eps_growth = _growth_yoy_by_date(sec_eps), _growth_yoy_by_date(yahoo_eps)
    eps_growth = _merge_growth_primary_with_fallback(sec_eps, sec_eps_growth, yahoo_eps, yahoo_eps_growth)
    sec_revenue_growth, yahoo_revenue_growth = _growth_yoy_by_date(sec_revenue), _growth_yoy_by_date(yahoo_revenue)
    revenue_growth = _merge_growth_primary_with_fallback(sec_revenue, sec_revenue_growth, yahoo_revenue, yahoo_revenue_growth)

    latest_eps, previous_eps_comparable, latest_eps_source, latest_eps_date, previous_eps_date = _latest_comparable_value(sec_eps, yahoo_eps)
    latest_revenue = _latest_valid(revenue)

    splits, splits_error = _get_yfinance_splits(stock, years=6)
    split_events = _assess_split_integrity(splits, sec_eps, sec_net_income, sec_diluted_shares)
    split_statuses = {event["status"] for event in split_events}
    if not split_events:
        split_integrity_status = "NO_RECENT_SPLITS" if splits_error is None else "UNKNOWN"
    elif split_statuses == {"ALREADY_ADJUSTED"}:
        split_integrity_status = "VERIFIED_ALREADY_ADJUSTED"
    elif "UNADJUSTED" in split_statuses:
        split_integrity_status = "UNADJUSTED_DETECTED"
    else:
        split_integrity_status = "REVIEW_REQUIRED"
    current_yoy_crosses_split = _split_between(splits, previous_eps_date, latest_eps_date)

    consistency = {
        "EPS": _source_consistency(sec_eps, yahoo_eps),
        "REVENUE": _source_consistency(sec_revenue, yahoo_revenue),
        "NET_INCOME": _source_consistency(sec_net_income, yahoo_net_income),
    }

    eps_loss_to_profit = (
        latest_eps is not None and previous_eps_comparable is not None
        and previous_eps_comparable <= 0 and latest_eps > 0
    )

    surprises = []
    try:
        surprises = _history_records(stock.earnings_history, n=8)
    except Exception:
        pass
    surprise_values = []
    for record in surprises:
        value = _clean_number(record.get("surprisePercent"))
        if value is not None:
            surprise_values.append(value * 100.0)

    estimates, revisions, growth_estimates = {}, {}, {}
    try:
        estimates = _extract_estimate_table(stock.eps_estimate, {
            "current_qtr": ["Current Qtr.", "0q"], "next_qtr": ["Next Qtr.", "+1q"],
            "current_year": ["Current Year", "0y"], "next_year": ["Next Year", "+1y"],
        })
    except Exception:
        pass
    try:
        revisions = _extract_estimate_table(stock.eps_revisions, {
            "up_7d": ["Up Last 7 Days"], "up_30d": ["Up Last 30 Days"],
            "down_7d": ["Down Last 7 Days"], "down_30d": ["Down Last 30 Days"],
        })
    except Exception:
        pass
    try:
        growth_estimates = _extract_estimate_table(stock.growth_estimates, {
            "current_qtr": ["Current Qtr.", "0q"], "next_qtr": ["Next Qtr.", "+1q"],
            "current_year": ["Current Year", "0y"], "next_year": ["Next Year", "+1y"],
        })
    except Exception:
        pass

    latest_eps_yoy = _latest_valid(eps_growth)
    previous_eps_yoy = _consecutive_previous(eps_growth)
    eps_acceleration = _growth_acceleration(eps_growth)
    latest_revenue_yoy = _latest_valid(revenue_growth)
    previous_revenue_yoy = _consecutive_previous(revenue_growth)
    revenue_acceleration = _growth_acceleration(revenue_growth)

    core_complete = all(v is not None for v in [
        latest_eps_yoy, previous_eps_yoy, eps_acceleration,
        latest_revenue_yoy, previous_revenue_yoy, revenue_acceleration,
    ])
    if core_complete:
        data_quality = "completa"
    elif latest_eps_yoy is not None and latest_revenue_yoy is not None:
        data_quality = "suficiente"
    elif latest_eps_yoy is not None or latest_revenue_yoy is not None:
        data_quality = "parcial"
    else:
        data_quality = "insuficiente"
    if current_yoy_crosses_split and split_integrity_status in {"UNADJUSTED_DETECTED", "REVIEW_REQUIRED", "UNKNOWN"}:
        data_quality = "revision_split"

    eps_change_type = "perdidas_a_beneficios" if eps_loss_to_profit else ("sin_datos" if latest_eps is None else "normal")

    return {
        "ticker": symbol,
        "version": "2.5.2",
        "data_source": "SEC Company Facts + Yahoo Finance/yfinance",
        "cik": cik,
        "cik_source": cik_source,
        "error": income_error,
        "timeseries_error": timeseries_error,
        "sec_error": sec_error,
        "splits_error": splits_error,
        "sec_tags": sec_tags,
        "eps_quarters": _last_values(eps, 16),
        "revenue_quarters": _last_values(revenue, 16),
        "net_income_quarters": _last_values(net_income, 16),
        "eps_yoy_pct": _last_values(eps_growth.dropna(), 12),
        "revenue_yoy_pct": _last_values(revenue_growth.dropna(), 12),
        "latest_eps": latest_eps,
        "latest_eps_source": latest_eps_source,
        "latest_eps_yoy_pct": latest_eps_yoy,
        "previous_eps_yoy_pct": previous_eps_yoy,
        "eps_acceleration_pp": eps_acceleration,
        "latest_revenue": latest_revenue,
        "latest_revenue_yoy_pct": latest_revenue_yoy,
        "previous_revenue_yoy_pct": previous_revenue_yoy,
        "revenue_acceleration_pp": revenue_acceleration,
        "latest_eps_positive": latest_eps is not None and latest_eps > 0,
        "eps_loss_to_profit": eps_loss_to_profit,
        "eps_change_type": eps_change_type,
        "earnings_surprises": surprises,
        "latest_eps_surprise_pct": surprise_values[-1] if surprise_values else None,
        "positive_surprises_count": sum(1 for v in surprise_values if v > 0),
        "surprises_available": len(surprise_values),
        "eps_estimates": estimates,
        "eps_revisions": revisions,
        "growth_estimates": growth_estimates,
        "quarters_eps_available": len(eps),
        "quarters_revenue_available": len(revenue),
        "eps_yoy_calculable": int(eps_growth.dropna().shape[0]),
        "revenue_yoy_calculable": int(revenue_growth.dropna().shape[0]),
        "sec_eps_quarters": len(sec_eps),
        "sec_revenue_quarters": len(sec_revenue),
        "sec_diluted_shares_quarters": len(sec_diluted_shares),
        "yahoo_eps_quarters": len(yahoo_eps),
        "yahoo_revenue_quarters": len(yahoo_revenue),
        "splits": [{"date": pd.Timestamp(d).strftime("%Y-%m-%d"), "ratio": float(v)} for d, v in splits.items()],
        "split_events": split_events,
        "split_integrity_status": split_integrity_status,
        "current_yoy_crosses_split": current_yoy_crosses_split,
        "source_consistency": consistency,
        "data_quality": data_quality,
    }


def _fmt(value, suffix="") -> str:
    return "N/D" if value is None else f"{value:.2f}{suffix}"


def print_report(report: dict) -> None:
    print(f"\n{'=' * 78}")
    print(f"CAN SLIM+ v{report['version']} | C — {report['ticker']}")
    print(f"{'=' * 78}")
    print(f"CIK: {report['cik'] or 'N/D'} | resolver: {report['cik_source']}")
    if report.get("error"):
        print(f"Aviso yfinance: {report['error']}")
    if report.get("timeseries_error"):
        print(f"Aviso Yahoo TS: {report['timeseries_error']}")
    if report.get("sec_error"):
        print(f"Aviso SEC: {report['sec_error']}")
    if report.get("splits_error"):
        print(f"Aviso splits: {report['splits_error']}")
    print("\n[EPS]")
    print(f"  Último EPS:             {_fmt(report['latest_eps'])}")
    print(f"  Fuente último EPS:      {report['latest_eps_source'] or 'N/D'}")
    print(f"  EPS YoY actual:         {_fmt(report['latest_eps_yoy_pct'], '%')}")
    print(f"  EPS YoY anterior:       {_fmt(report['previous_eps_yoy_pct'], '%')}")
    print(f"  Aceleración EPS:        {_fmt(report['eps_acceleration_pp'], ' pp')}")
    print("\n[VENTAS]")
    print(f"  Ventas YoY actual:      {_fmt(report['latest_revenue_yoy_pct'], '%')}")
    print(f"  Ventas YoY anterior:    {_fmt(report['previous_revenue_yoy_pct'], '%')}")
    print(f"  Aceleración ventas:     {_fmt(report['revenue_acceleration_pp'], ' pp')}")
    print("\n[SPLITS / INTEGRIDAD EPS]")
    print(f"  Estado:                 {report['split_integrity_status']}")
    print(f"  YoY actual cruza split: {report['current_yoy_crosses_split']}")
    if report["splits"]:
        print(f"  Splits recientes:       {report['splits']}")
    for event in report["split_events"]:
        print(
            f"  {event['date']} x{event['ratio']:g} -> {event['status']} | "
            f"shares ratio={_fmt(event['diluted_shares_observed_ratio'])} | "
            f"EPS diff antes/después={_fmt(event['before_eps_diff_pct'], '%')}/{_fmt(event['after_eps_diff_pct'], '%')}"
        )
    print("\n[CONSISTENCIA SEC ↔ YAHOO]")
    for metric, info in report["source_consistency"].items():
        print(
            f"  {metric:10s}: {info['status']} | solapes={info['overlaps']} | "
            f"max diff={_fmt(info['max_diff_pct'], '%')} | media={_fmt(info['avg_diff_pct'], '%')}"
        )
    print("\n[SORPRESA EPS]")
    print(f"  Última sorpresa:        {_fmt(report['latest_eps_surprise_pct'], '%')}")
    print(f"  Sorpresas positivas:    {report['positive_surprises_count']}/{report['surprises_available']}")
    print("\n[DATOS]")
    print(f"  SEC EPS / ventas:       {report['sec_eps_quarters']} / {report['sec_revenue_quarters']}")
    print(f"  SEC diluted shares:     {report['sec_diluted_shares_quarters']}")
    print(f"  Yahoo EPS / ventas:     {report['yahoo_eps_quarters']} / {report['yahoo_revenue_quarters']}")
    print(f"  Híbrido EPS / ventas:   {report['quarters_eps_available']} / {report['quarters_revenue_available']}")
    print(f"  YoY EPS calculables:    {report['eps_yoy_calculable']}")
    print(f"  YoY ventas calculables: {report['revenue_yoy_calculable']}")
    print(f"  Tags SEC:               {report['sec_tags']}")
    print(f"  Calidad:                {report['data_quality']}")
    print("\n[HISTÓRICO EPS YoY]")
    for item in report["eps_yoy_pct"]:
        print(f"  {item['date']}: {item['value']:+.2f}%")
    print("\n[HISTÓRICO VENTAS YoY]")
    for item in report["revenue_yoy_pct"]:
        print(f"  {item['date']}: {item['value']:+.2f}%")
    if report.get("growth_estimates"):
        print("\n[CRECIMIENTO ESTIMADO]")
        for name, values in report["growth_estimates"].items():
            print(f"  {name}: {values}")


if __name__ == "__main__":
    TEST_TICKERS = ["NVDA", "MSFT", "COST", "XOM", "AMZN", "AAPL"]
    for ticker in TEST_TICKERS:
        print_report(analyze_current_earnings(ticker))
