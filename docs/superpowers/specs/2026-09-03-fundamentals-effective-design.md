# Fase 3A — Arquitectura de fundamentales efectivos

**Estado:** especificación propuesta para revisión humana

**Fecha:** 2026-09-03

**Política inicial:** `c-v2.6-compatible-v1`

## 1. Propósito

Esta especificación define la arquitectura mínima para alimentar el contrato de
`fundamental_c.py` desde PostgreSQL sin cambiar `c_score_v1.py`, sus fórmulas ni
el comportamiento financiero actual.

La arquitectura separa de forma explícita:

1. evidencia obtenida de cada proveedor;
2. observaciones normalizadas por fuente;
3. selección efectiva versionada;
4. adaptación al contrato que consume `build_c_score()`.

La primera implementación debe reproducir exactamente la política actual antes
de introducir mejoras metodológicas. No se calcularán valores `DERIVED` ni se
derivará Q4 en esta fase.

## 2. Estado de partida y línea base

La línea base de regresión es:

- repositorio `main` en
  `89cfe1b880045c8ee0a4311cff3801babddbeb3a`;
- `fundamentals_raw` conserva evidencia SEC auditable;
- `fundamentals_quarterly` materializa una fila SEC por empresa y
  `period_end` mediante `sec-quarterly-v2`;
- AAPL contiene 135 filas SEC raw y 19 periodos normalizados;
- las cuatro series SEC suman 75 observaciones: 19 EPS, 19 revenue,
  19 net income y 18 diluted shares;
- PostgreSQL reproduce 75/75 observaciones SEC usadas por el código;
- el híbrido EPS/revenue/net income contiene 60 observaciones: 57 respaldadas
  por PostgreSQL y tres fallbacks Yahoo para Q4 FY2025;
- PostgreSQL reproduce 10/10 grupos de inputs actuales del C Score de AAPL;
- `fundamentals_quarterly` no contiene Q4 posteriores a FY2020 porque el
  normalizador sólo acepta hechos SEC explícitos de 70–110 días; no calcula
  `FY - Q1 - Q2 - Q3`.

## 3. Alcance

### Incluido

- contrato y responsabilidades de las capas de datos;
- esquema lógico de `fundamentals_normalized`;
- estrategia append-only, versionado y trazabilidad;
- normalización SEC y Yahoo sin mezcla entre fuentes;
- política efectiva `c-v2.6-compatible-v1`;
- selección actual y selección point-in-time observada;
- contrato del futuro adaptador PostgreSQL para C;
- estados de calidad, ambigüedad y revisión;
- etapas de migración y pruebas de aceptación.

### Excluido

- cambios en `c_score_v1.py` o en las fórmulas del score;
- cambios semánticos en `fundamentals_quarterly`;
- derivación de Q4 o cualquier otro valor `DERIVED`;
- una política conservadora alternativa;
- recalibración definitiva de umbrales SEC–Yahoo;
- retirada de `fundamentals_quarterly`;
- implementación, migraciones, ingestión o backfill.

## 4. Arquitectura

```text
                                 camino nuevo

SEC Company Facts ─┐
Yahoo ─────────────┼──> fundamentals_raw
DERIVED (futuro) ──┘             │
                                 ├── normalización SEC
                                 ├── normalización Yahoo
                                 └── normalización DERIVED (deshabilitada)
                                                │
                                                ▼
                                  fundamentals_normalized
                                                │
                                     política versionada
                                                │
                                                ▼
                                  fundamentals_effective_current
                                                │
                                   adaptador contrato C
                                                │
                                                ▼
                                      build_c_score() intacto

                              camino legacy durante transición

fundamentals_raw ──> normalizador SEC actual ──> fundamentals_quarterly
```

La capa efectiva es una proyección. No copia valores entre fuentes ni convierte
Yahoo o `DERIVED` en SEC. Toda fila efectiva conserva el identificador de la
observación normalizada seleccionada y la razón de selección.

## 5. Responsabilidad de cada capa

### 5.1 `fundamentals_raw`

Es el registro auditable de evidencia obtenida de proveedores.

- Conserva el valor y payload tal como se recibieron.
- Identifica fuente, métrica, periodo, metadatos de proveedor y momento de
  ingestión.
- No decide qué observación alimenta C.
- No alinea fechas entre fuentes.
- No mezcla observaciones.

SEC y Yahoo deben persistirse aquí antes de participar en la nueva capa. La
restricción de métricas actual es suficiente para EPS diluted, revenue, net
income y diluted shares. Cualquier ampliación futura de métricas queda fuera de
esta fase.

### 5.2 `fundamentals_normalized`

Es una capa larga y por fuente. Cada fila representa exactamente:

```text
una empresa + una métrica + una fuente + una observación + una versión de normalización
```

Normaliza tipo, unidad, moneda, identidad fiscal y fechas sin elegir una fuente
ganadora. Puede contener simultáneamente SEC y Yahoo para el mismo trimestre.

### 5.3 Selección efectiva

Aplica una política identificada y versionada a observaciones normalizadas
elegibles. La primera y única política de Fase 3A es
`c-v2.6-compatible-v1`.

La selección:

- no modifica observaciones normalizadas;
- no oculta la fuente seleccionada;
- registra `selection_reason`;
- excluye de selección silenciosa observaciones ambiguas;
- no permite `DERIVED` en Fase 3A.

