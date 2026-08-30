import yfinance as yf
import pandas as pd
from watchlist_screener import sma, vwma

df = yf.download("ADI", period="2y", interval="1wk", progress=False, auto_adjust=False)
if hasattr(df.columns, 'get_level_values'):
    df.columns = df.columns.get_level_values(0)

df = df.iloc[:-1]

close = df["Close"]
volume = df["Volume"]
base = sma(close, 25)
amarilla = vwma(base, volume, 25)

print("Ultimo cierre (sin ajustar):", close.iloc[-1])
print("Ultimo valor amarilla (VWMA, sin ajustar):", amarilla.iloc[-1])