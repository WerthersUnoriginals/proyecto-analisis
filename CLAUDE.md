# CAN SLIM Plus — instrucciones del proyecto

Sistema de análisis de acciones que convierte el método CAN SLIM de William O'Neil
en un proceso sistemático, explicable y verificable. Uso informativo y educativo.
Las siete piezas (C, A, N, S, L, I, M) se amplían con calidad de datos,
point-in-time, capa técnica con estados temporales y un Experience Store.

## Estado actual (2026-10-02)

- **C — Current Earnings: CERRADO.** Contrato end-to-end en commit `4bb7a81`.
- **A — Annual Earnings: spec en borrador** en
  `docs/superpowers/specs/2026-09-28-annual-earnings-design.md` (sin commitear,
  pendiente de revisión humana). Primera tarea tras aprobación: A1.1 (§21).
- N, S, L, I, M, capa técnica, Experience Store: sin empezar.

## Estructura

- `fundamental_c.py` — módulo C legacy (v2.6). Oráculo de comportamiento. **No modificar.**
- `c_score_v1.py` — C Score v1.2. **No modificar** (byte a byte).
- `database/` — capa PostgreSQL:
  - `fundamentals.py`, `sec_import.py`, `yahoo_import.py` — evidencia raw.
  - `normalized_fundamentals.py` — `fundamentals_normalized` append-only por fuente.
  - `effective_fundamentals.py` — política `c-v2.6-compatible-v1`.
  - `c_fundamentals_adapter.py` — contrato de 11 inputs de C. **No modificar.**
  - `c_data_integrity.py`, `split_integrity.py`, `corporate_actions.py`,
    `corporate_action_acquisition.py` — integridad y corporate actions.
  - `c_dual_run*.py`, `c_live_*.py` — comparación dual y diagnóstico live.
  - `migrations/` — migraciones versionadas `YYYY-MM-DD_nombre_vN.sql`. Nunca editar una existente.
- `fixtures/` — líneas base congeladas (AAPL).
- `test_*.py` en la raíz — tests `unittest`.
- `docs/superpowers/specs|plans/` — specs y planes (flujo superpowers).
- `watchlist_screener.py`, `seleccion.py`, `afinar.py`, `prueba.py`,
  `html pantallas/` — prototipos iniciales, fuera del núcleo.

## Tests

Batería offline (sin red ni PostgreSQL), ~408 tests en segundos:

```bash
python -m unittest test_backfill_normalized test_backfill_semantics_v2 test_c_data_integrity test_c_dual_run test_c_dual_run_contract test_c_fundamentals_adapter test_c_independent_contract test_c_independent_postgres_integration test_c_live_diagnostic test_c_live_runner test_c_normalized_row_shape test_corporate_action_acquisition test_corporate_action_migration test_corporate_action_postgres_concurrency test_corporate_action_postgres_concurrency_harness test_corporate_action_postgres_repository_integration test_corporate_action_repository test_corporate_actions test_effective_fundamentals test_fundamental_c_snapshot test_fundamentals_effective_schema test_fundamentals_semantics_v2_migration test_normalized_fundamentals test_split_integrity test_validate_fundamentals test_yahoo_import
```

- Integración PostgreSQL: opt-in con `CANSLIM_RUN_PG_INTEGRATION=1`. Usa la BD real
  `canslim` (usuario `canslim_app`, PostgreSQL 17) con preflight de identidad y
  UIDs de prueba reservados. Ejecutar sólo con autorización explícita.
- `test_c_score_*`, `test_sec_history`, `test_splits`, `test_yahoo_historico` son
  scripts de validación con red (SEC/Yahoo), no tests unitarios. No ejecutarlos sin pedirlo.
- Línea base de C que ningún cambio puede romper: `sec_effective=75`,
  `hybrid_effective=60`, contrato exacto de 11 inputs, vista
  `fundamentals_effective_current` de 37 columnas.
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
