# CAN SLIM Plus — instrucciones del proyecto

Sistema de análisis de acciones que convierte el método CAN SLIM de William O'Neil
en un proceso sistemático, explicable y verificable. Uso informativo y educativo.
Las siete piezas (C, A, N, S, L, I, M) se amplían con calidad de datos,
point-in-time, capa técnica con estados temporales y un Experience Store.

## Estado actual (2026-10-02)

- **Auditoría de C (2026-10-02):** `docs/audits/2026-10-02-c-block-audit.md`.
  El pipeline v2 (`4bb7a81`) tenía defectos reales: mezcla de bases por split,
  Q4 nunca derivado, trimestre desfasado, PIT con conocimiento posterior,
  ingesta no repetible.
- **Fundamentos v3 implementados** (spec
  `docs/superpowers/specs/2026-10-02-fundamentals-v3-design.md`, sin commitear,
  pendiente de revisión): evidencia raw literal, normalización PIT al leer,
  base de splits verificada, Q4 derivado (ventas/BN), contrato C v3 de 11 inputs.
  Migraciones `2026-10-02_evidence_v3*` aplicadas; AAPL, MSFT y NVDA ingeridos.
- **C v2 (`c-v2.6-compatible-v1`)** queda congelado como ruta legacy; sus tests
  y líneas base siguen válidos sólo para v2.
- **A — Annual Earnings: implementado sobre v3** (spec
  `docs/superpowers/specs/2026-10-02-annual-earnings-v3-design.md`; el borrador
  de Codex del 2026-09-28 queda como histórico superado). Contrato
  `a-input-contract-v1` y A Score v1 (`a-1.0-exp`, pesos aprobados).
  Live 2026-10-02: AAPL 43.92 POOR, MSFT 88.31 VERY_STRONG, NVDA 100 EXCEPTIONAL.
- **Validación en 26 empresas** (`docs/audits/2026-10-02-validation-26-companies.md`):
  catálogo v5 (ventas totales, banca, utilities), XBRL del filing cuando la API
  de SEC va con retraso, sucesiones de registrante (XOM), emisores extranjeros
  excluidos explícitamente, splits del proveedor verificados contra SEC.
- **N — New Highs (2026-10-03):** spec
  `docs/superpowers/specs/2026-10-03-new-highs-n-v1-design.md`. Implementado
  offline: `prices_v3.py` (barras Yahoo literales, base = fecha de observación,
  conversión con `split-basis-v1`), `new_highs_v1.py`, `catalysts_v3.py` (8-K
  5.02/2.01, informativos), `n_contract_v1.py`, `n_v3_runner.py`, ingesta
  `yfinance.daily_bars` e items 8-K; N Score v1 (`n_score_v1.py`, `n-1.0-exp`,
  pesos aprobados). Migraciones `2026-10-03_price_bars_v1` y
  `2026-10-03_filing_acceptance_observations_v1` aplicadas; ejecución real en
  26 empresas (`docs/audits/2026-10-03-n-live-26-companies.md`). SEC sirve a
  veces `acceptanceDateTime` desplazado por la zona de Nueva York: se guarda
  cada observación y los catalizadores usan la más tardía (spec N §14).
- **S — Supply and Demand (2026-10-03):** spec
  `docs/superpowers/specs/2026-10-03-supply-demand-s-v1-design.md`. Oferta de
  acciones (diluidas, básicas como alternativa), deuda/patrimonio con 4
  definiciones de deuda sin mezclar, volumen subida/bajada en 50 sesiones;
  splits y tamaño informativos; insiders diferidos. Catálogo SEC v7. Ejecución
  real en `docs/audits/2026-10-03-s-live-26-companies.md`. S Score v1
  (`s-1.0-exp`, pesos aprobados).
- **L — Leader or Laggard (2026-10-03):** spec
  `docs/superpowers/specs/2026-10-03-leader-laggard-l-v1-design.md`. RS Rating
  (percentil 1-99, fórmula IBD 40/20/20/20) frente al S&P 500 (posiciones
  oficiales del SPY, ~500 empresas ingeridas), grupo SIC de 2 dígitos, línea RS
  frente al SPY. Ejecución real en `docs/audits/2026-10-03-l-live-26-companies.md`.
  L Score v1 (`l-1.0-exp`, pesos aprobados).