### 5.4 `fundamentals_effective_current`

Es la proyección de conocimiento actual de la política compatible. Expone una
observación efectiva por empresa, métrica e identidad de periodo cuando la
selección es inequívoca.

No sustituye a la consulta point-in-time parametrizada. Una vista sin parámetro
no puede responder correctamente a un `as_of` histórico.

### 5.5 Adaptador PostgreSQL para C

Transforma la salida efectiva en las mismas series y el mismo diccionario que
`analyze_current_earnings()` entrega hoy a `build_c_score()`.

No calcula puntos ni cambia reglas del score. Su frontera termina antes de
`build_c_score()`.

### 5.6 `fundamentals_quarterly`

Permanece como capa legacy SEC y oráculo de comparación durante toda la
transición.

- No cambia su esquema ni semántica en Fase 3A.
- El normalizador SEC actual continúa funcionando.
- No se añaden filas Yahoo ni `DERIVED`.
- Dejará de ser fuente principal de scores sólo después de demostrar
  equivalencia.
- Su retirada requerirá una fase y aprobación independientes.

## 6. Esquema lógico de `fundamentals_normalized`

### 6.1 Columnas

| Columna | Tipo lógico | Nulabilidad | Semántica |
|---|---|---:|---|
| `id` | `BIGINT IDENTITY` | no | Identificador estable de observación normalizada. |
| `company_id` | `BIGINT` | no | FK a `companies(id)`, `ON DELETE RESTRICT`. |
| `metric` | `TEXT` | no | Una métrica admitida por `fundamentals_raw`. |
| `source` | `TEXT` | no | Familia de fuente: `SEC`, `YAHOO` o, en el futuro, `DERIVED`. |
| `source_variant` | `TEXT` | no | Dataset/subfuente estable que produjo el dato; no se infiere del payload. |
| `observation_kind` | `TEXT` | no | `REPORTED` o `DERIVED`. |
| `value` | `NUMERIC(30,8)` | no | Valor normalizado sin conversión entre fuentes. |
| `unit` | `TEXT` | no | Unidad normalizada: `USD`, `USD/shares` o `shares` según métrica. |
| `currency` | `TEXT` | sí | Moneda cuando la métrica sea monetaria. |
| `source_period_start` | `DATE` | sí | Inicio original publicado por la fuente. |
| `source_period_end` | `DATE` | no | Fin original publicado por la fuente; nunca se reescribe. |
| `canonical_period_end` | `DATE` | sí | Fecha de periodo canónico asignada con evidencia suficiente. |
| `series_date` | `DATE` | no | Índice que verá la serie compatible con C; en Fase 3A coincide con `source_period_end`. |
| `fiscal_year` | `INTEGER` | sí | Año fiscal confirmado o provisional según `intrinsic_quality_status`. |
| `fiscal_quarter` | `SMALLINT` | sí | Trimestre 1–4 confirmado o provisional según `intrinsic_quality_status`. |
| `filed_date` | `DATE` | sí | Fecha de filing; obligatoria para SEC elegible. Nula para Yahoo. |
| `source_available_at` | `TIMESTAMPTZ` | sí | Momento probado de disponibilidad pública. |
| `observed_at` | `TIMESTAMPTZ` | no | Primera captura de esa evidencia por CAN SLIM+. |
| `raw_id` | `BIGINT` | no | FK a la fila exacta de `fundamentals_raw`, `ON DELETE RESTRICT`. |
| `normalizer_version` | `TEXT` | no | Versión inmutable de reglas de normalización. |
| `intrinsic_quality_status` | `TEXT` | no | Calidad propia: `OK`, `REVIEW_REQUIRED`, `REVIEW_REQUIRED_HIGH` o `REJECTED`. |
| `intrinsic_quality_reasons` | `JSONB` | no | Razones estructurales propias, vacías cuando no hay incidencias. |
| `selection_eligibility` | `TEXT` | no | `ELIGIBLE` o `INELIGIBLE`; separa la capacidad de selección del estado de revisión. |
| `alignment_method` | `TEXT` | no | `EXACT`, `FISCAL_METADATA`, `SEC_CALENDAR`, `NEAREST_35D`, `SOURCE_ONLY` o `UNRESOLVED`. |
| `alignment_days` | `SMALLINT` | sí | Diferencia firmada `source_period_end - canonical_period_end`. |
| `alignment_reference_id` | `BIGINT` | sí | FK a la observación normalizada que aporta referencia temporal, normalmente SEC. |
| `created_at` | `TIMESTAMPTZ` | no | Momento de creación de esta versión normalizada. |

`source_variant` usa identificadores de máquina estables y extensibles. Los
valores iniciales son `sec.company_facts`, `yahoo.fundamentals_timeseries` y
`yfinance.quarterly_income_stmt`. Una variante futura recibe otro identificador;
nunca se cambia el significado de uno existente. Los cambios de interpretación
pertenecen a `normalizer_version`, mientras `source_variant` identifica el
dataset que entregó la evidencia.

`intrinsic_quality_reasons` es metadato de diagnóstico, no una vía para guardar
secretos ni un sustituto del payload raw.

