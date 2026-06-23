# 02 — Hard Postgres breaks (query ERRORS on Postgres, runs on MariaDB)

> MariaDB is permissive; PostgreSQL is strict. Queries that *run* on MariaDB can
> *error* on Postgres. This is every **hard break** seen migrating a real Frappe app,
> with the Postgres error, a one-line grep to detect it, the portable fix, and why the
> fix leaves MariaDB output identical. Examples cite real ERPNext files — substitute
> your app; **grep for the construct, not the line number.**
>
> **Before treating any hit as a break, check `01-false-positives.md`.** Raw
> `ifnull`/`locate`/backticks/`REGEXP` inside `frappe.db.sql()` and `.like()` filters
> are auto-handled and are NOT breaks. The patterns below have **no** such rewrite.

---

## 1. Loose GROUP BY (non-grouped, non-aggregated column)

Selecting or ordering by a column neither in `GROUP BY` nor wrapped in an aggregate.
MariaDB arbitrary-picks a row; Postgres errors. Postgres infers functional dependency
only from the **PRIMARY KEY** — in Frappe that is `name`. Grouping by `name` lets you
select any column *of the same table*; it does **not** cross joins (a column from
another table still needs to be grouped/aggregated).

- **Postgres error:** `column "X" must appear in the GROUP BY clause or be used in an aggregate function`
- **Detect:** `grep -rn "\.groupby(" --include="*.py" .` then inspect each `.select(...)`
  for bare columns not in the groupby and not wrapped in `Sum/Max/Min/Count/Avg`. Raw:
  `grep -rniE "group by" --include="*.py" .`
- **Fix:** add the column to `GROUP BY`, **or** wrap it in `Max()`/`Min()` (single-valued
  per group), **or** group by `name` (PK) when every bare column belongs to that same table.
- **Why MariaDB-identical:** if the column is functionally dependent on the grouping keys
  (one distinct value per group — typical once WHERE/joins pin it to one row), adding it to
  `GROUP BY` or wrapping in `Max()` returns the same single value MariaDB was already picking.
  If genuinely multi-valued, MariaDB's old pick was undefined → pick a deterministic value
  (`Max`) and add a release note. (See the `Max()` decision rule in `03`, §5.)
- **⚠ Row-count trap — `Max()`-wrap, do NOT add a non-FD column to `GROUP BY`.** Adding a
  multi-valued column (the classic case: the **child/row PK**, or an editable per-row field) to
  `GROUP BY` makes each row its own group → **one group row becomes N → the MariaDB row count
  changes = a regression.** Only add a column to `GROUP BY` if it is FD on the key; otherwise
  `Max()`/`Min()`-wrap it (row count preserved, value arbitrary→deterministic).
- **Judge FD by the source table, not the column name.** `t3.x` from a **master joined on the
  group key** (`t1.key = t3.name`) is FD → safe in `GROUP BY`. A descriptive field on the
  **transaction** table (`t1.supplier_name`, `t1.territory` — fetched/editable, can differ across
  historical rows for the same key) is **not** FD even though it looks master-derived → `Max()`-wrap.

```python
# BEFORE (errors on PG: bom/bin columns not in GROUP BY)
.groupby(bom_item.item_code)
# AFTER
.groupby(bom_item.item_code, bom.quantity, bom_item.stock_qty, bin.actual_qty)
```

---

## 2. MySQL-only functions

These do not exist on Postgres. Inside `frappe.db.sql()` only
`ifnull/locate/REGEXP/backticks` are auto-rewritten; everything below is a hard break in
both raw SQL **and** `frappe.qb` (pypika emits the name verbatim).

| MySQL func | Postgres error | Portable replacement |
|---|---|---|
| `timestamp(date, time)` (2-arg) | `function timestamp(date, time) does not exist` | A precomputed datetime column if the doctype has one (e.g. SLE `posting_datetime`); generally `CombineDatetime(date_col, time_col)` from `frappe.query_builder.functions`. |
| `timediff(a, b)` | `function timediff(...) does not exist` | Read both datetimes, subtract in Python (`get_datetime(a) - get_datetime(b)`), or interval arithmetic. |
| `str_to_date(s, fmt)` | `function str_to_date(...) does not exist` | Parse in Python (`getdate()`/`get_datetime()`), pass a real date param. |
| `date_add` / `date_format` | `function date_format(...) does not exist` | Compute boundaries in Python (`get_first_day`, `get_last_day`, `add_days`) and filter with a `>=`/`<=` date **range** (also index-friendly). |
| `group_concat(col)` | `function group_concat(...) does not exist` | `GroupConcat` from `frappe.query_builder.functions` (renders `string_agg` on PG), or aggregate in Python. |
| `if(cond, a, b)` | `function if(...) does not exist` | `frappe.qb.Case().when(cond, a).else_(b)`; `SUM(IF(...))` → `Sum(Case().when(...).else_(0))`. |
| 2-arg `substring(col, regex)` (pypika `Substring` is start/length, not regex) | wrong result / error | `CustomFunction("regexp_replace", [...])` on PG (digit-extract via `^.*?(\d*)$` → `\1`), wrapped in `NULLIF(..., '')`; branch by `frappe.db.db_type`. |