- **I — Institutional Sponsorship (2026-10-03):** spec
  `docs/superpowers/specs/2026-10-03-institutional-i-v1-design.md`. Fondos 13F
  (conjuntos de datos oficiales de SEC, 5 trimestres) y su tendencia, % de
  propiedad institucional; CUSIP oficial del SPY o por nombre (marcado);
  discontinuidades de CUSIP a revisión. Ejecución real en
  `docs/audits/2026-10-03-i-live-26-companies.md`. I Score v1 (`i-1.0-exp`,
  pesos aprobados; clásico falla si los fondos caen más de un 1 %).
- **M — Market Direction (2026-10-04):** spec
  `docs/superpowers/specs/2026-10-03-market-direction-m-v1-design.md`. SPY y QQQ
  (registro explícito de índices de referencia), días de distribución, días de
  confirmación, máquina de tres estados (regla B: la distribución solo pone
  "bajo presión"; corrección con caída del 8 %), medias de 50/200 sesiones y
  amplitud del S&P 500. M Score v1 (`m-1.0-exp`, pesos aprobados). Ejecución
  real en `docs/audits/2026-10-04-m-live-market.md`.
- **Las siete letras CAN SLIM tienen v1 con datos reales.** Pendiente: capa
  técnica (documento "Score Fase de Entrada v2"), Experience Store y una
  puntuación conjunta.

## Estructura

- `fundamental_c.py` — módulo C legacy (v2.6). Oráculo de comportamiento. **No modificar.**
- `c_score_v1.py` — C Score v1.2 (legacy). **No modificar** (byte a byte).
- `a_score_v1.py` — A Score v1: resultado clásico O'Neil (25%/año, ROE 17%,
  sin años en pérdida) + puntuación graduada 0-100.
- `n_score_v1.py` — N Score v1: resultado clásico (≤15 % bajo el máximo de 52
  semanas) + puntuación graduada 0-100; catalizadores sólo como flags.
- `s_score_v1.py` — S Score v1: acciones, deuda y volumen; resultado clásico
  (distribución, dilución, deuda creciente).
- `l_score_v1.py` — L Score v1: RS Rating, grupo, línea RS; clásico RS ≥ 80.
- `i_score_v1.py` — I Score v1: fondos, tendencia y propiedad; tolerancia 1 %.
- `m_score_v1.py` — M Score v1: estado del mercado, distribución, medias, amplitud.
- `c_score_v13.py` — C Score v1.3, usado por C v3: una pérdida es evidencia
  desfavorable, no dato ausente (spec `2026-10-02-c-score-v1-3-design.md`).
  Mismos pesos y umbrales que v1.2.