En `fundamentals_normalized`, `intrinsic_quality_status` e
`intrinsic_quality_reasons` describen exclusivamente la calidad propia conocida
al normalizar esa evidencia: semántica, unidad, escala, signo, identidad fiscal
y fechas. No contienen estados producidos por comparar dos fuentes.

Los controles SEC–Yahoo son contextuales y se calculan al aplicar la política
versionada. La capa efectiva los expone por separado como `comparison_status`,
`comparison_reasons`, `comparison_reference_id`, `comparison_diff_pct` y
`comparison_rules_version`. Una comparación nueva nunca obliga a mutar una
observación normalizada histórica.

### 6.2 Restricciones

- `metric` usa el mismo dominio admitido por `fundamentals_raw`.
- `source_variant` es coherente con `source`: `sec.*` exige `SEC`, y
  `yahoo.*` o `yfinance.*` exigen `YAHOO`.
- `fiscal_quarter` es nulo o está entre 1 y 4.
- `source_period_start` es nulo o no supera `source_period_end`.
- `series_date = source_period_end` para `c-v2.6-compatible-v1`.
- `source='SEC'` implica `observation_kind='REPORTED'` y `filed_date IS NOT NULL`
  para que la observación sea elegible.
- `source='YAHOO'` implica `observation_kind='REPORTED'` y `filed_date IS NULL`.
- `source='DERIVED'` implica `observation_kind='DERIVED'`; además queda
  inhabilitado por la política inicial.
- `alignment_method='UNRESOLVED'` implica `canonical_period_end IS NULL` y un
  estado de revisión.
- `alignment_method='EXACT'` implica `alignment_days=0`.
- `alignment_days` debe coincidir con la diferencia entre las fechas cuando
  ambas existan.
- Una observación intrínsecamente `REJECTED`, con defecto estructural propio o con
  identidad fiscal ambigua tiene `selection_eligibility='INELIGIBLE'` y no es
  seleccionable automáticamente.
- Una discrepancia exclusivamente numérica es un diagnóstico comparativo y no
  modifica `selection_eligibility`; así se registra el control sin cambiar por
  sí solo la prioridad SEC o el C Score compatible.

Las restricciones relacionales que dependan de varias filas —por ejemplo una
referencia de alineación de la misma empresa y métrica— deben validarse en el
servicio de normalización y mediante tests; no se introducirán triggers en esta
fase salvo que el plan posterior demuestre que son necesarios.

### 6.3 Identidad y deduplicación

La identidad de una versión normalizada es:

```text
UNIQUE (raw_id, normalizer_version)
```

Una fila raw puede normalizarse de nuevo con otra versión sin sobrescribir la
versión anterior. Reejecutar la misma versión es idempotente. `source_variant`
es parte obligatoria de la identidad semántica y debe coincidir al resolver un
conflicto de esta clave; una discordancia para el mismo `raw_id` y versión es un
error de normalización, no una segunda fila válida.

Se requieren índices de lectura sobre:

```text
(company_id, metric, source, source_variant, source_period_end)
(company_id, metric, fiscal_year, fiscal_quarter)
(company_id, metric, observed_at)
(raw_id, normalizer_version) UNIQUE
```

No se declara una unicidad por empresa/fuente/métrica/periodo: dos filings SEC,
amendments o snapshots Yahoo pueden ser observaciones legítimas distintas.

## 7. Append-only y versionado

### 7.1 Regla append-only

Las observaciones normalizadas son inmutables. Una corrección se expresa como:

- nueva evidencia raw;
- una nueva `normalizer_version`; o
- ambas.

No se actualiza una fila histórica para cambiar valor, identidad fiscal,
alineación o calidad. Se permiten únicamente correcciones operativas que no
alteren su significado, y deberán evitarse en la primera implementación.

### 7.2 Versiones independientes

- `normalizer_version` identifica cómo se interpretó una evidencia de fuente.
- `selection_policy_version` identifica cómo se eligieron observaciones para
  formar series efectivas.
- La versión del C Score continúa siendo responsabilidad de `c_score_v1.py`.

No se reutilizará un nombre de versión después de cambiar sus reglas.

## 8. Fechas y disponibilidad point-in-time

### 8.1 Definiciones

- `source_period_start` y `source_period_end` son fechas contables originales.
- `canonical_period_end` identifica el periodo fiscal comparable sin destruir
  la fecha original.
- `series_date` es la fecha usada como índice por el algoritmo compatible.
- `filed_date` describe el filing SEC; no es una fecha genérica de ingestión.
- `source_available_at` es disponibilidad pública demostrable.
- `observed_at` es la primera disponibilidad demostrable dentro de CAN SLIM+.

### 8.2 Modo predeterminado

El modo point-in-time predeterminado es disponibilidad observada:

```text
observed_at <= as_of
```

Una consulta histórica debe formar sus candidatos después de aplicar este
filtro y sólo entonces resolver amendments, versiones y prioridad entre
fuentes. No se selecciona primero el estado actual para filtrarlo después.

### 8.3 Disponibilidad pública futura

Se conservará `source_available_at` para un futuro modo de información
públicamente disponible. Ese modo no forma parte de la primera política.

Para SEC, `filed_date` se conservará siempre. Si sólo se conoce una fecha y no
una hora fiable, la futura implementación deberá usar una convención explícita
y conservadora; no se inventará precisión temporal.