- **Detect:** `grep -rniE "timestamp\(|timediff|str_to_date|date_format|date_add|group_concat|\bif\(" --include="*.py" .`
  and `grep -rn "Timestamp(\|Substring(" --include="*.py" .` for qb forms.
- **Why MariaDB-identical:** the replacement computes the same value. A precomputed
  datetime equals `timestamp(date, time)` by construction; a date range
  `>= first_day AND <= last_day` selects the exact same rows as
  `DATE_FORMAT(d,'%Y-%m')='...'`; `Case` is the literal definition of `IF`.

```python
# BEFORE: order by timestamp(posting_date, posting_time) desc   ← errors on PG
# AFTER:  order by posting_datetime desc                        ← precomputed col, same value

# BEFORE: query_builder.if_(cond, a, b)  /  raw "IF(cond, a, b)"
# AFTER:
query_builder.Case().when(pe.payment_type == "Receive", a).else_(b).as_("amount")
```

---

## 3. HAVING referencing a SELECT alias

Postgres evaluates `HAVING` before SELECT aliases exist, and rejects `HAVING` on a
non-aggregated column when there is no `GROUP BY`. MariaDB resolves the alias and allows it.

- **Postgres error:** `column "X" does not exist` (the alias) or `column must appear in the GROUP BY clause`
- **Detect:** `grep -rn "\.having(" --include="*.py" .`; raw: `grep -rniE "having" --include="*.py" .`
- **Fix:** if no aggregation, move the predicate to `WHERE` on the underlying expression.
  If aggregating, pass the **full expression** to `.having(expr)` rather than the alias.
- **Why MariaDB-identical:** the alias *is* the underlying expression; filtering the
  expression directly is the same computation, same rows.

```python
# BEFORE (PG rejects HAVING on a SELECT alias with no GROUP BY)
.having(frappe.qb.Field("unclaimed_amount") > 0)
# AFTER
.where((doctype.base_total - doctype.claimed_landed_cost_amount) > 0)
```

---

## 4. SELECT DISTINCT with ORDER BY expr not in the select list

`SELECT DISTINCT ... ORDER BY creation` where `creation` is not selected.

- **Postgres error:** `for SELECT DISTINCT, ORDER BY expressions must appear in select list`
- **Detect:** `grep -rn "\.distinct()" --include="*.py" .` then check `.orderby(...)`
  columns are all in `.select(...)`; raw: `grep -rniE "select distinct" --include="*.py" .`
- **Fix:** add the order-by column to the select list (drop it downstream if unwanted), or
  drop the unnecessary `DISTINCT`.
- **Why MariaDB-identical:** the result *set* is the same; adding the column to SELECT
  doesn't change which rows distinct produces, and the order was already determined by it.

```python
# BEFORE
.select(party.parent.as_("name"), party.supplier).distinct().orderby(party.creation, ...)
# AFTER  (creation now in the select list)
.select(party.parent.as_("name"), party.supplier, party.creation).distinct().orderby(party.creation, ...)
```

---

## 5. Single-quoted column alias `AS 'x'`

In Postgres single quotes are **string literals**; an alias must be unquoted or
double-quoted. `AS 'remarks'` is a syntax error. Most common in f-string-built raw SQL.

- **Postgres error:** `syntax error at or near "'remarks'"`
- **Detect:** `grep -rnE "\bas\s+'[^']+'" --include="*.py" .`
- **Fix:** use a bare alias (`as remarks`). The aliased function (`substr`, etc.) is portable.
- **Why MariaDB-identical:** dropping the quotes doesn't change the alias name or value;
  MariaDB accepts both.

```python
# BEFORE
select_fields += f",substr(remarks, 1, {remarks_length}) as 'remarks'"
# AFTER
select_fields += f",substr(remarks, 1, {remarks_length}) as remarks"
```

---

## 6. Integer division truncates on Postgres

`int / int` is integer division on Postgres (`1/4 → 0`); MariaDB returns a decimal. Not
an *error* but a silent wrong answer — scan for it alongside the breaks.

