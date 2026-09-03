# Fundamentals Effective Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Construir una capa PostgreSQL auditable de fundamentales normalizados y efectivos que reproduzca exactamente el contrato y el C Score actual de AAPL sin modificar `c_score_v1.py`.

**Architecture:** Mantener `fundamentals_raw` y `fundamentals_quarterly` como evidencia y camino SEC legacy, añadir `fundamentals_normalized` append-only por fuente y seleccionar con la política versionada `c-v2.6-compatible-v1`. Un adaptador Python convertirá la selección efectiva en el mismo report que hoy consume `build_c_score()`, con ejecución dual antes de cualquier cambio de fuente principal.

**Tech Stack:** Python 3, `unittest`, pandas, requests, yfinance, psycopg 3, PostgreSQL 17, SQL y JSONB.

**Spec:** `docs/superpowers/specs/2026-09-03-fundamentals-effective-design.md`

## Global Constraints

- No modificar `c_score_v1.py` ni sus fórmulas, pesos o clasificación.
- Mantener `fundamentals_quarterly` y `normalize_sec_quarterly()` con su semántica SEC actual.
- La única política inicial es `c-v2.6-compatible-v1`; no crear una política conservadora.
- No calcular ni seleccionar observaciones `DERIVED`; no derivar Q4.
- Conservar SEC, Yahoo y futuras fuentes como observaciones independientes y append-only.
- Usar `observed_at <= as_of` como filtro point-in-time predeterminado, antes de resolver amendments o prioridades.
- No inventar `source_available_at`; Yahoo lo conserva nulo sin evidencia fiable.
- Mantener separados `intrinsic_quality_*` y `comparison_*`.
- Una discrepancia sólo numérica no cambia automáticamente elegibilidad, prioridad SEC ni C Score.
- El 5,00 % pertenece a `REVIEW_REQUIRED`; `REVIEW_REQUIRED_HIGH` empieza estrictamente por encima de 5 %.
- Preservar `source_period_end` y usarlo como `series_date` bajo la política compatible.
- Calcular YoY dentro de una misma fuente; nunca mezclar numerador SEC con denominador Yahoo.
- No hacer push durante la ejecución del plan salvo autorización humana posterior.
- Cada tarea termina con tests verdes, working tree limpio y un commit lógico.

---

## Mapa de archivos

### Archivos nuevos

| Ruta | Responsabilidad | Tests |
|---|---|---|
| `database/schema_fundamentals_effective.sql` | Crear `fundamentals_normalized`, restricciones, índices y `fundamentals_effective_current`. | `test_fundamentals_effective_schema.py` |
| `database/normalized_fundamentals.py` | Tipos, validación intrínseca, normalización SEC/Yahoo y persistencia append-only. | `test_normalized_fundamentals.py` |
| `database/yahoo_import.py` | Extraer las dos variantes Yahoo y producir hechos raw auditables e idempotentes. | `test_yahoo_import.py` |
| `database/effective_fundamentals.py` | Comparación SEC–Yahoo, política compatible, consultas current/as-of y cálculo intermedio de series. | `test_effective_fundamentals.py`, `test_fundamentals_point_in_time.py` |
| `database/c_fundamentals_adapter.py` | Construir desde PostgreSQL el contrato exacto anterior a `build_c_score()`. | `test_c_fundamentals_adapter.py` |
| `database/backfill_normalized.py` | CLI transaccional para normalizar raw existentes sin tocar la tabla legacy. | `test_backfill_normalized.py` |
| `fixtures/aapl_c_v26_baseline.json` | Línea base congelada de fechas, fuentes, nulos, report relevante y score AAPL. | `test_c_fundamentals_adapter.py` |
| `test_fundamentals_effective_schema.py` | Constraints e índices del esquema nuevo. | — |
| `test_normalized_fundamentals.py` | Normalización, variantes, calidad intrínseca e idempotencia. | — |
| `test_backfill_normalized.py` | Dry-run, transacción y resumen del backfill SEC. | — |
| `test_yahoo_import.py` | Extracción, hashes, variantes y persistencia raw Yahoo. | — |
| `test_effective_fundamentals.py` | Comparación, selección, merge, YoY y aceleración. | — |
| `test_fundamentals_point_in_time.py` | Filtro as-of anterior a amendments y prioridad. | — |
| `test_c_fundamentals_adapter.py` | Contrato C, ejecución dual y gates AAPL. | — |

### Archivos existentes que se modifican

| Ruta | Cambio acotado | Dependencia |
|---|---|---|
| `database/fundamentals.py` | Exponer lectura raw reutilizable y aceptar `source_record_id` Yahoo estable sin cambiar el normalizador legacy. | Tareas 3–6 |
| `database/sec_import.py` | Añadir `source_variant='sec.company_facts'` al contrato que consume el nuevo normalizador; no cambiar extracción SEC. | Tarea 3 |
| `database/validate_fundamentals.py` | Añadir comparación dual y salida machine-readable reutilizando tolerancias existentes. | Tareas 1 y 12 |
| `test_validate_fundamentals.py` | Añadir únicamente regresiones del modo dual y de la salida estructurada. | Tareas 1 y 12 |

### Archivos que no se modifican

- `fundamental_c.py`: sigue siendo oráculo de comportamiento durante ejecución dual.
- `c_score_v1.py`: permanece byte-for-byte sin cambios.
- `database/schema_fundamentals.sql`: conserva el esquema legacy.
- Tests de C Score existentes: se ejecutan como regresión, no se reescriben.

### Dependencias

```text
Task 1 baseline
   └─> Task 2 schema
        └─> Task 3 DAO + SEC normalization
             └─> Task 4 SEC backfill/equivalence
                  └─> Task 5 Yahoo raw
                       └─> Task 6 Yahoo normalization/identity
                            └─> Task 7 comparisons
                                 └─> Task 8 effective policy
                                      └─> Task 9 current projection
                                           └─> Task 10 C adapter
                                                └─> Task 11 dual equivalence
                                                     └─> Task 12 point-in-time
                                                          └─> Task 13 acceptance gate
```