Para Yahoo:

- `asOfDate` es fecha contable y no se usa como publicación;
- si no hay timestamp fiable, `source_available_at` queda nulo;
- `observed_at` sigue siendo obligatorio;
- el dato puede usarse en análisis actuales y backtesting observado;
- un futuro modo público no puede presumir disponibilidad anterior sin
  evidencia.

`observed_at` se obtiene de la primera captura raw conservada (`fetched_at`), no
del momento en que se ejecuta posteriormente el normalizador. Normalizar de
nuevo la misma evidencia no adelanta ni retrasa su disponibilidad observada.

## 9. SEC: filings, amendments y restatements

Cada hecho SEC raw elegible produce su propia observación normalizada. No se
colapsan revisions al escribir `fundamentals_normalized`.

Para un `as_of` dado, la serie SEC selecciona por empresa, métrica y
`source_period_end`:

1. observaciones con `observed_at <= as_of`;
2. la versión del normalizador solicitada;
3. observaciones elegibles por calidad y estructura;
4. el filing más reciente disponible, ordenado por `filed_date` y después por
   `raw_id` como desempate estable.

Así, un amendment o restatement sólo afecta consultas cuyo `as_of` sea posterior
a su observación. La consulta actual usa el último filing elegible conocido,
replicando la selección vigente de `fundamental_c.py` y del normalizador SEC.

La identidad fiscal sigue separando:

- valores: filing más reciente disponible;
- identidad del trimestre: evidencia original más antigua válida del periodo.

Este criterio replica `sec-quarterly-v2` y evita que una repetición posterior
reasigne el trimestre histórico al FY/FP del filing nuevo.

## 10. Yahoo

### 10.1 Persistencia

Las respuestas de Yahoo fundamentals-timeseries y los datos trimestrales de
yfinance deben persistirse en `fundamentals_raw` con identificadores de origen
y payload suficientes para reconstruir qué endpoint produjo cada valor.

El índice raw actual no incluye `value` ni `source_variant` en su clave de
deduplicación. Para no perder una corrección Yahoo que reutilice el mismo
periodo o identificador, el importador debe construir un `source_record_id`
estable que incluya el identificador de variante, el identificador del
proveedor y un hash canónico del contenido semántico relevante. Repetir
exactamente el mismo dato será idempotente; un valor o metadato corregido
producirá otra evidencia raw. Esto preserva el esquema raw actual y evita usar
un timestamp de cada descarga que generaría duplicados idénticos.

Dentro de la serie Yahoo, `source_variant` permite aplicar y auditar la prioridad
actual sin inspeccionar el payload:

```text
Yahoo fundamentals-timeseries > yfinance quarterly_income_stmt
```

El segundo sólo aporta un periodo si el primero no tiene una observación dentro
de ±35 días. Esta decisión también debe ser determinista y trazable.

### 10.2 Identidad fiscal

Yahoo recibe identidad fiscal automática sólo con evidencia suficiente e
inequívoca, por este orden conceptual:

1. metadatos fiscales fiables de la fuente;
2. calendario fiscal conocido de la empresa;
3. periodos SEC anteriores y posteriores coherentes;
4. proximidad temporal como evidencia auxiliar.

La proximidad por sí sola no resuelve una ambigüedad. Cuando no haya evidencia
suficiente:

- se conserva la observación;
- `fiscal_year`, `fiscal_quarter` y `canonical_period_end` pueden ser nulos;
- `alignment_method='UNRESOLVED'`;
- `intrinsic_quality_status='REVIEW_REQUIRED'`;
- queda excluida de selección automática.

La fecha original Yahoo se conserva siempre en `source_period_end` y
`series_date`.

### 10.3 Alineación ±35 días

Una observación Yahoo puede asociarse con un periodo SEC sólo si:

- coincide empresa y métrica;
- la diferencia absoluta es como máximo 35 días;
- unidad, moneda, escala, signo y semántica son compatibles;
- la identidad fiscal conocida no contradice la asociación;
- existe un único mejor candidato; un empate es ambiguo.

La alineación registra el periodo canónico, método, días y observación SEC de
referencia. No cambia la fecha Yahoo original.

Si no existe SEC dentro de la ventana, Yahoo puede aportar un periodo únicamente
cuando su identidad fiscal sea inequívoca. Éste es el caso que debe permitir
representar explícitamente los tres fallbacks Yahoo de Q4 FY2025 de AAPL.

## 11. Calidad y discrepancias SEC–Yahoo

Los controles de calidad no cambian automáticamente la prioridad SEC, el valor
seleccionado ni el C Score.

### 11.1 Controles estructurales previos

Antes de calcular una diferencia numérica se comprueban:

- métrica y semántica;
- periodo e identidad fiscal;
- moneda y unidad;
- escala;
- signo;
- fechas y alineación;
- EPS basic frente a diluted.

Una incompatibilidad estructural propia produce
`intrinsic_quality_status='REVIEW_REQUIRED'` o `REVIEW_REQUIRED_HIGH` y
`selection_eligibility='INELIGIBLE'`. Una incompatibilidad detectada únicamente
al enfrentar SEC con Yahoo se registra en `comparison_status` y puede excluir
esa asociación contextual sin mutar ni invalidar globalmente las observaciones.
No se fuerza una comparación porcentual entre valores no comparables.