- **Symptom:** percentages/ratios truncate to 0 for any minority group; no exception.
  Also a **constant divisor** on an Int column — `manufacturing_time_in_mins / 1440` floors a
  lead-time to whole days on Postgres only (then `math.ceil` + `add_days` shift a date by a day).
- **Detect:** `grep -rnE "Count\(|count\(.*/|/.*\* ?100\b|/ ?[0-9]+\b|[0-9]+ ?/ ?[a-z_]+\." --include="*.py" .`
- **Fix:** force float before dividing — `* 100.0 / total` (multiply first), **float a literal**
  (`col / 1440` → `col / 1440.0`, or `1440 / col` → `1440.0 / col`), or `Cast(num, "DECIMAL") / den`.
  (SQL-level `/` only — Python `/` is already float.)
- **Why MariaDB-identical:** MariaDB already divided in decimal; multiplying by `100.0`
  makes Postgres do the same, and `x * 100.0 / y == x / y * 100` in decimal.

```python
# BEFORE: Round((Count(q.name).distinct() / total_quotations * 100), 2)   ← 1/4 → 0 on PG
# AFTER:  Round((Count(q.name).distinct() * 100.0 / total_quotations), 2)
```

---

## 7. Boolean strictness — bitwise `|` on varchar, and `OR <int>`

Postgres has no implicit cast from text/int to boolean and no `varchar | varchar` operator.

- pypika `a | b` is a **bitwise OR**, often mistakenly used where `COALESCE` was meant. On
  Postgres `varchar | varchar` → `operator does not exist`. On MariaDB it bitwise-ORs the
  integer coercions of the strings (usually `0`), silently wrong.
- `... OR <int>` or `WHERE <int_column>` treating an int as boolean →
  `argument of OR must be type boolean, not type integer`.

- **Postgres error:** `operator does not exist: character varying | character varying` /
  `argument of OR must be type boolean, not type integer`
- **Detect:** `grep -rnE "\) ?\| ?\(|\.\w+ ?\| ?\w+\.|\bor [0-9]" --include="*.py" .`
- **Fix:** for "first non-null" use `Coalesce(a, b)`, not `a | b`. For truthiness use a real
  predicate: `Coalesce(int_col, 0) != 0`.
- **Why MariaDB-identical:** the original *intent* was coalesce/boolean; `Coalesce` returns
  the first non-null exactly as MySQL's `COALESCE` did, and `!= 0` matches what MariaDB's
  implicit int-as-bool evaluated to.

```python
# BEFORE: (table.sales_invoice | child_table.sales_invoice).as_("sales_invoice")  ← bitwise OR
# AFTER:  Coalesce(table.sales_invoice, child_table.sales_invoice).as_("sales_invoice")
```

---

## 8. Capital-cased identifiers (columns are lowercase & case-sensitive on PG)

Frappe creates **lowercase** column names. MariaDB is case-insensitive on identifiers;
Postgres folds unquoted identifiers to lowercase but treats *quoted* ones case-sensitively
— and Frappe quotes them. So `"Status"`, `"Name"` in raw SQL, and capitalised field names
in `get_value`/`get_all`/`get_list`/`order_by`/`db_set` fail on Postgres. (DocType *names*
like `"Sales Invoice"` are fine — stored as-is in the `tab<DocType>` table name; this is
about **field/column** identifiers.)

- **Postgres error:** `column "Status" does not exist` (often hints `Perhaps you meant "status"`)
- **Detect:** `grep -rnE "(get_value|get_all|get_list|db_set|db_get_value|order_by)\b.*\"[A-Z]" --include="*.py" .`
- **Fix:** lowercase the field/column identifier to the real fieldname.
- **Why MariaDB-identical:** the lowercase name is the actual column; MariaDB resolved the
  capitalised form to the same column, so the value is unchanged.

```python
# BEFORE: po.db_set("Status", "On Hold")            /  get_value(dt, n, "Status")
# AFTER:  po.db_set("status", "On Hold")            /  get_value(dt, n, "status")
```

> This pattern is *rampant in test files* (`db_set("Status", ...)`, `get_value(..., "Name")`).
> See `SKILL.md` Phase 3 — sweep the test files.

---

## 9. UPDATE ... JOIN

Postgres has no `UPDATE t1 JOIN t2 ... SET ...` syntax (it uses `UPDATE ... FROM`, but
pypika's portable form doesn't emit it). MariaDB supports multi-table `UPDATE`.