## Interfaces comunes

Definir en `database/normalized_fundamentals.py`:

```python
SEC_VARIANT = "sec.company_facts"
YAHOO_TS_VARIANT = "yahoo.fundamentals_timeseries"
YFINANCE_INCOME_VARIANT = "yfinance.quarterly_income_stmt"
SEC_NORMALIZER_VERSION = "sec-normalized-v1"
YAHOO_NORMALIZER_VERSION = "yahoo-normalized-v1"

@dataclass(frozen=True)
class NormalizedObservation:
    company_id: int
    metric: str
    source: str
    source_variant: str
    observation_kind: str
    value: Decimal
    unit: str
    currency: str | None
    source_period_start: date | None
    source_period_end: date
    canonical_period_end: date | None
    series_date: date
    fiscal_year: int | None
    fiscal_quarter: int | None
    filed_date: date | None
    source_available_at: datetime | None
    observed_at: datetime
    raw_id: int
    normalizer_version: str
    intrinsic_quality_status: str
    intrinsic_quality_reasons: tuple[str, ...]
    selection_eligibility: str
    alignment_method: str
    alignment_days: int | None
    alignment_reference_id: int | None
```

Definir en `database/effective_fundamentals.py`:

```python
SELECTION_POLICY_VERSION = "c-v2.6-compatible-v1"
COMPARISON_RULES_VERSION = "sec-yahoo-comparison-v1"

@dataclass(frozen=True)
class ComparisonDiagnostic:
    comparison_status: str
    comparison_reasons: tuple[str, ...]
    comparison_reference_id: int | None
    comparison_diff_pct: float | None
    comparison_rules_version: str

@dataclass(frozen=True)
class EffectiveObservation:
    observation: NormalizedObservation
    selection_policy_version: str
    selection_reason: str
    comparison: ComparisonDiagnostic
```

---

### Task 1: Congelar contrato y regresión AAPL

**Stage:** Etapa 0

**Files:**
- Create: `fixtures/aapl_c_v26_baseline.json`
- Modify: `database/validate_fundamentals.py:601-667`
- Modify: `test_validate_fundamentals.py`

**Interfaces:**
- Consumes: `run_validation(ticker: str)` y `analyze_current_earnings(ticker: str)`.
- Produces: `build_validation_result(ticker: str) -> dict` y fixture JSON con `sec`, `hybrid`, `q4`, `inputs`, `report`, `c_score`.

- [ ] **Step 1: Escribir tests fallidos de salida estructurada**

```python
class StructuredBaselineTests(unittest.TestCase):
    def test_result_exposes_all_regression_gates(self):
        result = build_validation_result("AAPL", code_data=fake_code(), postgres=fake_pg())
        self.assertEqual(set(result), {"sec", "hybrid", "q4", "inputs", "report", "c_score"})

    def test_fixture_names_the_three_yahoo_q4_fallbacks(self):
        fixture = json.loads(Path("fixtures/aapl_c_v26_baseline.json").read_text())
        self.assertEqual(
            {(row["metric"], row["period"]) for row in fixture["yahoo_fallbacks"]},
            {("EPS_DILUTED", "2025-09-30"), ("REVENUE", "2025-09-30"),
             ("NET_INCOME", "2025-09-30")},
        )
```

- [ ] **Step 2: Ejecutar los tests y confirmar el fallo**

Run: `python -m unittest test_validate_fundamentals.py -v`

Expected: FAIL por ausencia de `build_validation_result` y del fixture.

- [ ] **Step 3: Extraer la construcción de resultados sin cambiar cálculos**

```python
def build_validation_result(ticker, code_data=None, postgres=None):
    code_data = code_data or load_code_series(ticker)
    postgres = postgres or load_postgres_records(ticker)
    # Reutilizar compare_sec_records, compare_hybrid_records,
    # q4_coverage y c_score_inputs sin alterar sus tolerancias.
    return {"sec": sec_rows, "hybrid": hybrid_rows, "q4": q4_rows,
            "inputs": inputs, "report": report_subset,
            "c_score": code_data["report"]["c_score_v1"]}
```

Capturar una vez AAPL con las fuentes actuales y guardar números como JSON, sin credenciales ni payloads completos. Incluir orden, fechas, fuente, nulos relevantes, las 75 observaciones SEC efectivas por métrica-periodo tras aplicar las reglas compatibles de resolución/deduplicación de filings, 60 híbridas, tres fallbacks y 10 grupos de inputs.

- [ ] **Step 4: Ejecutar regresiones**

Run: `python -m unittest test_validate_fundamentals.py -v`

Expected: los 14 tests de `test_validate_fundamentals.py`, todos `OK`; fixture con `75/75` observaciones SEC efectivas por métrica-periodo, `57/60`, tres Yahoo y `10/10`.

- [ ] **Step 5: Confirmar archivos protegidos y commit**

Run: `git diff --exit-code -- fundamental_c.py c_score_v1.py database/fundamentals.py`

Expected: sin salida.

```bash
git add fixtures/aapl_c_v26_baseline.json database/validate_fundamentals.py test_validate_fundamentals.py
git commit -m "Congelar contrato fundamental y regresión AAPL"
```

---

### Task 2: Crear el esquema normalizado append-only

**Stage:** Etapa 1A

**Files:**
- Create: `database/schema_fundamentals_effective.sql`
- Create: `test_fundamentals_effective_schema.py`

**Interfaces:**
- Consumes: `companies(id)` y `fundamentals_raw(id)`.
- Produces: tabla `fundamentals_normalized` y sus constraints/índices; la vista se reserva para Task 9.

- [ ] **Step 1: Escribir tests fallidos del DDL**

```python
class EffectiveSchemaTests(unittest.TestCase):
    def test_schema_declares_every_required_column(self):
        sql = Path("database/schema_fundamentals_effective.sql").read_text()
        for name in REQUIRED_COLUMNS:
            self.assertRegex(sql, rf"\b{name}\b")

    def test_unique_key_is_raw_and_normalizer_version(self):
        self.assertIn("UNIQUE (raw_id, normalizer_version)", compact_sql())

    def test_no_derived_row_can_claim_reported(self):
        self.assertIn("fundamentals_normalized_kind_check", schema_sql())
```