### 11.2 Umbrales numéricos iniciales

La diferencia porcentual absoluta se clasifica sin huecos:

| Diferencia | `comparison_status` |
|---:|---|
| `<= 0,1 %` | `MINOR_DIFFERENCE` |
| `> 0,1 %` y `< 1 %` | `DISCREPANCY_RECORDED` |
| `>= 1 %` y `<= 5 %` | `REVIEW_REQUIRED` |
| `> 5 %` | `REVIEW_REQUIRED_HIGH` |

Una discrepancia exactamente igual a 5,00 % pertenece a `REVIEW_REQUIRED`.
`REVIEW_REQUIRED_HIGH` comienza estrictamente por encima del 5 %.

Los umbrales son controles provisionales y deberán recalibrarse con una muestra
amplia. Cambiarlos exigirá nueva versión de reglas de calidad o de política.
Se registran bajo `comparison_rules_version`; ninguno de estos cuatro estados
forma parte del dominio de `intrinsic_quality_status`.

### 11.3 EPS

EPS requiere además registrar:

- cambio de signo;
- transición profit/loss;
- uso de basic frente a diluted;
- cambio material del YoY resultante.

Estas condiciones pueden elevar el estado a revisión aunque la diferencia
porcentual aislada no alcance el umbral general.

## 12. Política `c-v2.6-compatible-v1`

### 12.1 Universo elegible

La política sólo considera:

- observaciones `REPORTED` SEC y Yahoo;
- versión de normalizador requerida;
- observaciones disponibles para el `as_of` solicitado;
- observaciones sin ambigüedad estructural que impida selección.

`DERIVED` se excluye de forma incondicional.

### 12.2 Construcción de series independientes

Para cada métrica se construyen por separado:

- serie SEC;
- serie Yahoo fundamentals-timeseries;
- fallback yfinance trimestral;
- serie Yahoo resultante.

Las dos primeras se distinguen mediante `source_variant`. La prioridad se
resuelve sobre ese campo, no mediante inspección del payload, orden accidental
de filas ni inferencias sobre `source_record_id`.

No se calcula un YoY mezclando numerador de una fuente con denominador de otra.

### 12.3 Merge de valores

Para EPS diluted, revenue y net income:

1. incluir todas las observaciones SEC elegibles;
2. para cada observación Yahoo elegible, buscar SEC dentro de ±35 días;
3. si existe SEC, mantener SEC y no añadir Yahoo;
4. si no existe SEC, añadir Yahoo conservando su `series_date` original.

Diluted shares conserva inicialmente el comportamiento actual: participa la
serie SEC; Yahoo no actúa como fallback efectivo para esta métrica.

La prioridad es por presencia temporal compatible, no por reemplazo físico:

```text
SEC reported > Yahoo reported
```

Una discrepancia registrada no altera por sí sola esta prioridad. Una
incompatibilidad o identidad ambigua excluye el candidato afectado de selección
silenciosa y exige revisión.

### 12.4 Crecimiento YoY

El YoY se calcula primero dentro de cada fuente:

1. para cada fecha actual, buscar el periodo más cercano a exactamente un año
   antes;
2. aceptar como comparable sólo una fecha a ±45 días;
3. exigir valor anterior estrictamente mayor que cero;
4. calcular `(actual / anterior - 1) * 100`;
5. mantener la fecha de la observación actual como índice del crecimiento.

Después se combinan los crecimientos:

- se incluyen todos los crecimientos SEC;
- un crecimiento Yahoo se añade sólo si no existe una observación SEC raw para
  su periodo actual dentro de ±35 días, igual que hace el código vigente.

No se permite recomputar un crecimiento híbrido tomando un periodo actual Yahoo
y un comparable SEC, ni al contrario.

### 12.5 Aceleración y últimos valores

- `latest_*_yoy_pct`: último crecimiento válido de la serie combinada.
- `previous_*_yoy_pct`: crecimiento válido inmediatamente anterior sólo si su
  distancia respecto al último está entre 70 y 120 días.
- `*_acceleration_pp`: último YoY menos el anterior cuando ambos existen.
- `latest_eps` y su comparable anual se obtienen de la misma fuente seleccionada
  para el último EPS.
- `eps_loss_to_profit` compara el último EPS con su comparable anual de la misma
  fuente.
- `eps_yoy_pct` conserva el historial ordenado que usa tendencia y persistencia.

### 12.6 `selection_reason`

La capa efectiva utiliza razones enumeradas, al menos:

- `SEC_ONLY`;
- `SEC_PREFERRED_WITHIN_35D`;
- `YAHOO_FALLBACK_NO_SEC_WITHIN_35D`;
- `YAHOO_SECONDARY_FALLBACK`;
- `NOT_SELECTED_AMBIGUOUS_FISCAL_IDENTITY`;
- `NOT_SELECTED_STRUCTURAL_MISMATCH`;
- `NOT_SELECTED_INELIGIBLE_REVIEW`;
- `NOT_SELECTED_DERIVED_DISABLED`.

Las razones `NOT_SELECTED_*` pertenecen a diagnósticos de candidatos; no deben
aparecer como una fila efectiva seleccionada.

## 13. Forma de la capa efectiva

`fundamentals_effective_current` expone como mínimo:

