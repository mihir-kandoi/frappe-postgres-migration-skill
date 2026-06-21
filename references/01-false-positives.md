# 01 — Postgres false positives: forms the framework auto-handles

> **Read this BEFORE you "fix" anything.** These constructs look MariaDB-specific but
> are transparently translated (or rendered engine-correctly) by the framework on
> every query. They produce identical results on MariaDB and Postgres. Flagging or
> rewriting them is wasted effort — and rewriting can *introduce* real divergence.
>
> Citations are to a real `frappe develop` checkout. **Line numbers drift between
> versions — grep for the named identifier (`IFNULL_PATTERN`, `modify_query`,
> `FUNCTION_MAPPING`, `patch_like_operators`, `has_index`), not the line number.**
> If your installed frappe is very old, confirm these handlers exist before trusting
> them.

---

## 1. Raw SQL `ifnull(` → `coalesce(` (all engines, every `frappe.db.sql`)

Every string passed through `frappe.db.sql()` has `ifnull(` rewritten to `coalesce(`
before execution, case-insensitively, on **all** backends.

- Regex: `IFNULL_PATTERN = re.compile(r"ifnull\(", flags=re.IGNORECASE)` — `frappe/database/database.py` (~`:63`)
- Applied unconditionally in the base `sql()`: `query = IFNULL_PATTERN.sub("coalesce(", query.strip())` — `frappe/database/database.py` (~`:247`)

Because it lives in the engine-agnostic base class, MariaDB also gets the rewrite
(MariaDB supports both, so no behaviour change) and Postgres (no `ifnull`) gets valid SQL.

```python
# Both forms execute as: SELECT coalesce(qty, 0) FROM `tabBin`
frappe.db.sql("SELECT ifnull(qty, 0) FROM `tabBin`")   # works on PG — NOT a break
```

Textual substitution only. It does **not** touch the `IfNull` pypika function used by
the query builder (that already renders per-dialect). Raw `ifnull(...)` is a **false positive**.

---

## 2. Postgres-only rewrites in `modify_query` (backtick, `locate(`, ` REGEXP `)

On Postgres only, `frappe.db.sql()` routes through `modify_query(query)` before
execution. Definition: `frappe/database/postgres/database.py` (find `def modify_query`):

1. **Backtick → double-quote identifiers**: `query = str(query).replace("`", '"')`. MariaDB
   backtick quoting (`` `tabItem` ``) becomes ANSI `"tabItem"` (`from tabX` → `from "tabX"`
   handled by `FROM_TAB_PATTERN`).
2. **`locate(x, y)` → `strpos(y, x)`**: `replace_locate_with_strpos(query)`. Note MySQL
   `LOCATE(substr, str)` has reversed argument order vs Postgres `strpos(str, substr)`,
   and the rewrite swaps them. **Caveat: `strpos` is case-*sensitive*** — that's a soft
   divergence (see `03`), not a crash.
3. **` REGEXP ` → ` ~* `**: `REGEXP_PATTERN.sub(" ~* ", query)`. `~*` is Postgres
   case-insensitive regex match, deliberately chosen to mirror MySQL's default
   case-insensitive collation.

```python
# All three are auto-translated on PG — NONE is a syntax break:
frappe.db.sql("SELECT name FROM `tabItem` WHERE item_name REGEXP %s", "^wid")
frappe.db.sql("SELECT locate('-', name) FROM `tabItem`")
```

Backtick identifiers, `locate(...)`, and ` REGEXP ` in raw SQL are **false positives**.

---

## 3. `.like()` / `["like", ...]` filters render as `ILIKE` on Postgres

MariaDB's default collation makes `LIKE` case-insensitive; Postgres `LIKE` is
case-sensitive. Frappe bridges this by converting `like` → `ilike` on Postgres in
**both** filter paths, so case-insensitive matching is the cross-db default.

- **Query-builder engine** (`frappe.qb.get_query`, behind `get_all`/`get_list`/`get_value`):
  `frappe/database/query.py` — `if (self.is_postgres and _operator.casefold() == "like"): operator_fn = OPERATOR_MAP["ilike"]`
- `OPERATOR_MAP["ilike"]` → `key.ilike(value)` — `frappe/database/operator_map.py`
- **Legacy `frappe.model.db_query`** (the `get_list`/report path): `frappe/database/db_query.py` —
  `if f.operator.lower() == "like" and frappe.conf.get("db_type") == "postgres": f.operator = "ilike"`
- Also patched at the pypika level: `patch_like_operators` in `frappe/query_builder/utils.py`
  makes `Term.like`/`not_like` render `ILIKE`/`NOT ILIKE` on Postgres.

