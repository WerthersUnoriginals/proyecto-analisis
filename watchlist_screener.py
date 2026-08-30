"""
Prototipo: screener semanal de watchlist con criterio propio de Ruben.

Condiciones de entrada (según lo descrito):
- Precio cierra por encima de las medias.
- Medias HMA (high/low) por encima de la SMA amarilla (25 semanas).
- SMA amarilla por encima de la HMA blanca (hl2, 200 semanas).
- Laguerre RSI lo más próximo a 100 posible.
- RSI (config. por defecto, 14) tocando sobrecompra (>=70).
- Fase Wyckoff / estructura en "uptrend" -> de momento NO se automatiza
  (es lectura visual tuya); el script solo te avisa cuando el resto de
  condiciones numéricas se cumplen, para que TÚ confirmes la fase.

Requiere: pandas, numpy, yfinance
Ejecutar en tu propio ordenador (con acceso a internet a Yahoo Finance).
"""

import numpy as np
import pandas as pd
import yfinance as yf


# ---------- Indicadores ----------

def sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(length).mean()


def vwma(series: pd.Series, volume: pd.Series, length: int) -> pd.Series:
    """Volume Weighted Moving Average: suma(precio*volumen)/suma(volumen) en la ventana."""
    return (series * volume).rolling(length).sum() / volume.rolling(length).sum()


def wma(series: pd.Series, length: int) -> pd.Series:
    weights = np.arange(1, length + 1)
    return series.rolling(length).apply(
        lambda x: np.dot(x, weights) / weights.sum(), raw=True
    )


def hma(series: pd.Series, length: int) -> pd.Series:
    """Hull Moving Average clásica."""
    half_len = int(length / 2)
    sqrt_len = int(np.sqrt(length))
    wma_half = wma(series, half_len)
    wma_full = wma(series, length)
    raw_hma = 2 * wma_half - wma_full
    return wma(raw_hma, sqrt_len)


def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def laguerre_rsi(df: pd.DataFrame, gamma: float = 0.2) -> pd.Series:
    """
    Laguerre RSI (John Ehlers), configuración real de Ruben: gamma=0.2,
    calculado sobre el precio de CIERRE (no hl2).
    """
    price = df["Close"]
    L0 = np.zeros(len(price))
    L1 = np.zeros(len(price))
    L2 = np.zeros(len(price))
    L3 = np.zeros(len(price))
    lrsi = np.zeros(len(price))

    p = price.values
    for i in range(len(p)):
        if i == 0:
            L0[i] = L1[i] = L2[i] = L3[i] = p[i]
        else:
            L0[i] = (1 - gamma) * p[i] + gamma * L0[i - 1]
            L1[i] = -gamma * L0[i] + L0[i - 1] + gamma * L1[i - 1]
            L2[i] = -gamma * L1[i] + L1[i - 1] + gamma * L2[i - 1]
            L3[i] = -gamma * L2[i] + L2[i - 1] + gamma * L3[i - 1]

        cu = 0.0
        cd = 0.0
        if L0[i] >= L1[i]:
            cu += L0[i] - L1[i]
        else:
            cd += L1[i] - L0[i]
        if L1[i] >= L2[i]:
            cu += L1[i] - L2[i]
        else:
            cd += L2[i] - L1[i]
        if L2[i] >= L3[i]:
            cu += L2[i] - L3[i]
        else:
            cd += L3[i] - L2[i]

        lrsi[i] = 0.0 if (cu + cd) == 0 else cu / (cu + cd)

    return pd.Series(lrsi * 100, index=price.index)


# ---------- Screener ----------

def calcular_hma_mensual_hl2(ticker: str, length: int = 200) -> pd.Series:
    """
    Descarga histórico mensual (necesita ~200 meses = ~17 años) y calcula
    el HMA(200) sobre hl2 en timeframe mensual -> esta es la línea BLANCA.
    Devuelve una serie indexada por fecha de cierre de cada mes.
    """
    df_m = yf.download(ticker, period="max", interval="1mo", progress=False)
    if isinstance(df_m.columns, pd.MultiIndex):
        df_m.columns = df_m.columns.get_level_values(0)

    if df_m.empty or len(df_m) < length + 5:
        # Histórico mensual insuficiente para HMA200 mensual (raro salvo cotizaciones muy recientes)
        return pd.Series(dtype=float)

    hl2_m = (df_m["High"] + df_m["Low"]) / 2
    return hma(hl2_m, length)