`REQUIRED_COLUMNS` debe enumerar exactamente los 27 campos de la spec, incluido `source_variant` y los campos `intrinsic_quality_*`; no incluir `comparison_*`.

- [ ] **Step 2: Ejecutar y confirmar fallo por archivo ausente**

Run: `python -m unittest test_fundamentals_effective_schema.py -v`

Expected: ERROR `FileNotFoundError`.

- [ ] **Step 3: Escribir DDL mínimo**

Crear `fundamentals_normalized` con tipos de la spec, FK `ON DELETE RESTRICT`, JSONB con default `'[]'::jsonb`, checks de métrica, trimestre, periodo, familia/variante, `series_date = source_period_end`, kind/fuente, alineación y elegibilidad. Crear:

```sql
CONSTRAINT fundamentals_normalized_raw_version_unique
    UNIQUE (raw_id, normalizer_version);

CREATE INDEX fundamentals_normalized_source_period_idx
    ON fundamentals_normalized
       (company_id, metric, source, source_variant, source_period_end);
CREATE INDEX fundamentals_normalized_fiscal_idx
    ON fundamentals_normalized
       (company_id, metric, fiscal_year, fiscal_quarter);
CREATE INDEX fundamentals_normalized_observed_idx
    ON fundamentals_normalized (company_id, metric, observed_at);
```

No crear tabla o proceso `DERIVED`; el check sólo reserva el dominio.

- [ ] **Step 4: Probar DDL y aplicarlo de forma controlada**

Run: `python -m unittest test_fundamentals_effective_schema.py -v`

Expected: todos `OK`.

Después de copia de seguridad y aprobación operativa, aplicar una vez:

```powershell
python -c "from pathlib import Path; from database.db import get_connection; sql=Path('database/schema_fundamentals_effective.sql').read_text(encoding='utf-8'); c=get_connection(); c.execute(sql); c.commit()"
```

Expected: tabla vacía; conteos legacy AAPL siguen siendo raw `135` y quarterly `19`.

- [ ] **Step 5: Commit**

```bash
git add database/schema_fundamentals_effective.sql test_fundamentals_effective_schema.py
git commit -m "Añadir esquema append-only de fundamentales normalizados"
```

---

### Task 3: Persistencia normalizada y normalizador SEC

**Stage:** Etapa 1A

**Files:**
- Create: `database/normalized_fundamentals.py`
- Create: `test_normalized_fundamentals.py`
- Modify: `database/fundamentals.py:49-64`
- Modify: `database/sec_import.py:139-158`

**Interfaces:**
- Consumes: filas dict de `fundamentals_raw` y `insert_raw_fundamentals_batch()`.
- Produces: `normalize_sec_raw_row(row: dict, fiscal_identity: dict) -> NormalizedObservation`, `insert_normalized_batch(observations: Iterable[NormalizedObservation]) -> list[int]`, `load_raw_fundamentals(company_id: int, source: str | None = None) -> list[dict]`.

- [ ] **Step 1: Escribir tests fallidos de tipos, SEC e idempotencia**

```python
class SecNormalizedTests(unittest.TestCase):
    def test_sec_preserves_raw_dates_and_variant(self):
        item = normalize_sec_raw_row(sec_row(), fiscal_identity={"fiscal_year": 2025, "fiscal_quarter": 3})
        self.assertEqual(item.source_variant, "sec.company_facts")
        self.assertEqual(item.series_date, item.source_period_end)
        self.assertEqual(item.observed_at, sec_row()["fetched_at"])
        self.assertEqual(item.intrinsic_quality_status, "OK")

    def test_repeated_raw_and_version_return_same_id(self):
        self.assertEqual(insert_twice_in_rollback_fixture(), [101, 101])
```

- [ ] **Step 2: Ejecutar y confirmar fallo de importación**

Run: `python -m unittest test_normalized_fundamentals.py -v`

Expected: ERROR porque `database.normalized_fundamentals` no existe.

- [ ] **Step 3: Implementar dataclass, validación e INSERT idempotente**

Usar:

```sql
INSERT INTO fundamentals_normalized (...)
VALUES (...)
ON CONFLICT (raw_id, normalizer_version) DO NOTHING
RETURNING id;
```

Si no retorna fila, consultar por `(raw_id, normalizer_version)` y verificar que `source_variant` y todos los campos semánticos coinciden; si difieren, lanzar `RuntimeError("Conflicto semántico...")`.

`load_raw_fundamentals` debe ordenar por `period_end, metric, filed_date, id` y no modificar `ALL_SEC_RAW_SQL` ni `normalize_sec_quarterly()`.

- [ ] **Step 4: Implementar identidad fiscal SEC compatible**

Reutilizar la semántica de `_original_fiscal_metadata`: valor de cada raw independiente, identidad fiscal del filing original para el mismo `period_end`, Q4 sólo para hecho 70–110 días procedente de `10-K`/`10-K/A`. No restar periodos anuales.

Run: `python -m unittest test_normalized_fundamentals.py test_validate_fundamentals.py -v`

Expected: todos `OK`.

- [ ] **Step 5: Commit**

```bash
git add database/normalized_fundamentals.py database/fundamentals.py database/sec_import.py test_normalized_fundamentals.py
git commit -m "Normalizar observaciones SEC por fuente"
```

---

### Task 4: Backfill SEC y gate 75/75

**Stage:** Etapa 1B

**Files:**
- Create: `database/backfill_normalized.py`
- Create: `test_backfill_normalized.py`
- Modify: `database/validate_fundamentals.py`

**Interfaces:**
- Consumes: `load_raw_fundamentals`, `normalize_sec_raw_row`, `insert_normalized_batch`.
- Produces: `backfill_company(company_id: int, source: str, dry_run: bool = True) -> dict` con `raw_read`, `normalized_candidates`, `inserted`, `existing`, `review`, `rejected`.