```python
# Matches "Blue Widget" on BOTH MariaDB and Postgres — case-insensitive by design:
frappe.db.get_all("Item", filters={"item_name": ["like", "%widget%"]})
frappe.db.get_all("Item", filters=[["item_name", "like", "%WIDGET%"]])
```

A finding that says "`like` is case-sensitive on Postgres, must fix" is almost always a
**false positive** for these ORM/qb/`.like()` forms — the framework already emits `ILIKE`.
(Genuinely *raw* `LIKE` in `frappe.db.sql()` that you *intend* to be case-sensitive is the
only thing to scrutinise.)

---

## 4. Dict-aggregate `fields` (`{"SUM": "col", "as": "x"}`) — engine-agnostic

`frappe.db.get_all` / `get_list` accept aggregate functions expressed as a dict in
`fields`. The query engine maps the uppercase key to a pypika function class, which
renders correctly on every dialect.

- Mapping table `FUNCTION_MAPPING` — `frappe/database/query.py`: includes
  `SUM → functions.Sum`, `AVG → functions.Avg`, `MAX → functions.Max`,
  `MIN → functions.Min`, `COUNT → functions.Count` (also `ABS`, `EXTRACT`, `IFNULL`,
  `CONCAT`, `NULLIF`, …).
- Dispatch: `is_function_dict` detects a dict field; `parse_function` builds the pypika
  func and applies the `as` alias.

```python
# SELECT SUM(grand_total) AS total — identical SQL shape on MariaDB & Postgres
frappe.db.get_all("Sales Invoice",
    fields=[{"SUM": "grand_total", "as": "total"}], group_by="customer")
frappe.db.get_all("Sales Invoice", fields=[{"AVG": "grand_total", "as": "avg_total"}])
```

The key must be **uppercase**; an uppercase key not in the mapping raises "Unsupported
function or operator". Aggregate-dict `fields` are **not** a Postgres break.

(Aside: aggregate columns still need a correct `GROUP BY` — that part *is* the
developer's responsibility and *can* be a real Postgres issue. The dict syntax itself is fine.)

---

## 5. `frappe.db.has_index` / `frappe.db.get_column_index` have native Postgres impls

These look like they'd emit MariaDB-only `SHOW INDEX`, but each backend has its own
implementation; the framework dispatches to the right one.

- **Postgres `has_index`** — `frappe/database/postgres/database.py`: `SELECT 1 FROM pg_indexes WHERE tablename=%s and schemaname=%s and indexname=%s limit 1`.
- **Postgres `get_column_index`** — joins `pg_index`/`pg_class`/`pg_namespace`/`pg_attribute`,
  filtering on `i.indisunique` and `i.indnkeyatts = 1` (single-column leading index) —
  the cross-db counterpart of MariaDB's `SHOW INDEX … Seq_in_index = 1`.
- **MariaDB** equivalents: `frappe/database/mariadb/database.py` (`SHOW INDEX … WHERE Key_name=…`).

```python
# Works on both engines — dispatches to pg_indexes on Postgres:
if not frappe.db.has_index("tabSales Invoice", "customer_index"):
    frappe.db.add_index("Sales Invoice", ["customer"])
frappe.db.get_column_index("tabSales Invoice", "customer", unique=False)
```

Calls to `has_index` / `get_column_index` are **false positives**.

---

## Audit summary — treat these as false positives

| Construct | Why it's safe on Postgres | Where to grep |
|---|---|---|
| Raw `ifnull(...)` in `frappe.db.sql` | Rewritten to `coalesce(` on all engines | `IFNULL_PATTERN` in `database/database.py` |
| Backtick identifiers in raw SQL | `` ` `` → `"` on PG via `modify_query` | `modify_query` in `database/postgres/database.py` |
| `locate(a,b)` in raw SQL | → `strpos(b,a)` on PG (args swapped) | `replace_locate_with_strpos` |
| ` REGEXP ` in raw SQL | → ` ~* ` (case-insensitive) on PG | `REGEXP_PATTERN` |
| `["like", ...]` / `.like` filter | → `ILIKE` on PG (matches MariaDB collation) | `OPERATOR_MAP["ilike"]`, `patch_like_operators` |
| `fields=[{"SUM"/"AVG"/"MIN"/"MAX": "c", "as": "x"}]` | pypika func → standard SQL | `FUNCTION_MAPPING` in `database/query.py` |
| `has_index` / `get_column_index` | Native PG impls (`pg_indexes`/`pg_index`) | `database/postgres/database.py` |

**NOT covered by these auto-handlers (still audit):** non-grouped columns in an
aggregate `SELECT` (GROUP BY strictness), genuinely raw `LIKE`/`strpos` where case
matters, arbitrary tie-breaker / `ORDER BY` assumptions, and engine-specific functions
not in `FUNCTION_MAPPING` or `modify_query`. → see `02` and `03`.