def analizar_ticker(ticker: str) -> dict:
    df = yf.download(ticker, period="6y", interval="1wk", progress=False)
    if df.empty or len(df) < 210:
        return {"ticker": ticker, "error": "datos semanales insuficientes"}

    # Si yfinance devuelve columnas MultiIndex (por ticker), aplanar
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    close = df["Close"]
    volume = df["Volume"]

    # "Espere al cierre de los intervalos de tiempo": si la última vela es la semana
    # en curso (aún sin cerrar), la descartamos para que todos los indicadores usen
    # solo velas ya cerradas, igual que en tu configuración de TradingView.
    hoy = pd.Timestamp.today().normalize()
    if df.index[-1] + pd.Timedelta(days=6) >= hoy:  # la última vela semanal aún no ha cerrado
        df = df.iloc[:-1]
        close = df["Close"]
        volume = df["Volume"]

    if len(df) < 210:
        return {"ticker": ticker, "error": "datos semanales insuficientes tras excluir semana en curso"}

    sma_base = sma(close, 25)
    df["amarilla"] = vwma(sma_base, volume, 25)   # VWMA(25) aplicado sobre SMA(25) -> línea amarilla real
    df["hma200_high"] = hma(df["High"], 200)      # gris 1: canal superior (HMA 200 semanas sobre máximos)
    df["hma200_low"] = hma(df["Low"], 200)        # gris 2: canal inferior (HMA 200 semanas sobre mínimos)
    df["rsi14"] = rsi(close, 14)
    df["lrsi"] = laguerre_rsi(df)

    # Blanca: HMA 200 MENSUAL sobre hl2, trasladada a la vista semanal
    hma_mensual = calcular_hma_mensual_hl2(ticker, 200)
    if hma_mensual.empty:
        df["hma_blanca"] = np.nan
    else:
        semanal_fechas = df.index.to_series().rename("fecha_semana").reset_index(drop=True)
        mensual_valores = hma_mensual.rename("hma_blanca").rename_axis("fecha_mes").reset_index()
        alineado = pd.merge_asof(
            pd.DataFrame({"fecha_semana": semanal_fechas}).sort_values("fecha_semana"),
            mensual_valores.sort_values("fecha_mes"),
            left_on="fecha_semana",
            right_on="fecha_mes",
            direction="backward",  # último mes YA CERRADO hasta esa semana
        )
        df["hma_blanca"] = alineado["hma_blanca"].values

    ultima = df.iloc[-1]

    condiciones = {
        # Precio por encima de TODAS las medias: amarilla y el canal gris completo (incluida la superior)
        "precio_sobre_medias": bool(
            ultima["Close"] > ultima["amarilla"] and ultima["Close"] > ultima["hma200_high"]
        ),
        # Canal gris (high/low) por encima de la amarilla
        "canal_sobre_sma25": bool(ultima["hma200_low"] > ultima["amarilla"]),
        # Amarilla por encima de la blanca (HMA mensual 200 hl2)
        "sma25_sobre_hma_blanca": bool(pd.notna(ultima["hma_blanca"]) and ultima["amarilla"] > ultima["hma_blanca"]),
        "rsi_sobrecompra": bool(ultima["rsi14"] > 69),
        "laguerre_alto": bool(ultima["lrsi"] > 85),
    }

    return {
        "ticker": ticker,
        "fecha": df.index[-1].date(),
        "close": round(float(ultima["Close"]), 2),
        "amarilla": round(float(ultima["amarilla"]), 2),
        "hma200_high": round(float(ultima["hma200_high"]), 2),
        "hma200_low": round(float(ultima["hma200_low"]), 2),
        "hma200_hl2_blanca": round(float(ultima["hma_blanca"]), 2) if pd.notna(ultima["hma_blanca"]) else None,
        "rsi14": round(float(ultima["rsi14"]), 1),
        "laguerre": round(float(ultima["lrsi"]), 1),
        "condiciones": condiciones,
        "cumple_todo": all(condiciones.values()),
    }


