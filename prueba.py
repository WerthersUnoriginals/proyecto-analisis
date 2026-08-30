import yfinance as yf
df = yf.download("ADI", period="2y", interval="1wk", progress=False)
if hasattr(df.columns, 'get_level_values'):
    df.columns = df.columns.get_level_values(0)
print(df[["Close"]].tail(27))
print("Media manual de las ultimas 25 velas:", df["Close"].tail(25).mean())