"""
Selecciona, de la lista completa de tickers, las 100 acciones con mayor
precio por unidad (precio de cierre más reciente), y las deja listas
para usar como watchlist en watchlist_screener.py.

Requiere: yfinance
Ejecutar en tu PC (necesita acceso a internet / Yahoo Finance).
"""

import time
import yfinance as yf

TICKERS_RAW = """NYSE:WELL,NASDAQ:NFLX,NYSE:UNP,NASDAQ:CTAS,NYSE:AME,NYSE:NKE,NASDAQ:MU,NYSE:MCK,NYSE:NOC,NYSE:TT,NYSE:NSC,NASDAQ:AMZN,NASDAQ:ADBE,NYSE:COF,NASDAQ:BKNG,NYSE:DG,NYSE:PRU,NYSE:GIS,NASDAQ:HBAN,NASDAQ:NTRS,NYSE:AZO,NYSE:TDG,NYSE:MTD,NYSE:LLY,NYSE:GS,NASDAQ:EQIX,NYSE:BLK,NASDAQ:COST,NYSE:CAT,NYSE:PH,NASDAQ:REGN,NYSE:MSCI,NASDAQ:META,NASDAQ:AMAT,NYSE:DE,NASDAQ:IDXX,NASDAQ:AMD,NYSE:LMT,NASDAQ:LIN,NYSE:BRK.B,NYSE:MA,NYSE:TMO,LSE:MRO,NYSE:ROK,NYSE:AMP,NASDAQ:SNPS,NYSE:MCO,NASDAQ:VRTX,NASDAQ:ADI,NYSE:SPGI,NASDAQ:ISRG,NYSE:UNH,NYSE:DELL,NYSE:ETN,NYSE:ELV,NASDAQ:MAR,NASDAQ:MSFT,NASDAQ:CDNS,NASDAQ:AVGO,NYSE:HCA,NASDAQ:LRCX,NYSE:HUM,NASDAQ:GOOGL,NASDAQ:GOOG,NYSE:GD,NASDAQ:AMGN,NYSE:HLT,NYSE:GE,NYSE:FDX,NYSE:AXP,NASDAQ:ROP,NYSE:HD,NYSE:AON,NYSE:CB,NYSE:V,NYSE:PSA,NYSE:SHW,NYSE:JPM,NASDAQ:NXPI,NYSE:RCL,NASDAQ:TXN,NYSE:SYK,NYSE:TRV,NYSE:LHX,NASDAQ:AAPL,NYSE:CI,NYSE:MCD,NASDAQ:PANW,NYSE:APD,NASDAQ:INTU,NASDAQ:STLD,NYSE:IBM,NASDAQ:CME,NYSE:ITW,NYSE:NUE,NASDAQ:KLAC,NYSE:MPC,NYSE:VLO,NASDAQ:ODFL,NASDAQ:EXPE,NASDAQ:ROST,NYSE:JNJ,NYSE:PNC,NASDAQ:HON,NASDAQ:ADP,NYSE:ALL,NYSE:ABBV,NASDAQ:QCOM,NYSE:LOW,NYSE:MS,NYSE:WM,NYSE:TEL,NYSE:AJG,NASDAQ:NVDA,NYSE:RSG,NYSE:PGR,NASDAQ:ADSK,NYSE:ORCL,NASDAQ:FANG,NASDAQ:TMUS,NYSE:GLW,NYSE:RTX,NYSE:PM,NYSE:DHR,NYSE:CVX,NYSE:PSX,NYSE:ANET,NYSE:STT,NYSE:TJX,NYSE:ACN,NYSE:CRM,NYSE:APH,NYSE:MMM,NYSE:YUM,NYSE:PG,NASDAQ:FTNT,NYSE:PLD,NYSE:EMR,NASDAQ:PEP,NYSE:JCI,NASDAQ:CHTR,NYSE:IT,NYSE:BK,NYSE:C,NYSE:XOM,NYSE:ICE,NYSE:CBRE,NYSE:TGT,NYSE:EOG,NYSE:A,NASDAQ:AEP,LSE:DFS,NASDAQ:INTC,NYSE:DUK,NASDAQ:GILD,NASDAQ:WMT,NASDAQ:PCAR,NASDAQ:CSCO,NASDAQ:UAL,NYSE:AFL,NYSE:MRK,NASDAQ:DLTR,NYSE:COP,NYSE:AEE,NASDAQ:EBAY,NASDAQ:TROW,NYSE:ED,NYSE:NEM,NYSE:NOW,NASDAQ:KMB,NYSE:DIS,NASDAQ:SBUX,NASDAQ:PAYX,NYSE:CVS,NYSE:SO,NYSE:TXT,NASDAQ:MNST,NYSE:SRE,NYSE:SCHW,NYSE:CL,NASDAQ:ORLY,NYSE:ABT,NYSE:MET,NYSE:OKE,NYSE:EW,NYSE:NEE,NYSE:DAL,NYSE:GM,NYSE:WFC,NYSE:KO,NYSE:MDT,NYSE:PEG,NYSE:ZTS,NASDAQ:XEL,NYSE:IR,NYSE:AIG,NYSE:SYF,NYSE:EIX,NYSE:OTIS,NYSE:WMB,NYSE:CARR,NYSE:FCX,NYSE:MO,NYSE:D,NYSE:CFG,NYSE:APTV,NYSE:KR,NYSE:LYB,NASDAQ:BKR,NYSE:USB,NYSE:BAC,NYSE:OXY,NYSE:SLB,NASDAQ:CTSH,NYSE:EQT,NYSE:DD,NYSE:TFC,NASDAQ:CSX,NYSE:VZ,NYSE:BSX,NASDAQ:EXC,NASDAQ:FAST,NYSE:LUV,NYSE:DVN,NASDAQ:PYPL,NYSE:HAL,NASDAQ:APA,NYSE:DOW,NYSE:CMG,NYSE:BEN,NYSE:KMI,NYSE:CCL,NYSE:IVZ,NYSE:RF,NYSE:HPQ,NASDAQ:KHC,NASDAQ:CMCSA,NYSE:KEY,NYSE:NCLH,NYSE:PCG,NASDAQ:AAL,NYSE:F"""