- **Postgres error:** `syntax error at or near "JOIN"` (or near `SET`)
- **Detect:** `grep -rniE "update .* (inner |left )?join|\.update\(.*\.join\(" --include="*.py" .`
- **Fix:** restrict the updated table with a **correlated subquery** in `WHERE ... IN (SELECT ...)`
  instead of joining; build the `SET` with a `Case()` if it depends on per-row values.
- **Why MariaDB-identical:** the subquery selects exactly the rows the join would have matched
  (same predicate, now `parent IN (subquery)`), so the same rows get the same new values.

```python
# AFTER: no UPDATE...JOIN — scope variant rows via a subquery on the parent
variant_items = (frappe.qb.from_(item).select(item.name)
    .where(item.variant_of.isnotnull()).where(item.variant_of != ""))
(frappe.qb.update(iva).set(iva.attribute_value, case_expr)
    .where(iva.parent.isin(variant_items)).where(...)).run()
```

See the fuller correlated-UPDATE recipe in `04-portable-cookbook.md` §9.

---

## 10. f-string-interpolated raw SQL

Interpolating values/identifiers into a SQL string is a **SQL-injection risk** and a
portability hazard (hand-written dialect-specific SQL). Not always a *syntax* error, but it
routinely smuggles in MySQL-isms.

- **Detect:** `grep -rnE "frappe\.db\.sql\(\s*f\"|\.format\(|%\s*\(" --include="*.py" .` and `grep -rn 'f"""' --include="*.py" .`
- **Fix:** rewrite as parameterized `frappe.qb` (or `get_all` with `filters=`). If an
  **identifier** must be dynamic, whitelist it against a known set and `frappe.throw`
  otherwise — never interpolate it raw.
- **Why MariaDB-identical:** the qb query compiles to the same logical query on MariaDB;
  parameter binding changes only *how* the value reaches the DB, not the value.

```python
# BEFORE
cond = f"and ste.{subcontract_order_field} = '{subcontract_order}'"   # injection + raw SQL
# AFTER (qb + identifier whitelist)
if subcontract_order_field not in ("subcontracting_order", "subcontracting_inward_order"):
    frappe.throw(_("Invalid subcontract order field: {0}").format(subcontract_order_field))
query = query.where(ste[subcontract_order_field] == subcontract_order)
```

---

## 11. Raw MySQL DDL introspection (`SHOW INDEX`, `SHOW TABLES`, …)

`SHOW INDEX FROM tabFoo` and MySQL-only result keys (`Column_name`) don't exist on Postgres.

- **Postgres error:** `syntax error at or near "SHOW"` (and `KeyError: 'Column_name'`)
- **Detect:** `grep -rniE "show index|show tables|show columns|information_schema\.statistics|Column_name" --include="*.py" .`
- **Fix:** use the db-agnostic API — `frappe.db.has_index(table, index_name)` or
  `frappe.db.get_column_index(table, column, unique=...)` (see `01` §5, `04` §10).
- **Why MariaDB-identical:** the helpers introspect each engine's catalog and return the
  same boolean/index fact; the assertion logic is unchanged.

```python
# BEFORE: frappe.db.sql("show index from tabItem", as_dict=1) ... index.get("Column_name")
# AFTER:
if (frappe.db.get_column_index("tabItem", column, unique=False)
        or frappe.db.get_column_index("tabItem", column, unique=True)):
    expected_columns.discard(column)
```

---

## 12. `set_value(<Check field>, True/False)` — bool vs integer column

Frappe Check fields are `smallint`/`bigint`. `frappe.db.set_value(dt, dn, check_field, True)`
emits `SET check_field=true` (a boolean literal); Postgres rejects assigning a boolean to an
integer column (`DatatypeMismatch`). MariaDB coerces `true→1`. **Fix:** pass `1`/`0`. (ORM
`doc.field = True; doc.save()` is fine — docfield typing casts it.) Detail in `06`.

## 13. Caught insert error → aborted transaction (`InFailedSqlTransaction`)