- [ ] **Step 1: Escribir tests fallidos de dry-run y transacción**

```python
class BackfillTests(unittest.TestCase):
    def test_dry_run_never_calls_insert(self):
        summary = backfill_company(1, "SEC", dry_run=True)
        self.assertEqual(summary["raw_read"], 135)
        insert_mock.assert_not_called()

    def test_failure_rolls_back_whole_batch(self):
        with self.assertRaises(RuntimeError):
            run_failing_transaction_fixture()
        self.assertEqual(count_rows_in_fixture(), 0)
```

- [ ] **Step 2: Ejecutar y confirmar fallo**

Run: `python -m unittest test_backfill_normalized.py -v`

Expected: ERROR por funciones ausentes.

- [ ] **Step 3: Implementar CLI y resumen determinista**

CLI exacta:

```text
python database/backfill_normalized.py AAPL --source SEC --dry-run
python database/backfill_normalized.py AAPL --source SEC --apply
```

`--apply` debe usar una transacción, rechazar fuentes distintas de `SEC` en esta tarea y terminar con commit sólo si todas las filas son procesables.

- [ ] **Step 4: Ejecutar gate SEC**

Run:

```powershell
python database/backfill_normalized.py AAPL --source SEC --dry-run
python database/backfill_normalized.py AAPL --source SEC --apply
python database/validate_fundamentals.py AAPL
```

Expected: raw SEC `135`; `fundamentals_normalized` conserva de forma append-only todas las observaciones válidas de filings y revisiones, por lo que su cardinalidad puede ser superior a `75` y el backfill no debe colapsarla artificialmente. Tras aplicar las reglas compatibles de resolución/deduplicación de filings, la proyección SEC efectiva reproduce `75/75` observaciones por métrica-periodo: EPS `19`, Revenue `19`, Net Income `19` y Diluted Shares `18`; quarterly `19`; ninguna fila `DERIVED`.

- [ ] **Step 5: Commit**

```bash
git add database/backfill_normalized.py database/validate_fundamentals.py test_backfill_normalized.py
git commit -m "Añadir backfill SEC normalizado y equivalencia AAPL"
```

---

### Task 5: Persistir Yahoo raw por variante

**Stage:** Etapa 2A

**Files:**
- Create: `database/yahoo_import.py`
- Create: `test_yahoo_import.py`
- Modify: `database/fundamentals.py:187-242`

**Interfaces:**
- Consumes: `_fetch_yahoo_timeseries`, yfinance `quarterly_income_stmt`, `insert_raw_fundamentals_batch`.
- Produces: `extract_yahoo_raw_facts(ticker: str, company_id: int, observed_at: datetime) -> list[dict]`, `canonical_source_record_id(source_variant: str, provider_id: str, semantic_payload: dict) -> str`, `import_yahoo_fundamentals(...) -> dict`.

- [ ] **Step 1: Escribir tests fallidos de variantes y hash**

```python
class YahooImportTests(unittest.TestCase):
    def test_variants_are_explicit(self):
        rows = extract_yahoo_raw_facts("AAPL", 1, OBSERVED, clients=fakes())
        self.assertEqual({r["source_variant"] for r in rows},
                         {"yahoo.fundamentals_timeseries", "yfinance.quarterly_income_stmt"})

    def test_record_id_is_stable_but_changes_with_value(self):
        self.assertEqual(record_id(value=1), record_id(value=1))
        self.assertNotEqual(record_id(value=1), record_id(value=2))
```

- [ ] **Step 2: Ejecutar y confirmar fallo**

Run: `python -m unittest test_yahoo_import.py -v`

Expected: ERROR por módulo ausente.

- [ ] **Step 3: Implementar extracción sin red en tests**

Mapear exactamente:

```python
YAHOO_TYPES = {
    "quarterlyDilutedEPS": ("EPS_DILUTED", "USD/shares"),
    "quarterlyTotalRevenue": ("REVENUE", "USD"),
    "quarterlyNetIncome": ("NET_INCOME", "USD"),
}
```

Para yfinance usar aliases actuales de `fundamental_c.py`. Guardar `source='YAHOO'`, fecha contable original, `source_available_at=None`, payload, variante explícita en el contrato y `source_record_id` con SHA-256 canónico. No importar diluted shares Yahoo.

- [ ] **Step 4: Probar idempotencia raw**

Run: `python -m unittest test_yahoo_import.py test_validate_fundamentals.py -v`

Expected: todos `OK`; dos importaciones del mismo fixture devuelven los mismos raw IDs y una corrección de valor crea otra evidencia.

- [ ] **Step 5: Commit**

```bash
git add database/yahoo_import.py database/fundamentals.py test_yahoo_import.py
git commit -m "Persistir fundamentales Yahoo por variante auditable"
```

---

### Task 6: Normalizar Yahoo e identificar periodos fiscales

**Stage:** Etapa 2B

**Files:**
- Modify: `database/normalized_fundamentals.py`
- Modify: `test_normalized_fundamentals.py`

**Interfaces:**
- Consumes: raw Yahoo, calendario SEC normalizado y `source_variant` explícito.
- Produces: `normalize_yahoo_raw_row(row: dict, source_variant: str, sec_calendar: Sequence[NormalizedObservation]) -> NormalizedObservation` y `resolve_yahoo_fiscal_identity(...) -> FiscalIdentityResult`.

- [ ] **Step 1: Escribir tests fallidos de identidad y fechas**