| Campo | Semántica |
|---|---|
| `company_id` | Empresa seleccionada. |
| `metric` | Métrica efectiva. |
| `fiscal_year`, `fiscal_quarter` | Identidad fiscal confirmada. |
| `canonical_period_end` | Periodo fiscal asociado. |
| `series_date` | Fecha que indexará la serie C. |
| `value`, `unit`, `currency` | Valor efectivo. |
| `source`, `source_variant`, `observation_kind` | Procedencia inequívoca. |
| `selected_observation_id` | FK lógica a `fundamentals_normalized(id)`. |
| `raw_id` | Evidencia raw. |
| `filed_date` | Filing SEC cuando aplique. |
| `source_available_at`, `observed_at` | Disponibilidad pública y observada. |
| `normalizer_version` | Normalización utilizada. |
| `selection_policy_version` | `c-v2.6-compatible-v1`. |
| `selection_reason` | Razón de elección. |
| `intrinsic_quality_status`, `intrinsic_quality_reasons` | Calidad propia preservada. |
| `selection_eligibility` | Elegibilidad independiente del estado de revisión. |
| `comparison_status`, `comparison_reasons` | Diagnóstico contextual SEC–Yahoo. |
| `comparison_reference_id` | Observación contraparte usada en la comparación. |
| `comparison_diff_pct` | Diferencia numérica cuando los valores son comparables. |
| `comparison_rules_version` | Versión de controles y umbrales aplicada. |
| `alignment_method`, `alignment_days` | Evidencia de alineación. |

La vista actual usa el último conocimiento observado. La reconstrucción
histórica se realizará mediante una consulta o función read-only con parámetros
`as_of`, `normalizer_version` y `selection_policy_version`; no mediante la vista
actual.

## 14. Contrato del adaptador PostgreSQL para C

El adaptador debe producir los mismos tipos y claves relevantes que
`analyze_current_earnings()` antes de llamar a `build_c_score()`:

- series ordenadas de EPS, revenue y net income;
- serie SEC de diluted shares;
- series `eps_yoy_pct` y `revenue_yoy_pct` con fechas originales;
- `latest_eps` y `latest_eps_source`;
- `latest_eps_yoy_pct`, `previous_eps_yoy_pct`, `eps_acceleration_pp`;
- `latest_revenue`, `latest_revenue_yoy_pct`,
  `previous_revenue_yoy_pct`, `revenue_acceleration_pp`;
- `eps_loss_to_profit` y `eps_change_type`;
- conteos SEC, Yahoo e híbridos;
- tags SEC y calidad de diluted shares;
- diagnósticos de consistencia y `data_integrity` necesarios por C.

Las funciones de splits, sorpresas y estimaciones permanecen fuera de esta
capa de fundamentales efectivos salvo que una fase posterior decida
persistirlas. El adaptador debe combinarlas con el mismo contrato actual sin
cambiar su semántica.

La equivalencia se evalúa sobre valores, fechas, fuentes, orden, nulos,
diagnósticos relevantes y resultado final, no sólo sobre el score numérico.

## 15. Datos ambiguos, errores y estados de revisión

### 15.1 Principio

Una observación nunca se descarta por ser ambigua: permanece en raw y
normalizada. Lo que se impide es su selección silenciosa.

### 15.2 Estados intrínsecos normalizados

- `OK`: observación estructuralmente válida.
- `REVIEW_REQUIRED`: identidad o estructura propia requiere revisión.
- `REVIEW_REQUIRED_HIGH`: defecto estructural propio de severidad alta.
- `REJECTED`: observación no utilizable por una causa determinista.

### 15.3 Estados comparativos efectivos

- `NOT_COMPARED`: no existe comparación aplicable.
- `MINOR_DIFFERENCE`: diferencia numérica menor o igual a 0,1 %.
- `DISCREPANCY_RECORDED`: diferencia superior a 0,1 % e inferior a 1 %.
- `REVIEW_REQUIRED`: incompatibilidad contextual o diferencia entre 1 % y 5 %,
  ambos inclusive.
- `REVIEW_REQUIRED_HIGH`: incompatibilidad contextual de severidad alta o
  diferencia superior a 5 %.

Una revisión puramente numérica no modifica automáticamente la elegibilidad ni
la prioridad SEC. La capa efectiva conserva simultáneamente el seleccionado y
el diagnóstico.

### 15.4 Comportamiento operacional

- Un fallo de normalización de una fila no debe borrar resultados anteriores.
- Una ejecución debe informar filas procesadas, insertadas, ya existentes,
  pendientes de revisión y rechazadas.
- Reejecutar una versión con los mismos raw debe ser idempotente.
- La ausencia de Yahoo no invalida la serie SEC.
- Una discrepancia sólo numérica se señala, pero no cambia automáticamente la
  prioridad SEC ni el C Score de compatibilidad.
- La ausencia de SEC permite fallback Yahoo sólo con identidad fiscal
  inequívoca.
- Una capa efectiva vacía o parcial debe producir datos parciales explícitos,
  nunca valores inventados.

## 16. Preparación futura para `DERIVED`

El dominio reserva `source='DERIVED'` y `observation_kind='DERIVED'`, pero la
política inicial lo excluye y ningún proceso lo generará.

