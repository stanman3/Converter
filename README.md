# Converter

Reads an Informatica PowerCenter XML mapping export and generates equivalent
dbt SQL models, built as a chain of CTEs ending in `select * from final`.

## How to run

```bash
env/bin/python converter.py examples/FLOWLINE_DEMO_JAFFLESHOP.xml --out models/
```

Or, with the virtualenv activated:

```bash
source env/bin/activate
pip install -r requirements.txt

python converter.py path/to/your_export.xml --out models/
```

Options:

| Flag | Default | Effect |
| --- | --- | --- |
| `--out DIR` | `models` | where the `.sql` files are written |
| `--source-prefix P` | `stg_` | prefix applied to generated `ref()` names |
| `--preserve-case` | off | keep PowerCenter's uppercase identifiers |
| `--strict` | off | exit non-zero on an unsupported transformation instead of emitting a TODO |

One `.sql` file is written per `<MAPPING>` in the export, named after the
mapping — a single export containing five mappings produces five files. Output
lands in `models/`, which is gitignored.

Run the tests with `pytest`.

## How it works

Three stages, in [converter.py](converter.py):

1. **Parse** — read instances, transformations, ports and connectors into
   dataclasses. Nothing after this stage touches lxml.
2. **Plan** — treat connectors as edges between instances and topologically
   sort them (`build_plan`). This is what fixes CTE ordering.
3. **Generate** — emit one CTE per instance in that order, each selecting from
   the CTEs upstream of it.

A source feeding a Source Qualifier is folded into a single CTE, so the `ref()`
appears once. The terminal step is renamed `final`.

## Assumptions

- The export is rooted at `<POWERMART>`; the referenced DTD is not required and
  is not resolved.
- Every PowerCenter source has a corresponding dbt model named
  `<source-prefix><source>` — by default `stg_customers` for source `customers`.
- Informatica expression syntax is close enough to the target SQL dialect to be
  copied through verbatim. `MIN()`, `SUM()`, `||` and the like survive; anything
  Informatica-specific (`IIF`, `DECODE`, `TO_DATE` format strings) does not.
- Identifiers are lowercased to match dbt convention; text inside single quotes
  is left alone. `--preserve-case` turns this off.
- In a Joiner, the first upstream pipeline in connector order is treated as the
  left side of the SQL join.

## Known limitations

Keep in sync with `SUPPORTED_TYPES` in [converter.py](converter.py).

- Router transformations (RTR_*) not supported
- Custom transformations (UN_*) not supported
- **Join master/detail is a guess.** PowerCenter's Master/Detail Outer Join
  semantics are mapped to `left`/`right join` by assuming the first connected
  pipeline is the master. Verify every non-inner join by hand.
- **Join conditions are qualified heuristically.** PowerCenter writes them as
  bare port pairs (`ORDER_ID = ORDER_ID`); `_qualify_condition` rewrites those
  as `left.ORDER_ID = right.ORDER_ID`. Conditions with function calls or
  literals may come out wrong.
- **Router, and mappings with multiple targets, are not modelled.** One mapping
  produces one model with one terminal CTE.
- **No dialect awareness.** Nothing is translated to Snowflake/BigQuery/Postgres
  equivalents.
- **Not handled at all:** mapplets, parameters and variables (`$$PARAM`),
  sessions/workflows, update strategies, and pre/post SQL.
- **CTE names can collide.** Names are derived by stripping the conventional
  instance prefix (`AGG_CUSTOMER_ORDERS` → `customer_orders`); collisions get a
  `_2` suffix, which is correct but ugly.

## Where the agent went wrong

Conversion failures and bad output found while building this, kept as a record of what to distrust.

| Where | What went wrong | Why it happened | Status |
| --- | --- | --- | --- |
| Generation | First version emitted a single flat `select` per mapping, so two chained Expressions collapsed into one projection referencing an alias defined alongside it — invalid SQL. | The connector graph was parsed but never used; transformation order was ignored entirely. | Fixed — replaced by the topological plan in `build_plan`. |
| Parsing | Every projection degraded to `select *` and all join conditions vanished on the example export. | The parser only looked for `<TRANSFORMATION>` inside `<MAPPING>`, but reusable transformations are defined at `<FOLDER>` level. | Fixed — `parse_tree` now collects a reusable pool first. |
| Generation | `final` selected `total_amount` from a CTE that was never joined, producing SQL referencing a nonexistent column. | Non-Joiner transformations took `upstream[0]` and silently dropped any other input pipeline. | Flagged, not fixed — `_from_clause` now emits a TODO naming the dropped pipelines. |
| Generation | `STATUS = 'A'` was emitted as `status = A`, turning a string literal into an identifier. | `_lower_outside_quotes` split on `'` and rejoined with `""`, deleting every quote. | Fixed — caught by `test_lowercasing_preserves_quoted_literals`. |
| Examples | The first `examples/m_customers.xml` wired several pipelines straight into one Expression, which PowerCenter itself would reject. | The example was written to exercise the generator, not to be a faithful export. | Fixed — joins are chained through two Joiner instances. |
| Generation | Generated iif() instead of CASE WHEN for dbt compatibility | Fixed |
| Transformation | Used Oracle-specific date functions instead of standard SQL | Fixed |