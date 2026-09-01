# Validación C Score v1.2 experimental — S&P 500

Fecha: 2026-09-01

## Objetivo

Validar una defensa de baja persistencia detectada tras la prueba integral de C Score v1.1 sobre 503 tickers del S&P 500.

La v1.2 mantiene intactos los pesos y el score numérico. Solo modifica la política de usabilidad:

- Si `C Score >= 70`
- y `persistence <= 2`
- entonces la señal pasa a `C_SCORE_REVIEW`.

Nuevos diagnósticos:

- `LOW_PERSISTENCE_HIGH_SCORE`
- `LOW_PERSISTENCE_BASE_EFFECT` cuando además existe `BASE_EFFECT_RISK`.

## Ejecuciones

### Prueba dirigida sobre los 17 casos de v1.1

- Workflow: `Test C Score v1.2 low persistence cases`
- Run: `33536620840`
- Resultado: **SUCCESS**
- 17/17 casos procesados correctamente.

### Prueba integral S&P 500

- Workflow: `Test C Score v1.2 full S&P 500`
- Run: `33536416847`
- Resultado: **SUCCESS**
- Universo: **503 tickers**
- Scores válidos: **502/503**

## Comparación v1.1 vs v1.2

El score numérico y las clases no cambian:

- Scores modificados: **0**
- Clases modificadas: **0**
- Score >=70: **74** en ambas versiones
- Score >=80: **32** en ambas versiones
- Score >=90: **11** en ambas versiones

Usabilidad:

- v1.1 `C_SCORE_USABLE`: **315**
- v1.2 `C_SCORE_USABLE`: **306**
- v1.1 `C_SCORE_REVIEW`: **188**
- v1.2 `C_SCORE_REVIEW`: **197**

Entre las compañías con score >=70:

- v1.1 USABLE: **47**
- v1.2 USABLE: **38**
- v1.1 REVIEW: **27**
- v1.2 REVIEW: **36**

## Casos afectados

Doce compañías tienen `score >=70` y persistencia <=2. Tres ya estaban en REVIEW por otros motivos y nueve cambian de USABLE a REVIEW:

- SMCI — 80.76
- SNDK — 80.00
- CDNS — 79.91
- CVX — 78.60
- PSX — 78.60
- XOM — 78.18
- ZBRA — 77.14
- SNPS — 71.31
- MRVL — 71.26

Tres casos con persistencia <=2 ya estaban en REVIEW:

- FANG — REVIEW_REQUIRED
- FIS — PARTIAL_CORE_DATA
- COP — REVIEW_REQUIRED

Ocho de los doce casos de baja persistencia coinciden además con `BASE_EFFECT_RISK` y reciben `LOW_PERSISTENCE_BASE_EFFECT`.

## Casos que permanecen utilizables

La defensa no bloquea persistencia 3. Entre los 17 casos originales permanecen USABLE:

- EXPD — 79.28 — persistencia 3
- STT — 73.41 — persistencia 3
- MCO — 72.25 — persistencia 3

CVNA y BRK-B siguen en REVIEW por problemas previos de integridad/completitud y no dependen de esta defensa.

## Conclusión

La v1.2 corrige de forma localizada el hueco observado en v1.1 sin modificar la fortaleza fundamental medida ni alterar el ranking. La separación entre score y confianza de uso automático funciona como se pretendía.

Resultado recomendado: **mantener v1.2 como candidata para la siguiente prueba de estrés (Russell 1000), sin modificar todavía pesos ni umbrales del score numérico**.