- `database/` — capa PostgreSQL:
  - `fundamentals.py`, `sec_import.py`, `yahoo_import.py` — evidencia raw.
  - `normalized_fundamentals.py` — `fundamentals_normalized` append-only por fuente.
  - `effective_fundamentals.py` — política `c-v2.6-compatible-v1`.
  - `c_fundamentals_adapter.py` — contrato de 11 inputs de C. **No modificar.**
  - `c_data_integrity.py`, `split_integrity.py`, `corporate_actions.py`,
    `corporate_action_acquisition.py` — integridad y corporate actions.
  - `c_dual_run*.py`, `c_live_*.py` — comparación dual y diagnóstico live.
  - v3: `sec_facts.py` (catálogo y hechos SEC literales), `split_basis.py`,
    `quarterly_v3.py` (calendario fiscal, Q4, selección, crecimiento),
    `c_contract_v3.py`, `providers_v3.py`, `evidence_v3.py`,
    `ingest_v3.py` (`python -m database.ingest_v3 AAPL NVDA:0001045810`),
    `c_v3_runner.py` (`python -m database.c_v3_runner AAPL [--as-of ISO]`),
    `sec_xbrl_instance.py` (XBRL del propio filing), `registrant_v3.py`
    (emisor extranjero, sucesiones), `batch_v3.py`
    (`python -m database.batch_v3 AAPL MSFT [--ingest] [--file f] [--out r.json]`).
  - A: `annual_v3.py` (años fiscales reales, ROE sobre patrimonio medio, CAGR
    anclado), `a_contract_v1.py`, `a_v3_runner.py`
    (`python -m database.a_v3_runner AAPL`).
  - M: `market_v1.py` (`python -m database.market_v1`: barras de SPY y QQQ),
    `market_direction_v1.py`, `m_contract_v1.py`, `m_v3_runner.py`
    (`python -m database.m_v3_runner`).
  - I: `institutional_v1.py` (`python -m database.institutional_v1`: 13F de SEC
    y resolución de CUSIP), `sponsorship_v1.py`, `i_contract_v1.py`,
    `i_v3_runner.py` (`python -m database.i_v3_runner NVDA`).
  - L: `universe_v1.py` (`python -m database.universe_v1`: posiciones del SPY,
    perfiles SIC y barras del universo), `relative_strength_v1.py`,
    `l_contract_v1.py`, `l_v3_runner.py` (`python -m database.l_v3_runner NVDA`).
  - S: `supply_demand_v1.py`, `s_contract_v1.py`, `s_v3_runner.py`
    (`python -m database.s_v3_runner AAPL`).
  - N: `prices_v3.py`, `new_highs_v1.py`, `catalysts_v3.py`, `n_contract_v1.py`,
    `n_v3_runner.py` (`python -m database.n_v3_runner NVDA`); `batch_v3` sin
    `--as-of` evalúa tras la ingesta.
  - `migrations/` — migraciones versionadas `YYYY-MM-DD_nombre_vN.sql`. Nunca editar una existente.
- `fixtures/` — líneas base congeladas (AAPL) y Company Facts reales recortados
  (AAPL, NVDA, 2026-10-02) para tests offline de v3.
- `test_*.py` en la raíz — tests `unittest`.
- `docs/superpowers/specs|plans/` — specs y planes (flujo superpowers).
- `watchlist_screener.py`, `seleccion.py`, `afinar.py`, `prueba.py`,
  `html pantallas/` — prototipos iniciales, fuera del núcleo.

## Tests

Batería offline (sin red ni PostgreSQL), ~759 tests en segundos:

```bash
python -m unittest test_backfill_normalized test_backfill_semantics_v2 test_c_data_integrity test_c_dual_run test_c_dual_run_contract test_c_fundamentals_adapter test_c_independent_contract test_c_independent_postgres_integration test_c_live_diagnostic test_c_live_runner test_c_normalized_row_shape test_corporate_action_acquisition test_corporate_action_migration test_corporate_action_postgres_concurrency test_corporate_action_postgres_concurrency_harness test_corporate_action_postgres_repository_integration test_corporate_action_repository test_corporate_actions test_effective_fundamentals test_fundamental_c_snapshot test_fundamentals_effective_schema test_fundamentals_semantics_v2_migration test_normalized_fundamentals test_split_integrity test_validate_fundamentals test_yahoo_import test_sec_facts test_split_basis test_quarterly_v3 test_c_contract_v3 test_providers_v3 test_c_score_v13 test_annual_v3 test_a_contract_v1 test_a_score_v1 test_registrant_v3 test_sec_xbrl_instance test_batch_v3 test_prices_v3 test_new_highs_v1 test_catalysts_v3 test_n_contract_v1 test_n_score_v1 test_supply_demand_v1 test_s_contract_v1 test_relative_strength_v1 test_l_contract_v1 test_sponsorship_v1 test_i_contract_v1 test_market_direction_v1 test_m_contract_v1
```

- Integración PostgreSQL: opt-in con `CANSLIM_RUN_PG_INTEGRATION=1`
  (v3: `test_evidence_v3_postgres_integration`, transacción revertida, sin rastro). Usa la BD real
  `canslim` (usuario `canslim_app`, PostgreSQL 17) con preflight de identidad y
  UIDs de prueba reservados. Ejecutar sólo con autorización explícita.