```python
def test_unambiguous_yahoo_q4_keeps_original_date():
    item = normalize_yahoo_raw_row(yahoo_row("2025-09-30"), YAHOO_TS_VARIANT, apple_calendar())
    self.assertEqual((item.fiscal_year, item.fiscal_quarter), (2025, 4))
    self.assertEqual(item.source_period_end, date(2025, 9, 30))
    self.assertEqual(item.series_date, date(2025, 9, 30))

def test_ambiguous_identity_is_preserved_but_ineligible():
    item = normalize_yahoo_raw_row(yahoo_row("2025-08-15"), YAHOO_TS_VARIANT, ambiguous_calendar())
    self.assertIsNone(item.canonical_period_end)
    self.assertEqual(item.selection_eligibility, "INELIGIBLE")
    self.assertEqual(item.intrinsic_quality_status, "REVIEW_REQUIRED")

def test_single_sec_within_35_days_is_not_fiscal_identity_evidence():
    item = normalize_yahoo_raw_row(
        yahoo_row_without_fiscal_identity("2025-08-15"),
        YAHOO_TS_VARIANT,
        calendar_with_single_sec_reference("2025-09-19"),
    )
    self.assertEqual((item.fiscal_year, item.fiscal_quarter), (None, None))
    self.assertIsNone(item.canonical_period_end)
    self.assertEqual(item.alignment_method, "UNRESOLVED")
    self.assertEqual(item.selection_eligibility, "INELIGIBLE")
    self.assertEqual(item.intrinsic_quality_status, "REVIEW_REQUIRED")
```

- [ ] **Step 2: Ejecutar y confirmar fallos**

Run: `python -m unittest test_normalized_fundamentals.py -v`

Expected: FAIL por funciones Yahoo ausentes.

- [ ] **Step 3: Implementar resolución de identidad**

Resolver por separado dos resultados:

- **Identidad fiscal:** `fiscal_year`, `fiscal_quarter` y `canonical_period_end` sólo se asignan cuando resultan inequívocos del conjunto de evidencias permitido por la spec, como metadatos fiables, calendario fiscal conocido y contexto SEC anterior/posterior coherente.
- **Alineación temporal:** `alignment_method`, `alignment_days` y `alignment_reference_id` describen la relación con evidencia SEC. Una referencia SEC dentro de `±35` días sólo puede apoyar la alineación cuando la identidad fiscal ya es inequívoca por evidencia suficiente; entonces puede registrarse `NEAREST_35D` y su referencia.

La proximidad temporal, incluso ante un único SEC dentro de `±35` días, no asigna FY/FQ/canonical ni resuelve por sí sola una identidad ambigua. Si sólo existe esa proximidad, o hay empate o contradicción, conservar FY/FQ/canonical nulos cuando corresponda, `alignment_method='UNRESOLVED'`, revisión intrínseca y `selection_eligibility='INELIGIBLE'`. No usar `asOfDate` como publicación.

- [ ] **Step 4: Ejecutar tests de límites**

Añadir casos 35/36 días con identidad fiscal ya resuelta por evidencia independiente, un único SEC dentro de 35 días sin evidencia suficiente de identidad, empate, calendario irregular y `source_available_at is None`.

Run: `python -m unittest test_normalized_fundamentals.py -v`

Expected: todos `OK`.

- [ ] **Step 5: Commit**

```bash
git add database/normalized_fundamentals.py test_normalized_fundamentals.py
git commit -m "Normalizar identidad fiscal de observaciones Yahoo"
```

---

### Task 7: Comparación contextual SEC–Yahoo

**Stage:** Etapa 2B

**Files:**
- Create: `database/effective_fundamentals.py`
- Create: `test_effective_fundamentals.py`

**Interfaces:**
- Consumes: dos `NormalizedObservation` comparables.
- Produces: `compare_observations(sec, yahoo) -> ComparisonDiagnostic`.

- [ ] **Step 1: Escribir tabla de tests fallidos**

```python
CASES = [
    (0.001, "MINOR_DIFFERENCE"),
    (0.001001, "DISCREPANCY_RECORDED"),
    (0.009999, "DISCREPANCY_RECORDED"),
    (0.01, "REVIEW_REQUIRED"),
    (0.05, "REVIEW_REQUIRED"),
    (0.050001, "REVIEW_REQUIRED_HIGH"),
]

def test_numeric_review_does_not_change_source_eligibility():
    diagnostic = compare_observations(sec(100), yahoo(105))
    self.assertEqual(diagnostic.comparison_status, "REVIEW_REQUIRED")
    self.assertEqual(sec(100).selection_eligibility, "ELIGIBLE")
```

- [ ] **Step 2: Ejecutar y confirmar fallos**

Run: `python -m unittest test_effective_fundamentals.py -v`

Expected: FAIL porque `compare_observations` no existe.

- [ ] **Step 3: Implementar chequeos estructurales antes del porcentaje**

Comparar métrica, identidad fiscal, moneda, unidad, escala, signo, fechas, semántica y basic/diluted. Si no son comparables, retornar `comparison_diff_pct=None` y razón enumerada; no mutar ninguno de los objetos normalizados.

La diferencia numérica es `abs(sec-yahoo) / max(abs(sec), abs(yahoo)) * 100`, con caso ambos cero igual a 0.

- [ ] **Step 4: Probar dominio y versionado**

Run: `python -m unittest test_effective_fundamentals.py -v`

Expected: todos `OK`; cada diagnóstico contiene `sec-yahoo-comparison-v1` y referencia a la contraparte.

- [ ] **Step 5: Commit**

```bash
git add database/effective_fundamentals.py test_effective_fundamentals.py
git commit -m "Separar diagnósticos comparativos SEC Yahoo"
```

---

### Task 8: Motor de selección `c-v2.6-compatible-v1`

**Stage:** Etapa 3A

**Files:**
- Modify: `database/effective_fundamentals.py`
- Modify: `test_effective_fundamentals.py`

**Interfaces:**
- Consumes: `Sequence[NormalizedObservation]`, `as_of: datetime | None`.
- Produces: `select_effective_observations(observations, as_of=None) -> list[EffectiveObservation]`, `growth_yoy_by_source(rows) -> list[dict]`, `build_effective_series(rows) -> dict[str, list[EffectiveObservation]]`.

- [ ] **Step 1: Escribir tests fallidos de prioridad**