A separate *category*, not a single bad statement: on Postgres a failed insert/update aborts
the **whole transaction**, so catch-and-continue code dies on the next statement. Frappe no
longer auto-savepoints inserts (frappe#40075). Full treatment, the safe/unsafe matrix, and
the savepoint fix are in **`references/06-transaction-and-runtime.md`**.

## 14. `.rlike()` / `RLIKE` — not auto-translated (unlike `REGEXP`)

Frappe rewrites ` REGEXP ` → ` ~* ` on Postgres but **not** `RLIKE` (the rewrite pattern
matches only the word `REGEXP`). pypika's `.rlike()` emits `RLIKE`, which Postgres has no
operator for → hard break. **Fix:** `.regexp()` (translated to `~*`) or `.like()`/`.ilike()`
for a simple prefix.

```python
# BEFORE: sp.partner_website.rlike("^http://")     ← RLIKE not translated, errors on PG
# AFTER:  sp.partner_website.like("http://%")       ← or .regexp("^http://")
```

## 15. `.like()` / `CAST` on a non-text column (`bigint ILIKE`, `CHAR` = `character(1)`)

Because `.like()` → `ILIKE`, applying it to a numeric/date column hits `bigint ILIKE text`
(`operator does not exist`). Cast to text first — but **`Cast_(col, "varchar")`, never
`Cast(col, "char")`**: on Postgres bare `CHAR` is `character(1)`, so `CAST(12 AS CHAR)` → `'1'`
(silently truncates multi-digit values). MariaDB coerces the int implicitly, so the cast is a
no-op there.

```python
# BEFORE: table.idx.like("12%")               ← bigint ILIKE on PG; also Cast(idx,"char") truncates to "1"
# AFTER:  Cast_(table.idx, "varchar").like("12%")
```

## 16. Aggregate with NO `GROUP BY` at all

A `Sum()`/`Count()` selected next to **bare** columns and no `.groupby()` anywhere: MariaDB
silently collapses every row into one arbitrary-valued row (usually a *wrong-output* bug there
too — see `03`), Postgres errors (`must appear in the GROUP BY clause`). Add the intended
`.groupby(...)` (this is the same trap as §1 with the `GROUP BY` simply omitted).

## 17. `qb.update(dt).set(<Check field>, True/False)` / `get_all(fields=["CapitalCase"])`

Two more shapes of §8 and §12: a Python bool into a Check column via the **query builder**
`qb.update(dt).set(check_field, False)` (not just `set_value`/`db_set`) → pass `1`/`0`; and a
capital-cased fieldname in `get_all(dt, fields=["Account"])` (not just `get_value`) → Postgres
quotes it case-sensitively (`column "Account" does not exist`), use the stored lower-case name.

---

## 18. `IfNull`/`Coalesce` of a typed column with a different-typed literal

`IfNull(asset.disposal_date, 0)` renders `COALESCE("disposal_date", 0)` -- coalescing a DATE
with an integer. Postgres requires `COALESCE` args to share a type and raises `DatatypeMismatch:
COALESCE types date and integer cannot be matched`; MariaDB's `IFNULL` is permissive. The common
shape is a presence test `IfNull(date_col, 0) != 0 / == 0` -> replace with
`date_col.isnotnull()` / `date_col.isnull()` (identical, valid on both). Otherwise coalesce to a
**same-type** default (`Coalesce(date_col, '1900-01-01')`, `Coalesce(text_col, '')`). Numeric
`IfNull(int_or_currency_col, 0)` is fine -- only a *type mismatch* (date/text vs int) breaks.

## One-shot detection sweep

Run from the app root **(including `test_*.py`)** before any Postgres cutover:

```bash
# loose GROUP BY / DISTINCT / HAVING (review each hit's select list)
grep -rn "\.groupby(\|\.having(\|\.distinct()" --include="*.py" .
# MySQL-only functions (raw + qb forms)
grep -rniE "timestamp\(|timediff|str_to_date|date_format|date_add|group_concat|\bif\(|Timestamp\(|Substring\(" --include="*.py" .
# single-quoted alias / bitwise-OR / int-as-bool
grep -rnE "\bas\s+'[^']+'|\) ?\| ?\(|\bor [0-9]" --include="*.py" .
# capital-cased field identifiers in ORM calls
grep -rnE "(get_value|get_all|get_list|db_set|order_by)\b.*\"[A-Z]" --include="*.py" .
# UPDATE...JOIN, f-string SQL, MySQL DDL introspection
grep -rniE "update .*join|frappe\.db\.sql\(\s*f\"|show index|show tables|Column_name" --include="*.py" .
# .rlike()/RLIKE (not translated) and Cast-to-char (character(1) truncation) and .like() on idx/int cols
grep -rniE "\.rlike\(|\brlike\b|cast\([^)]*as char\b|Cast\([^,]+, ?[\"']char[\"']|\.(idx|docstatus)\.i?like\(" --include="*.py" .
```

Every fix is verifiable the way the source migration was: ship a test that runs the touched
path and **asserts concrete values on both engines**. Reserve a release note for the rare
loose-GROUP-BY case where the grouped column was *genuinely* multi-valued (MariaDB's old
arbitrary pick changes to a deterministic `Max`/`Min`).
