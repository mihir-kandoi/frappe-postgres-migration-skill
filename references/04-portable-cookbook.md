# 04 — Portable query cookbook (renders correctly on MariaDB *and* Postgres)

> Raw SQL with MySQL-isms (backticks, `IFNULL`, `TIMESTAMP(date, time)`, `UPDATE … JOIN`,
> `LOWER`-less `=`) silently passes on MariaDB and breaks on Postgres. The constructs below
> render correct SQL on **both** engines because Frappe's query builder maps them per-backend.
> Import paths verified against `frappe/query_builder/functions.py` + pypika. **Prefer ORM
> over raw SQL; grep for the identifier, not the line number.**

## Import cheat-sheet

```python
# Aggregates, scalar functions, NULL handling, casts, datetime — all from .functions
from frappe.query_builder.functions import (
    IfNull, Coalesce,           # NULL handling
    Sum, Count, Max, Min, Avg,  # aggregates
    Cast, Cast_,                # casts (see §4 for which to use)
    Lower, Upper,               # case folding
    CombineDatetime,            # date+time → timestamp
)
# Case, table/column refs, ordering — from the package root
from frappe.query_builder import Case, DocType, Field, Criterion, Order, Interval
```

`functions.py` re-exports all pypika scalar/aggregate functions (`from pypika.functions import *`),
then adds Frappe backend-aware wrappers (`CombineDatetime`, `Cast_`, `Locate`, `DateFormat`,
`DateDiff`, `UnixTimestamp`, …). So `Sum/Count/Max/Min/Avg/Coalesce/IfNull/Cast/Lower` are all
importable from `frappe.query_builder.functions`.

> Smoke test on the target install:
> `python -c "from frappe.query_builder.functions import Cast, Cast_, Coalesce, IfNull, Sum, Count, Max, Min, Lower, Avg, CombineDatetime; from frappe.query_builder import Case, DocType, Order, Field, Criterion, Interval"`

---

### 1. IfNull / Coalesce

```python
# BEFORE — raw MySQL-ism
frappe.db.sql("SELECT IFNULL(amount, 0) FROM `tabSales Invoice`")
# AFTER — pick either; both render correctly per-backend
from frappe.query_builder.functions import IfNull, Coalesce
si = frappe.qb.DocType("Sales Invoice")
frappe.qb.from_(si).select(IfNull(si.amount, 0))     # IFNULL on MariaDB, COALESCE on PG
frappe.qb.from_(si).select(Coalesce(si.amount, 0))   # COALESCE on both
```
Even raw `IFNULL` inside `frappe.db.sql` is auto-rewritten to `coalesce` (see `01` §1) — but
prefer the builder. `Coalesce` accepts multiple fallbacks: `Coalesce(a, b, 0)`.

### 2. Case().when().else_()

`else_` has a trailing underscore; `when(criterion, term)`.
```python
from frappe.query_builder import Case
sle = frappe.qb.DocType("Stock Ledger Entry")
direction = Case().when(sle.actual_qty < 0, "out").else_("in")
frappe.qb.from_(sle).select(direction.as_("direction"))
```

### 3. CombineDatetime (and the literal-vs-column caveat)

`TIMESTAMP(date, time)` is MySQL-only. `CombineDatetime` maps to `TIMESTAMP(...)` on MariaDB
and a cast-and-add expression on Postgres.
```python
from frappe.query_builder.functions import CombineDatetime
sle = frappe.qb.DocType("Stock Ledger Entry")
frappe.qb.from_(sle).orderby(CombineDatetime(sle.posting_date, sle.posting_time))
```
**Literal-vs-column caveat:** on Postgres the wrapper casts **string** operands
(`Cast(x, "date")`/`Cast(x, "time")`) but leaves **column references** untouched, relying on the
column already being `date`/`time` typed. So:
- Prefer **columns** for both operands: `CombineDatetime(tab.posting_date, tab.posting_time)`.
- If one side must be a literal datetime, compute it in **Python** and compare against the
  column expression — e.g.
  `CombineDatetime(riv.posting_date, riv.posting_time) > get_combine_datetime(self.posting_date, self.posting_time)`.
- On a column it preserves NULL semantics (rows with NULL `posting_time` stay excluded by `>`)
  identically on both engines.

### 4. Cast

`CAST(x AS VARCHAR)` fails on MariaDB (no `VARCHAR` cast target). `Cast_` special-cases this:
on MariaDB a varchar cast becomes `CONCAT(value, '')`; otherwise a normal `CAST`.
```python
from frappe.query_builder.functions import Cast_, Cast
ip = frappe.qb.DocType("Item Price")
frappe.qb.from_(ip).select(Cast_(ip.price_list_rate, "varchar"))  # string target → Cast_
frappe.qb.from_(ip).select(Cast(ip.valid_from, "date"))           # numeric/date → Cast is fine
```
Rule of thumb: **string/varchar target → `Cast_`**; numeric/date target → either works,
`Cast_` is always safe.

### 5. Lower (case-insensitive matching)

For identity/code matches that must behave like MariaDB's case-insensitive collation, fold
both sides:
```python
from frappe.query_builder.functions import Lower
frappe.qb.from_(sn).where(Lower(sn.serial_no) == serial_no.lower())  # identical on both engines
```
`.like()` already renders `ILIKE` on PG, so wildcard searches are already case-insensitive —
`Lower` is mainly for `==` and `.isin()` on identifier-like columns. (Detail in `03` §1.)