```python
def test_timeseries_beats_yfinance_within_35_days():
    chosen = build_yahoo_series([yahoo_ts("2025-09-30"), yfinance("2025-09-27")])
    self.assertEqual(chosen[0].observation.source_variant, YAHOO_TS_VARIANT)

def test_sec_beats_yahoo_within_35_days_even_with_numeric_review():
    chosen = select_effective_observations([sec("2025-06-28", 100), yahoo("2025-06-30", 106)])
    self.assertEqual(chosen[0].observation.source, "SEC")
    self.assertEqual(chosen[0].comparison.comparison_status, "REVIEW_REQUIRED_HIGH")

def test_yahoo_fills_only_period_without_sec():
    self.assertEqual(select_one(yahoo("2025-09-30", 1.85)).selection_reason,
                     "YAHOO_FALLBACK_NO_SEC_WITHIN_35D")
```

- [ ] **Step 2: Ejecutar y confirmar fallos**

Run: `python -m unittest test_effective_fundamentals.py -v`

Expected: FAIL por selección ausente.

- [ ] **Step 3: Implementar selección determinista**

Filtrar `REPORTED`, elegibilidad y versiones. Sólo las observaciones Yahoo cuya identidad fiscal haya quedado resuelta previamente mediante evidencia suficiente e independiente de la mera proximidad pueden entrar en las ventanas de selección. Construir SEC y cada variante Yahoo por separado. Resolver primero Yahoo TS > yfinance a ±35; después SEC > Yahoo a ±35. Estas ventanas alinean y seleccionan observaciones ya identificadas, pero nunca asignan ni desambiguan FY/FQ/canonical. Conservar `series_date`. Excluir diluted shares Yahoo y cualquier observación `DERIVED`. Empates o identidad ambigua no generan fila efectiva.

- [ ] **Step 4: Implementar YoY y aceleración compatibles**

Buscar comparable de la misma fuente a ±45 días, exigir anterior `>0`, calcular YoY por fuente y combinar crecimientos sólo después. `previous_yoy` existe sólo a 70–120 días; aceleración es latest menos previous.

Run: `python -m unittest test_effective_fundamentals.py test_validate_fundamentals.py -v`

Expected: todos `OK`, incluidos límites 35/36, 45/46 y 70/120/121 días.

- [ ] **Step 5: Commit**

```bash
git add database/effective_fundamentals.py test_effective_fundamentals.py
git commit -m "Implementar política compatible de selección efectiva"
```

---

### Task 9: Proyección `fundamentals_effective_current`

**Stage:** Etapa 3B

**Files:**
- Modify: `database/schema_fundamentals_effective.sql`
- Modify: `database/effective_fundamentals.py`
- Modify: `test_fundamentals_effective_schema.py`
- Modify: `test_effective_fundamentals.py`

**Interfaces:**
- Consumes: `fundamentals_normalized` con versiones `*-normalized-v1`.
- Produces: vista `fundamentals_effective_current` y `load_effective_current(company_id: int) -> list[dict]`.

- [ ] **Step 1: Escribir tests fallidos de forma y trazabilidad**

```python
def test_current_projection_exposes_lineage_and_comparison():
    row = load_effective_current(1)[0]
    for field in ("selected_observation_id", "raw_id", "source_variant",
                  "selection_policy_version", "selection_reason",
                  "comparison_status", "comparison_rules_version"):
        self.assertIn(field, row)
```

- [ ] **Step 2: Ejecutar y confirmar fallo**

Run: `python -m unittest test_fundamentals_effective_schema.py test_effective_fundamentals.py -v`

Expected: FAIL porque la vista/loader no existen.

- [ ] **Step 3: Añadir vista actual**

La vista debe usar el mismo ranking y ventanas que Task 8, hardcodear sólo las versiones `v1` aprobadas y exponer todos los campos de la sección 13 de la spec. La comparación es un `LEFT JOIN` contextual; `comparison_status='NOT_COMPARED'` sin contraparte. No materializar ni actualizar resultados.

Aplicar la definición actualizada de forma controlada:

```powershell
python -c "from pathlib import Path; from database.db import get_connection; sql=Path('database/schema_fundamentals_effective.sql').read_text(encoding='utf-8'); c=get_connection(); c.execute(sql); c.commit()"
```

Expected: `fundamentals_effective_current` existe y las tablas legacy conservan sus conteos.

- [ ] **Step 4: Verificar equivalencia Python/SQL**

Run: `python -m unittest test_fundamentals_effective_schema.py test_effective_fundamentals.py -v`

Expected: misma secuencia de `(metric, series_date, value, source, source_variant)` desde el selector puro y la vista para el fixture transaccional.

- [ ] **Step 5: Commit**

```bash
git add database/schema_fundamentals_effective.sql database/effective_fundamentals.py test_fundamentals_effective_schema.py test_effective_fundamentals.py
git commit -m "Exponer proyección actual de fundamentales efectivos"
```

---

### Task 10: Adaptador del contrato C

**Stage:** Etapa 4A

**Files:**
- Create: `database/c_fundamentals_adapter.py`
- Create: `test_c_fundamentals_adapter.py`

**Interfaces:**
- Consumes: `load_effective_current`, funciones de crecimiento de Task 8 y complementos no fundamentales obtenidos por `fundamental_c.py`.
- Produces: `load_c_fundamental_report(ticker: str, as_of: datetime | None = None) -> dict` y `analyze_current_earnings_postgres(ticker: str, as_of=None) -> dict`.

- [ ] **Step 1: Escribir test fallido del contrato**

```python
REQUIRED_C_KEYS = {
    "eps_quarters", "revenue_quarters", "net_income_quarters",
    "eps_yoy_pct", "revenue_yoy_pct", "latest_eps", "latest_eps_source",
    "latest_eps_yoy_pct", "previous_eps_yoy_pct", "eps_acceleration_pp",
    "latest_revenue", "latest_revenue_yoy_pct",
    "previous_revenue_yoy_pct", "revenue_acceleration_pp",
    "eps_loss_to_profit", "eps_change_type", "data_integrity",
}

def test_adapter_produces_c_contract_without_scoring():
    report = load_c_fundamental_report("AAPL")
    self.assertTrue(REQUIRED_C_KEYS <= report.keys())
    self.assertNotIn("c_score_v1", report)
```

