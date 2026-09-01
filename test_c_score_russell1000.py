"""Stress test C Score v1.2 sobre una aproximación operativa al Russell 1000 vía holdings de IWB."""
from __future__ import annotations
from collections import Counter
import time
import pandas as pd
import requests
from fundamental_c import analyze_current_earnings

IWB_URL = "https://www.ishares.com/us/products/239707/ishares-russell-1000-etf/1467271812596.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund"
OUTPUT_CSV = "russell1000_c_score_v12_results.csv"


def load_tickers() -> list[str]:
    headers={"User-Agent":"Mozilla/5.0 CANSLIMResearch/0.7"}
    r=requests.get(IWB_URL,headers=headers,timeout=60); r.raise_for_status()
    lines=r.text.splitlines()
    start=next(i for i,x in enumerate(lines) if x.startswith("Ticker,"))
    from io import StringIO
    df=pd.read_csv(StringIO("\n".join(lines[start:])))
    tickers=[]
    for x in df.get("Ticker",pd.Series(dtype=str)).dropna():
        t=str(x).strip().upper().replace(".","-")
        if t and t not in {"-","USD"} and t.replace("-","").isalnum(): tickers.append(t)
    return sorted(set(tickers))


def comp(score,name): return score.get("components",{}).get(name,{}).get("points")

def analyze_one(t):
    r=analyze_current_earnings(t); s=r.get("c_score_v1",{}); flags=r.get("c_flags",[]) or []
    return {"ticker":t,"score":s.get("normalized_score"),"class":s.get("class"),"usability":s.get("usability"),"score_status":s.get("status"),"available_points":s.get("available_points"),"eps_yoy":r.get("latest_eps_yoy_pct"),"sales_yoy":r.get("latest_revenue_yoy_pct"),"persistence":comp(s,"persistence"),"trend_quality":comp(s,"eps_trend_quality"),"integrity":r.get("data_integrity"),"flags":",".join(flags) if flags else "-"}

def main():
    tickers=load_tickers(); print(f"RUSSELL 1000 STRESS TEST V1.2 | tickers={len(tickers)}")
    rows=[]; failures=[]; st=time.time()
    for i,t in enumerate(tickers,1):
        try: rows.append(analyze_one(t))
        except Exception as e: failures.append({"ticker":t,"error":repr(e)})
        if i==1 or i%25==0 or i==len(tickers): print(f"Progreso {i}/{len(tickers)} exito={len(rows)} fallos={len(failures)} min={(time.time()-st)/60:.1f}")
        time.sleep(.10)
    f=pd.DataFrame(rows).sort_values(["score","ticker"],ascending=[False,True],na_position="last"); f.to_csv(OUTPUT_CSV,index=False)
    valid=f.score.dropna(); high=f[f.score>=70]; review=f[f.usability=="C_SCORE_REVIEW"]
    lowp=high[high.persistence.fillna(99)<=2]
    print("\nRESUMEN")
    print("universo",len(tickers),"exito",len(f),"fallos",len(failures),"scores_validos",len(valid))
    if len(valid): print("media",round(valid.mean(),2),"mediana",round(valid.median(),2),"p10",round(valid.quantile(.1),2),"p90",round(valid.quantile(.9),2))
    print("clases",f["class"].value_counts(dropna=False).to_dict())
    print("usabilidad",f.usability.value_counts(dropna=False).to_dict())
    print("integridad",f.integrity.value_counts(dropna=False).to_dict())
    print("score>=70",len(high),">=80",int((f.score>=80).sum()),">=90",int((f.score>=90).sum()))
    print(">=70 usable",len(high[high.usability=="C_SCORE_USABLE"]),">=70 review",len(high[high.usability=="C_SCORE_REVIEW"]))
    print(">=70 persistencia<=2",len(lowp),"review_total",len(review))
    c=Counter();
    for txt in f.flags.fillna("-"):
        if txt!="-":
            for x in str(txt).split(","): c[x]+=1
    print("flags",dict(c.most_common()))
    print("TOP20")
    print(f[["ticker","score","class","usability","persistence","flags","integrity"]].head(20).to_string(index=False))
    print("LOW_PERSISTENCE_HIGH_SCORE")
    print(lowp[["ticker","score","usability","persistence","flags","integrity"]].to_string(index=False))
    if failures: print("FAILURES",failures)
    print("CSV",OUTPUT_CSV)
if __name__=="__main__": main()