### 6. Sum / Count / Max / Min / Avg

```python
from frappe.query_builder.functions import Sum, Count, Max, Min
si = frappe.qb.DocType("Sales Invoice")
(frappe.qb.from_(si)
    .select(si.customer, Sum(si.grand_total), Count("*"), Max(si.posting_date))
    .groupby(si.customer))
```
GROUP BY caveat (the usual reason these break, not a function issue): every non-aggregated
select column must appear in `GROUP BY` on Postgres — add it to `groupby(...)` or wrap a
genuinely-arbitrary column in `Max(...)`/`Min(...)`. See `02` §1 and `03` §5.

### 7. get_all dict-aggregate `{"SUM": ...}`

```python
# Fully portable, no raw SQL
rows = frappe.get_all("Sales Invoice",
    fields=[{"SUM": "grand_total", "as": "total"}, "customer"],
    group_by="customer")
```
Key must be **uppercase** (`"SUM"`, `"MAX"`, …); unknown uppercase key raises "Unsupported
function or operator". (See `01` §4.)

### 8. limit_page_length=0 to avoid the silent 20-row cap

`get_list` defaults to **20** rows. `frappe.get_all` already forces `limit_page_length=0`
(unbounded) when you don't pass one. Any path through `db_query` with a truthy small page
length silently truncates.
```python
# AFTER — explicit unbounded fetch
rows = frappe.get_list("Sales Invoice", filters={"docstatus": 1},
    fields=["name", "grand_total"], limit_page_length=0)
```
`0` (and `None`) mean "no LIMIT"; any positive number caps. Prefer `frappe.get_all` for
internal/programmatic reads (unbounded + no permission filtering); reserve `get_list`
(20-cap + permissions) for user-facing reads. This is not a Postgres issue per se, but a
silent-undercount trap migrators hit when converting raw `SELECT … ` (no LIMIT) to ORM.

### 9. Correlated UPDATE instead of UPDATE ... JOIN

`UPDATE a JOIN b ON … SET a.x = b.y` is MySQL syntax; Postgres uses `UPDATE … FROM` and won't
accept the `JOIN` form. Write a **correlated subquery** in the `SET`, accepted by both:
```python
sii = frappe.qb.DocType("Sales Invoice Item")
si  = frappe.qb.DocType("Sales Invoice")
(frappe.qb.update(sii)
    .set(sii.customer, frappe.qb.from_(si).select(si.customer).where(si.name == sii.parent))
    .where(frappe.qb.from_(si).select(si.name)
           .where((si.name == sii.parent) & (si.docstatus == 1)).exists())
).run()
```
For the common "copy one column from the parent" case with few rows, doing it in Python
(`for row in frappe.get_all(...): frappe.db.set_value(...)`) is simplest and fully portable.

### 10. frappe.db.has_index / get_column_index

Both implemented per-engine — call the wrapper instead of `information_schema`/`pg_indexes`/`SHOW INDEX`.
```python
if not frappe.db.has_index("tabSales Invoice", "posting_date_index"):
    frappe.db.add_index("Sales Invoice", ["posting_date"])
idx = frappe.db.get_column_index("tabSales Invoice", "customer", unique=False)  # frappe._dict | None
```
`has_index(table_name, index_name)` — pass the physical `tab...` name.

### 11. frappe.db.delete / exists / count / get_value

All builder-backed and engine-agnostic — use instead of raw `DELETE`/`SELECT 1`/`SELECT COUNT(*)`.
```python
frappe.db.delete("Sales Invoice Item", {"parent": invoice_name})    # portable DELETE (no doc hooks)
frappe.db.exists("User", "jane@example.org", cache=True)            # name or None
frappe.db.exists({"doctype": "User", "full_name": "Jane Doe"})      # dict-of-filters form
frappe.db.count("Sales Invoice", filters={"docstatus": 1})         # COUNT(*) (distinct=True default)
total = frappe.db.get_value("Sales Invoice", invoice_name, "grand_total")
cust, total = frappe.db.get_value("Sales Invoice", invoice_name, ["customer", "grand_total"])
row = frappe.db.get_value("Sales Invoice", {"name": invoice_name}, ["customer", "grand_total"], as_dict=True)
```
`exists()` never raises on a missing table/column. `count()` defaults `distinct=True` (pass
`distinct=False` for a plain row count). `delete()` does **not** fire DocType hooks.

---

## Quick "does this break on Postgres?" checklist

- Backticks, `IFNULL`, `TIMESTAMP(d, t)`, `CAST(… AS VARCHAR)`, `UPDATE … JOIN`, `SHOW INDEX`,
  `GROUP_CONCAT`, `REGEXP`, `= 'literal'` on identifier columns → rewrite with the builder above.
- `frappe.db.sql()` already auto-rewrites `ifnull→coalesce` (all engines) and
  `backtick`/`locate`/`REGEXP` on Postgres → those specific tokens inside `db.sql` are
  **false positives** (see `01`); everything else in a raw string is on you.
- Loose `GROUP BY` (non-aggregated select column missing from GROUP BY) → add to `groupby` or
  wrap in `Max`/`Min`.
- Relying on `get_list`'s 20-row default for aggregation/loops → pass `limit_page_length=0`
  or use `frappe.get_all`.
