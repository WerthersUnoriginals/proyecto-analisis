# Validación integral del módulo C sobre el S&P 500

Fecha: 2026-09-01

## Ejecución

- Script: `test_c_score_full_sp500.py`
- GitHub Actions run: `33464552822`
- Job: `99721601740`
- Resultado del workflow: **SUCCESS**
- Duración del análisis: **14.09 min**
- Universo detectado: **503 tickers** (el S&P 500 puede tener más de 500 tickers por clases de acciones)
- Analizadas con éxito: **503/503**
- Fallos de ejecución: **0**
- Scores válidos: **502/503**

## Distribución del C Score v1.1

- Media: **41.97**
- Mediana: **38.22**
- P10 / P25 / P75 / P90: **13.50 / 22.65 / 61.31 / 76.59**
- POOR: **310**
- WEAK: **61**
- ACCEPTABLE: **57**
- STRONG: **42**
- VERY_STRONG: **21**
- EXCEPTIONAL: **11**
- N/D: **1**

Tramos:

- 0–19.99: **109**
- 20–39.99: **154**
- 40–49.99: **47**
- 50–59.99: **61**
- 60–69.99: **57**
- 70–79.99: **42**
- 80–89.99: **21**
- 90–100: **11**

## Usabilidad e integridad

- `C_SCORE_USABLE`: **315**
- `C_SCORE_REVIEW`: **188**
- `OK`: **374**
- `REVIEW_REQUIRED_DATA`: **92**
- `PARTIAL_SCORE`: **37**

Integridad:

- VERIFIED: **291**
- REVIEW_REQUIRED: **92**
- VERIFIED_WITH_PARTIAL_CORE_DATA: **83**
- VERIFIED_WITH_ACCOUNTING_DIFFERENCE: **23**
- VERIFIED_WITH_ACCOUNTING_DIFFERENCE_AND_PARTIAL_CORE_DATA: **10**
- Otros fallbacks de shares: **4**

Puntos disponibles:

- 95: **380**
- 85: **78**
- 75: **22**
- 65: **15**
- 55: **6**
- 20: **1**
- 0: **1**

## Flags

- SPLIT_VERIFIED: **46**
- ACCOUNTING_DIFFERENCE: **34**
- BASE_EFFECT_RISK: **31**
- LOSS_TO_PROFIT: **20**
- SMALL_BASE_RISK: **4**

## Top del ranking

1. WDC — 98.63 — EXCEPTIONAL — REVIEW
2. AMD — 97.54 — EXCEPTIONAL — USABLE
3. MU — 97.54 — EXCEPTIONAL — USABLE
4. MPWR — 95.57 — EXCEPTIONAL — USABLE
5. STX — 95.08 — EXCEPTIONAL — USABLE
6. AVGO — 94.15 — EXCEPTIONAL — USABLE
7. ADI — 93.61 — EXCEPTIONAL — USABLE
8. GS — 90.64 — EXCEPTIONAL — REVIEW
9. AMZN — 90.50 — EXCEPTIONAL — USABLE
10. GOOG / GOOGL — 90.23 — EXCEPTIONAL — REVIEW

## Chequeos de sanidad

- Score >=70: **74**
- Score >=80: **32**
- Score >=90: **11**
- Score >=70 y USABLE: **47**
- Score >=70 y REVIEW: **27**
- Score >=70 con persistencia <=3: **17**
- REVIEW total: **188**
- Partial / available_points <80: **45**
- BASE_EFFECT_RISK o SMALL_BASE_RISK: **35**

## Lectura técnica

La infraestructura ha pasado una prueba de escala completa: 503/503 tickers se procesaron sin que el motor se detuviera, incluso con errores puntuales de Yahoo (404/401), gracias a los fallbacks implementados.

La distribución del score es selectiva y no muestra inflación general: más del 61% queda en POOR y solo 11 compañías alcanzan EXCEPTIONAL. La separación entre score y usabilidad también está funcionando: 27 compañías con score >=70 quedan bloqueadas para selección automática por problemas de integridad o completitud.

La prueba sí revela un punto que requiere estudio antes de congelar definitivamente la C: **17 compañías obtienen score >=70 con persistencia <=3**. Algunos casos tienen crecimiento actual muy fuerte y pueden ser legítimos, pero el conjunto demuestra que la muestra previa de 20 compañías no era suficiente para validar esta defensa. Entre los ejemplos aparecen SMCI, SNDK, CDNS, EXPD, CVX, PSX, XOM, ZBRA, SNPS y MRVL.

Conclusión: la arquitectura, la adquisición de datos, los fallbacks, los flags y la separación `USABLE/REVIEW` funcionan de forma robusta. Sin embargo, antes de declarar cerrado el C Score conviene estudiar específicamente los 17 casos de score alto con baja persistencia para decidir si son líderes de crecimiento legítimos o si el peso del trimestre actual sigue siendo demasiado dominante en algunos patrones.