Una fase futura deberá añadir linaje normalizado, como mínimo mediante:

```text
fundamental_derivation_inputs
    derived_observation_id
    input_observation_id
    input_role
```

También deberá definir y versionar fórmula, inputs, redondeo, disponibilidad,
calidad y validación. La disponibilidad de un derivado no podrá preceder a la
última disponibilidad permitida de todos sus inputs.

Reservar estos conceptos no autoriza a crear la tabla de linaje ni calcular Q4
en Fase 3A.

## 17. Invariantes

1. Una observación normalizada representa una sola métrica, fuente y evidencia.
2. SEC, Yahoo y `DERIVED` nunca se sobrescriben entre sí.
3. La subfuente se identifica siempre mediante `source_variant` y nunca se
   infiere posteriormente del payload.
4. Un valor `DERIVED` nunca se presenta como `REPORTED`.
5. Toda observación normalizada apunta a una evidencia raw exacta.
6. Toda fila efectiva apunta a una observación normalizada exacta.
7. `source_period_end` es inmutable y conserva la fecha de fuente.
8. `canonical_period_end` nunca sustituye silenciosamente a
   `source_period_end`.
9. `series_date` conserva la fecha original bajo la política compatible.
10. `source_available_at` y `observed_at` nunca se confunden ni se infieren uno
   del otro.
11. Una consulta `as_of` no usa observaciones con `observed_at > as_of`.
12. Amendments sólo afectan estados posteriores a su disponibilidad observada.
13. La selección siempre declara versión y razón.
14. Una observación ambigua o estructuralmente incompatible no se selecciona
    silenciosamente.
15. Calidad intrínseca y comparación entre fuentes usan campos y dominios
    separados.
16. El YoY no mezcla fuentes entre numerador y denominador.
17. Yahoo no desplaza SEC elegible dentro de ±35 días.
18. `DERIVED` está excluido de `c-v2.6-compatible-v1`.
19. `fundamentals_quarterly` conserva su semántica SEC actual.
20. `c_score_v1.py` y sus fórmulas permanecen intactos.
21. Una nueva regla exige una nueva versión; no reescribe resultados de una
    versión anterior.
22. Una ejecución fallida no deja selecciones efectivas parcialmente
    actualizadas.

## 18. Estrategia de migración

### Etapa 0 — Congelar contrato y regresiones

- Capturar el report relevante y el C Score actual de AAPL.
- Fijar tolerancias ya usadas por `validate_fundamentals.py`.
- Añadir pruebas del orden, fechas y procedencia de las series, no sólo valores.
- Confirmar línea base 75/75, 57/60, tres fallbacks y 10/10.

### Etapa 1A — Crear esquema normalizado SEC

- Crear `fundamentals_normalized` y sus restricciones.
- Implementar normalización append-only exclusivamente SEC.
- Mantener el normalizador legacy sin cambios.

### Etapa 1B — Backfill y equivalencia SEC

- Poblar desde las 135 filas raw de AAPL.
- Comparar contra `fundamentals_quarterly` y extracción SEC en memoria.
- Exigir 75/75 antes de continuar.

La división 1A/1B reduce el riesgo de confundir defectos de esquema con defectos
de backfill.

### Etapa 2A — Persistencia Yahoo raw

- Persistir por separado fundamentals-timeseries y yfinance trimestral.
- Registrar `observed_at` y mantener `source_available_at` nulo cuando proceda.
- Demostrar idempotencia y auditoría.

### Etapa 2B — Normalización, identidad y calidad Yahoo

- Normalizar Yahoo sin seleccionar fuente efectiva.
- Asignar identidad fiscal sólo con evidencia inequívoca.
- Registrar alineación y controles SEC–Yahoo.
- Identificar expresamente los tres Q4 FY2025 de AAPL.

### Etapa 3A — Motor de selección compatible

- Implementar `c-v2.6-compatible-v1` sobre datos normalizados.
- Excluir `DERIVED`.
- Probar merge ±35, fechas originales y selección por fuente.

### Etapa 3B — Proyección efectiva actual

- Exponer `fundamentals_effective_current`.
- Verificar trazabilidad completa y ausencia de candidatos ambiguos
  seleccionados.

### Etapa 4A — Adaptador del contrato C

- Construir series y derivados intermedios desde PostgreSQL.
- Mantener `build_c_score()` intacto.

### Etapa 4B — Ejecución dual y equivalencia

- Comparar camino actual y PostgreSQL sobre el mismo instante de corte.
- Exigir equivalencia del report relevante y del C Score.
- Conservar `fundamentals_quarterly` como control SEC.

### Etapa 5 — Point-in-time observado

- Implementar consulta parametrizada con `observed_at <= as_of`.
- Probar amendments, primeras observaciones Yahoo y ausencia de look-ahead.
- Mantener fuera el modo público hasta definir precisión temporal suficiente.

`DERIVED` no pertenece a ninguna etapa anterior y será un proyecto posterior.

## 19. Pruebas requeridas

### Esquema e invariantes

- restricciones de fuente, clase de observación, trimestre, periodo y fechas;
- FK raw y referencia de alineación;
- idempotencia de `(raw_id, normalizer_version)`;
- coexistencia SEC/Yahoo sin sobrescritura;
- `source_variant` obligatorio, coherente con la familia y preservado hasta la
  salida efectiva;