- [ ] **Step 2: Ejecutar y confirmar fallo**

Run: `python -m unittest test_c_fundamentals_adapter.py -v`

Expected: ERROR por módulo ausente.

- [ ] **Step 3: Implementar adaptación sin duplicar score**

Construir listas `{date, value}` ordenadas, últimos valores, crecimiento, aceleración y loss-to-profit. `analyze_current_earnings_postgres` combina ese bloque con splits/consistencia/complementos ya existentes y llama exactamente una vez:

```python
report.update(build_c_score(report))
return report
```

No copiar ninguna fórmula desde `c_score_v1.py`.

- [ ] **Step 4: Probar nulos, orden y fuente**

Run: `python -m unittest test_c_fundamentals_adapter.py test_c_score_v12_low_persistence_cases.py -v`

Expected: todos `OK`; `c_score_v1.py` sin diff.

- [ ] **Step 5: Commit**

```bash
git add database/c_fundamentals_adapter.py test_c_fundamentals_adapter.py
git commit -m "Adaptar fundamentales PostgreSQL al contrato C"
```

---

### Task 11: Ejecución dual y equivalencia completa AAPL

**Stage:** Etapa 4B

**Files:**
- Modify: `database/validate_fundamentals.py`
- Modify: `test_validate_fundamentals.py`
- Modify: `test_c_fundamentals_adapter.py`

**Interfaces:**
- Consumes: `analyze_current_earnings("AAPL")`, `analyze_current_earnings_postgres("AAPL")`, fixture baseline.
- Produces: `compare_c_reports(live: dict, postgres: dict) -> dict` y CLI `--source-comparison` con exit code no cero ante diferencias materiales.

- [ ] **Step 1: Escribir tests fallidos de comparación exacta**

```python
def test_comparison_detects_order_source_and_null_differences():
    result = compare_c_reports(report_a(), report_b_with_wrong_source())
    self.assertFalse(result["equivalent"])
    self.assertIn("latest_eps_source", result["differences"])

def test_score_and_class_must_match_exactly():
    result = compare_c_reports(report_a(), report_b_with_wrong_score())
    self.assertFalse(result["equivalent"])
```

- [ ] **Step 2: Ejecutar y confirmar fallo**

Run: `python -m unittest test_validate_fundamentals.py test_c_fundamentals_adapter.py -v`

Expected: FAIL por comparador ausente.

- [ ] **Step 3: Implementar comparación**

Usar tolerancias existentes de EPS y valores grandes. Comparar fechas, fuente, variante, orden y nulos exactamente; score y clase exactamente; valores financieros con tolerancia. Salida estructurada incluye gates `sec_75_75`, `hybrid_57_60`, `yahoo_q4_3`, `c_inputs_10_10`, `report_equivalent`, `score_equivalent`.

- [ ] **Step 4: Ejecutar gate dual AAPL**

Run:

```powershell
python database/validate_fundamentals.py AAPL --source-comparison
python -m unittest test_validate_fundamentals.py test_c_fundamentals_adapter.py -v
```

Expected: 75/75 observaciones SEC efectivas por métrica-periodo tras resolución/deduplicación compatible de filings; 57/60 respaldadas por PostgreSQL; tres fallbacks `2025-09-30` con las tres métricas; 10/10 inputs; mismas fechas/fuentes/orden/nulos; mismo report relevante, C Score y clase.

- [ ] **Step 5: Confirmar legacy y commit**

Run: `git diff --exit-code -- fundamental_c.py c_score_v1.py database/schema_fundamentals.sql`

Expected: sin salida.

```bash
git add database/validate_fundamentals.py test_validate_fundamentals.py test_c_fundamentals_adapter.py
git commit -m "Validar ejecución dual de fundamentales AAPL"
```

---

### Task 12: Consultas point-in-time observadas

**Stage:** Etapa 5

**Files:**
- Modify: `database/effective_fundamentals.py`
- Create: `test_fundamentals_point_in_time.py`

**Interfaces:**
- Consumes: observaciones normalizadas y `as_of: datetime` timezone-aware.
- Produces: `load_effective_as_of(company_id: int, as_of: datetime) -> list[EffectiveObservation]`.

- [ ] **Step 1: Escribir tests fallidos contra look-ahead**

```python
def test_filter_happens_before_amendment_ranking():
    before = load_effective_as_of(1, datetime(2025, 2, 1, tzinfo=UTC))
    after = load_effective_as_of(1, datetime(2025, 3, 1, tzinfo=UTC))
    self.assertEqual(value(before), Decimal("1.00"))
    self.assertEqual(value(after), Decimal("1.10"))

def test_yahoo_is_invisible_before_first_observation():
    self.assertEqual(load_effective_as_of(1, OBSERVED - timedelta(seconds=1)), [])

def test_source_available_at_null_does_not_block_observed_mode():
    self.assertEqual(len(load_effective_as_of(1, OBSERVED)), 1)
```

- [ ] **Step 2: Ejecutar y confirmar fallos**

Run: `python -m unittest test_fundamentals_point_in_time.py -v`

Expected: FAIL por loader ausente.

- [ ] **Step 3: Implementar consulta parametrizada**

La primera condición SQL debe ser `observed_at <= %s`; dentro de ese conjunto resolver `normalizer_version`, filings SEC, variantes Yahoo y prioridad. Rechazar `as_of` sin zona horaria con `ValueError` para evitar cortes ambiguos. No filtrar por `source_available_at` en este modo.

- [ ] **Step 4: Ejecutar regresión point-in-time**

Run: `python -m unittest test_fundamentals_point_in_time.py test_effective_fundamentals.py -v`

Expected: todos `OK`; amendment y Yahoo aparecen sólo desde su `observed_at`; dos ejecuciones del mismo corte son idénticas.

- [ ] **Step 5: Commit**

```bash
git add database/effective_fundamentals.py test_fundamentals_point_in_time.py
git commit -m "Añadir selección point-in-time por disponibilidad observada"
```

---

### Task 13: Gate integral y cierre de implementación