- `test_c_score_*`, `test_sec_history`, `test_splits`, `test_yahoo_historico` son
  scripts de validación con red (SEC/Yahoo), no tests unitarios. No ejecutarlos sin pedirlo.
- Línea base de C v2 (legacy congelado): `sec_effective=75`,
  `hybrid_effective=60`, contrato exacto de 11 inputs, vista
  `fundamentals_effective_current` de 37 columnas.
- Línea base de C v3: AAPL a 2026-09-03 reproduce 62.69 (test offline); casos
  NVDA de split y Q4 de AAPL fijados con datos reales en `test_quarterly_v3`.
- Credenciales en `.env` (cargado por `database/db.py`). No leer ni mostrar su contenido.

## Método de trabajo (heredado de Codex)

1. Spec (diseño, decisiones cerradas/diferidas, invariantes, criterios de aceptación)
   → revisión humana → plan → implementación TDD (tests en rojo primero).
2. Cada tarea termina con tests verdes y un commit lógico.
3. **No hacer commit sin revisión del usuario. No hacer push sin autorización explícita.**
4. No llamar a proveedores (SEC/Yahoo), no aplicar migraciones/DDL y no escribir
   en PostgreSQL salvo autorización explícita en la tarea.
5. Trabajar en `main`.
6. Antes de entregar: tests offline completos, `git diff --check` y revisión de alcance.

### Autonomía de decisión (2026-10-03)

Decidir con criterio propio y contarlo en el resumen final, sin preguntar antes:
detalles técnicos de una spec (umbrales de sesiones, ventanas, códigos de
estado, nombres de módulos y columnas); lecturas de SEC/Yahoo, aplicar
migraciones nuevas y escribir en PostgreSQL dentro de la tarea en curso (D4);
ajustes menores que salgan con datos reales, si respetan los principios de datos
y quedan anotados en la spec; y pasar de spec a plan e implementación TDD sin
parar, cuando la spec no tenga decisiones de producto abiertas.

Seguir preguntando: commit y push; pesos y umbrales de puntuación; cualquier
excepción a los principios de datos (PIT, SEC > Yahoo, fail-closed); y
operaciones destructivas o irreversibles en la base de datos.

## Convenciones

- Mensajes de commit: español, una línea, imperativo ("Añadir…", "Corregir…", "Cerrar…").
- Código e identificadores en inglés. Docstrings y specs: inglés desde 2026-09-20
  (los módulos anteriores están en español; respetar el idioma del archivo que se edita).
- `from __future__ import annotations`, dataclasses `frozen=True`, `Decimal` para
  valores financieros, datetimes con zona horaria UTC.
- Lógica pura separada de la persistencia; el driver psycopg se importa de forma perezosa.
- Constantes de versión explícitas (`*_VERSION = "nombre-vN"`). Nunca reutilizar
  un nombre de versión tras cambiar sus reglas.

## Principios de datos (no negociables)

- Raw y normalizado son append-only; una corrección = nueva evidencia o nueva versión.
- Point-in-time: filtrar `observed_at <= as_of` **antes** de resolver identidad,
  amendments o prioridades. Nunca filtrar el ganador actual a posteriori.
- `source_available_at` y `observed_at` nunca se confunden ni se infieren.
- SEC > Yahoo; Yahoo nunca desplaza SEC en silencio. YoY/CAGR nunca mezclan fuentes.
- Fail-closed: lo ambiguo no se selecciona automáticamente; queda auditable.
- Dato ausente ≠ dato desfavorable ≠ histórico insuficiente ≠ contradicción.
- Calidad intrínseca, comparación entre fuentes, estado de cálculo e integridad
  agregada son dominios separados.
- Una puntuación mide cumplimiento de criterios, no probabilidad de subida.
- El `fy` de SEC Company Facts es el foco del filing y no es fiable entre años:
  la identidad fiscal sale de fechas reales de cierre (ver spec v3 §5/§12).
- YoY dentro de una fuente **y** un mismo concepto (tag); valores por acción en
  la base de splits vigente en `as_of`.