def limpiar_ticker(raw: str) -> str:
    """'NASDAQ:ADI' -> 'ADI'; 'NYSE:BRK.B' -> 'BRK-B' (formato que espera yfinance)."""
    simbolo = raw.split(":")[-1]
    return simbolo.replace(".", "-")


def main():
    tickers_originales = [t.strip() for t in TICKERS_RAW.split(",") if t.strip()]
    tickers_limpios = [limpiar_ticker(t) for t in tickers_originales]

    print(f"Total de tickers a consultar: {len(tickers_limpios)}\n")

    precios = []
    for original, ticker in zip(tickers_originales, tickers_limpios):
        try:
            info = yf.Ticker(ticker).fast_info
            precio = info.get("lastPrice") or info.get("last_price")
            if precio is None:
                raise ValueError("sin precio en fast_info")
            precios.append((original, ticker, float(precio)))
        except Exception as e:
            print(f"  Aviso: no se pudo obtener precio de {original} ({ticker}): {e}")
        time.sleep(0.15)  # margen prudente para no saturar peticiones

    precios.sort(key=lambda x: x[2], reverse=True)

    top_100 = precios[:100]

    print("\n--- TOP 100 por precio por acción ---")
    for original, ticker, precio in top_100:
        print(f"{ticker:8s}  {precio:10.2f}  ({original})")

    watchlist_python = [t for _, t, _ in top_100]
    print("\n--- Lista lista para pegar en watchlist_screener.py ---")
    print("watchlist = ", watchlist_python)


if __name__ == "__main__":
    main()