if __name__ == "__main__":
    import csv
    import time as _time

    watchlist = ['AZO', 'MTD', 'TDG', 'LLY', 'BLK', 'EQIX', 'GS', 'PH', 'COST', 'MU',
                 'MCK', 'CAT', 'REGN', 'DE', 'TMO', 'MA', 'META', 'MSCI', 'LMT', 'AMP',
                 'IDXX', 'NOC', 'VRTX', 'MCO', 'MSFT', 'BRK-B', 'LIN', 'AMD', 'AMAT', 'DELL',
                 'TT', 'SPGI', 'SNPS', 'AMGN', 'ROK', 'ROP', 'HCA', 'ETN', 'ELV', 'UNH',
                 'HUM', 'V', 'GD', 'ISRG', 'PANW', 'TRV', 'MPC', 'AVGO', 'ADI', 'INTU',
                 'JPM', 'AON', 'VLO', 'MAR', 'NSC', 'GOOGL', 'SHW', 'GOOG', 'GE', 'CDNS',
                 'CB', 'AXP', 'FDX', 'SYK', 'HD', 'EXPE', 'HLT', 'AAPL', 'PSA', 'APD',
                 'UNP', 'LRCX', 'ADBE', 'ADP', 'CME', 'ITW', 'RCL', 'CI', 'JNJ', 'AJG',
                 'AMZN', 'MCD', 'LHX', 'ADSK', 'ALL', 'TXN', 'CRM', 'ABBV', 'NUE', 'PSX',
                 'PNC', 'WELL', 'AME', 'IBM', 'STLD', 'ROST', 'NXPI', 'RSG', 'WM', 'PGR']

    resultados = []
    for i, t in enumerate(watchlist, 1):
        try:
            res = analizar_ticker(t)
            resultados.append(res)
            print(f"[{i}/{len(watchlist)}] {t}: OK")
        except Exception as e:
            print(f"[{i}/{len(watchlist)}] {t}: ERROR -> {e}")
            resultados.append({"ticker": t, "error": str(e)})
        _time.sleep(0.3)  # margen prudente entre tickers

    # Separar los que tienen error de los que sí calcularon
    validos = [r for r in resultados if "error" not in r]
    con_error = [r for r in resultados if "error" in r]

    # Ordenar por número de condiciones cumplidas (de más a menos)
    def num_condiciones_ok(r):
        return sum(1 for v in r["condiciones"].values() if v)

    validos.sort(key=num_condiciones_ok, reverse=True)

    print("\n" + "=" * 70)
    print("RESUMEN - ordenado por nº de condiciones cumplidas")
    print("=" * 70)
    for r in validos:
        n_ok = num_condiciones_ok(r)
        marca = " <-- CUMPLE TODO" if r["cumple_todo"] else ""
        print(f"{r['ticker']:8s}  {n_ok}/5 condiciones  (close={r['close']}){marca}")

    if con_error:
        print(f"\n{len(con_error)} tickers con error (revisar manualmente): "
              f"{[r['ticker'] for r in con_error]}")

    # Exportar a CSV para revisión con calma
    csv_path = "resultado_watchlist.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["ticker", "fecha", "close", "amarilla", "hma200_high", "hma200_low",
                          "hma200_hl2_blanca", "rsi14", "laguerre", "n_condiciones_ok",
                          "cumple_todo"] + list(validos[0]["condiciones"].keys()) if validos else [])
        for r in validos:
            writer.writerow([
                r["ticker"], r["fecha"], r["close"], r["amarilla"], r["hma200_high"],
                r["hma200_low"], r["hma200_hl2_blanca"], r["rsi14"], r["laguerre"],
                num_condiciones_ok(r), r["cumple_todo"],
            ] + [r["condiciones"][k] for k in r["condiciones"].keys()])

    print(f"\nResultado completo guardado en: {csv_path}")