- dominio intrínseco sin estados de comparación;
- exclusión incondicional de `DERIVED`.

### Normalización SEC

- mismos tags, unidades y filtro de 70–110 días que el código vigente;
- selección por cobertura de tag;
- amendments y desempate estable;
- valor del filing más reciente e identidad fiscal original;
- Q4 explícito en 10-K y ausencia de derivación.

### Yahoo

- prioridad fundamentals-timeseries sobre yfinance trimestral;
- prioridad basada explícitamente en `source_variant`;
- conservación de fecha original;
- `source_available_at=NULL` cuando no existe evidencia;
- identidad fiscal inequívoca, provisional y ambigua;
- empates y límites exactos de ±35 días;
- controles de unidad, escala, moneda, signo y basic/diluted.
- separación entre calidad intrínseca y `comparison_status` versionado.

### Selección y cálculo intermedio

- SEC preferido dentro de ±35 días;
- Yahoo añadido sólo sin SEC dentro de la ventana;
- YoY calculado dentro de cada fuente;
- comparable anual en los límites exactos de ±45 días;
- rechazo de comparable anterior no positivo;
- aceleración sólo con separación de 70–120 días;
- orden, nulos y fuente del último EPS.

### Point-in-time

- exclusión de observaciones posteriores a `as_of`;
- amendment invisible antes de su `observed_at`;
- Yahoo invisible antes de su primera observación;
- reconstrucción determinista para el mismo `as_of` y versiones.

### Regresión AAPL

- SEC: 75/75;
- híbrido: 57/60 respaldado por PostgreSQL;
- tres fallbacks Yahoo explícitos para EPS, revenue y net income de Q4 FY2025;
- 10/10 grupos de inputs C reproducibles;
- mismas fechas y fuentes en las series relevantes;
- mismo report relevante dentro de las tolerancias existentes;
- mismo C Score y clasificación;
- `c_score_v1.py` sin cambios;
- `fundamentals_quarterly` conserva sus 19 periodos y comportamiento SEC;
- ningún `DERIVED` activo;
- ninguna observación ambigua seleccionada.
- cada valor Yahoo identifica explícitamente si procede de
  `yahoo.fundamentals_timeseries` o `yfinance.quarterly_income_stmt`.

## 20. Criterios de aceptación de Fase 3A

La futura implementación se considerará apta para sustituir la fuente principal
del C Score únicamente si cumple todos estos puntos:

1. El esquema respeta las invariantes y es append-only por versión.
2. Toda observación efectiva es trazable hasta raw.
3. AAPL conserva 75/75 observaciones SEC reproducidas.
4. Se reproducen las 60 observaciones híbridas, con 57 respaldadas por SEC
   PostgreSQL y tres Yahoo identificadas explícitamente.
5. Se reproducen 10/10 grupos de inputs del C Score.
6. El report relevante y el C Score coinciden dentro de las tolerancias ya
   definidas.
7. `c_score_v1.py` no cambia.
8. `fundamentals_quarterly` conserva el comportamiento SEC legacy.
9. No se calcula ni selecciona ningún dato `DERIVED`.
10. Yahoo no sobrescribe ni desplaza silenciosamente SEC.
11. Ningún dato ambiguo se selecciona silenciosamente.
12. Las consultas point-in-time observadas no presentan look-ahead.
13. La ejecución es idempotente y un fallo no deja estado efectivo parcial.
14. Cada observación efectiva conserva `source_variant`, y la prioridad interna
    Yahoo se demuestra sin inferir la subfuente del payload.
15. Los estados comparativos no aparecen en el dominio de calidad intrínseca.

## 21. Decisiones cerradas

- Arquitectura B: raw → normalizada por fuente → efectiva → adaptador C.
- Primera política única `c-v2.6-compatible-v1`.
- Reproducción exacta antes de mejoras metodológicas.
- Point-in-time predeterminado por `observed_at`.
- `source_available_at=NULL` para Yahoo sin timestamp fiable.
- Controles estructurales antes de diferencias numéricas.
- Subfuente/dataset explícito mediante `source_variant`.
- Calidad intrínseca separada de diagnósticos comparativos SEC–Yahoo.
- Umbrales iniciales de discrepancia sin efecto automático sobre score; el
  límite exacto de 5,00 % pertenece a `REVIEW_REQUIRED`.
- Identidad Yahoo automática sólo con evidencia inequívoca.
- `DERIVED` preparado conceptualmente pero deshabilitado.
- `fundamentals_quarterly` mantenida como legacy SEC.
- Fechas originales preservadas y YoY calculado dentro de cada fuente.

## 22. Decisiones explícitamente diferidas

No bloquean Fase 3A porque pertenecen a fases posteriores:

- fórmula y activación de Q4 `DERIVED`;
- política conservadora alternativa;
- recalibración definitiva de umbrales SEC–Yahoo;
- modo point-in-time de disponibilidad pública y convención horaria SEC;
- persistencia de splits, sorpresas y estimaciones;
- retirada de `fundamentals_quarterly`;
- ampliación del catálogo de métricas.

No quedan decisiones humanas abiertas necesarias para redactar el futuro plan
de implementación de la arquitectura aquí especificada. La creación de ese
plan requiere aprobación personal de esta especificación.