**Stage:** Checkpoint final de Etapas 0–5

**Files:**
- Modify only if a demonstrated failure belongs to new code: files introduced in Tasks 1–12.
- Do not modify: `fundamental_c.py`, `c_score_v1.py`, `database/schema_fundamentals.sql`.

**Interfaces:**
- Consumes: todos los tests, CLI de validación dual y PostgreSQL local.
- Produces: evidencia de aceptación; no cambia la fuente principal todavía.

- [ ] **Step 1: Ejecutar suite específica completa**

```powershell
python -m unittest `
  test_fundamentals_effective_schema.py `
  test_normalized_fundamentals.py `
  test_backfill_normalized.py `
  test_yahoo_import.py `
  test_effective_fundamentals.py `
  test_fundamentals_point_in_time.py `
  test_c_fundamentals_adapter.py `
  test_validate_fundamentals.py -v
```

Expected: todos `OK`, cero failures y cero errors.

- [ ] **Step 2: Ejecutar regresiones de C existentes**

Run: `python -m unittest test_c_score_v12_low_persistence_cases.py -v`

Expected: todos `OK`. Ejecutar suites S&P/Russell sólo si sus fixtures/dependencias locales están disponibles; cualquier fallo de red se documenta y no se interpreta como equivalencia aprobada.

- [ ] **Step 3: Ejecutar validación real AAPL**

Run: `python database/validate_fundamentals.py AAPL --source-comparison`

Expected:

```text
SEC_EFFECTIVE_METRIC_PERIOD=75/75  # EPS 19 + Revenue 19 + Net Income 19 + Diluted Shares 18
HYBRID_POSTGRES=57/60
YAHOO_Q4_FALLBACKS=3/3
C_INPUT_GROUPS=10/10
REPORT_EQUIVALENT=PASS
C_SCORE_EQUIVALENT=PASS
DERIVED_ACTIVE=0
```

- [ ] **Step 4: Verificar invariantes PostgreSQL y legacy**

Ejecutar consultas read-only:

```sql
SELECT COUNT(*) FROM fundamentals_raw fr JOIN companies c ON c.id=fr.company_id
WHERE c.ticker='AAPL' AND fr.source='SEC';
-- 135

SELECT COUNT(*) FROM fundamentals_quarterly fq JOIN companies c ON c.id=fq.company_id
WHERE c.ticker='AAPL';
-- 19

SELECT COUNT(*) FROM fundamentals_normalized
WHERE source='DERIVED' OR observation_kind='DERIVED';
-- 0
```

Expected: `135`, `19`, `0`; ningún Yahoo ha sobrescrito SEC y cada fila efectiva enlaza raw/normalized.

- [ ] **Step 5: Verificar Git y checkpoint**

Run:

```powershell
git diff --check
git status --branch --porcelain=v1
git diff ab0ed2d57f9a43402bbe4ed8af641c7de27a410f -- c_score_v1.py database/schema_fundamentals.sql
```

Expected: diff check limpio; working tree limpio; cero cambios en archivos protegidos. No cambiar aún la fuente principal de `fundamental_c.py`.

No crear commit si no hubo corrección. Si aparece un fallo, detener este
checkpoint, volver a la tarea propietaria del módulo afectado y ejecutar allí
un ciclo rojo/verde con los archivos y el mensaje de commit exactos definidos
en esa tarea. Repetir Task 13 completa después de ese commit.

## Cobertura de gates por tarea

| Gate | Primera prueba | Gate definitivo |
|---|---|---|
| SEC AAPL efectivo por métrica-periodo 75/75 tras resolución/deduplicación compatible (19+19+19+18; no es el recuento de `fundamentals_normalized`) | Task 4 | Task 13 |
| Híbrido 57/60 PostgreSQL | Task 1 baseline | Tasks 11 y 13 |
| Tres Yahoo Q4 FY2025 | Tasks 1 y 6 | Tasks 11 y 13 |
| Inputs C 10/10 | Task 1 baseline | Tasks 11 y 13 |
| Fechas, fuentes, variantes, orden y nulos | Tasks 6–10 | Tasks 11 y 13 |
| Mismo report relevante | Task 10 | Tasks 11 y 13 |
| Mismo C Score y clase | Task 10 | Tasks 11 y 13 |
| `c_score_v1.py` intacto | Cada checkpoint | Tasks 11 y 13 |
| `fundamentals_quarterly` legacy | Tasks 2 y 4 | Task 13 |
| Ningún `DERIVED` | Tasks 2 y 8 | Task 13 |
| Sin look-ahead observado | Task 12 | Task 13 |

## Riesgos y controles de ejecución

- **Baseline dependiente de red:** capturar la línea base una sola vez con fecha y guardar sólo datos no secretos; comparar después contra fixture.
- **DDL sobre la base local:** aplicar únicamente tras backup y aprobación operativa; el SQL es aditivo y no altera tablas legacy.
- **Deduplicación Yahoo:** el hash canónico incluye variante, proveedor, métrica, periodo, valor, unidad y moneda; no incluye `observed_at`.
- **Duplicación Python/SQL:** Task 9 exige prueba de equivalencia entre selector puro y vista para impedir divergencia.
- **Calendarios fiscales irregulares:** ambigüedad produce observación conservada pero inelegible; nunca se resuelve sólo por proximidad.
- **Amendments y look-ahead:** filtrar por `observed_at` antes del ranking, probado en Task 12.
- **Cambio accidental de score:** `build_c_score()` se reutiliza; `c_score_v1.py` se verifica sin diff en checkpoints.
- **Conmutación prematura:** el plan termina con ejecución dual; cambiar la fuente principal requiere aprobación posterior.

## Opciones documentadas para la fase de ejecución

- **Subagent-Driven:** usar `superpowers:subagent-driven-development`, una tarea por agente y revisión entre commits.
- **Inline Execution:** usar `superpowers:executing-plans`, ejecutar por tareas con checkpoints humanos.

La elección y el inicio de ejecución quedan fuera de esta tarea y requieren revisión humana del plan.
