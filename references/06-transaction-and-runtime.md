# 06 — Transaction-abort semantics, and runtime lessons learned running it for real

> This file captures patterns that only surfaced once the **full suite ran on a real
> Postgres site against clean upstream `frappe develop`** — the kind of thing source
> reading and per-module runs miss. The headline is a whole failure *category* that
> isn't a single-statement error: **Postgres aborts the entire transaction on any
> failed statement.** Examples cite a real ERPNext migration; grep the construct, not
> the line number.

---

## 1. Transaction-abort on a caught insert/update error (THE big one)

**The rule.** On Postgres, once *any* statement errors inside a transaction, the whole
transaction enters an **aborted** state: every subsequent statement fails with
`InFailedSqlTransaction: current transaction is aborted, commands ignored until end of
transaction block`, until you `ROLLBACK` (or roll back to a savepoint). MariaDB does
*not* do this — a failed statement leaves the transaction usable.

So this **swallow-and-continue** shape — fine on MariaDB — breaks on Postgres:

```python
for row in rows:
    try:
        doc.insert()          # raises on a duplicate / constraint violation
    except frappe.UniqueValidationError:
        frappe.msgprint(...)  # swallow and keep going...
    # next iteration's doc.insert() -> InFailedSqlTransaction on Postgres
```

**Why this is newly dangerous (the framework contract changed).** Frappe *used* to wrap
every insert/update in an implicit savepoint, so a failed insert auto-recovered. That
blanket savepoint was **removed** (frappe#40075, "drop blanket savepoint from
db_insert/db_update") because it tripled `db_update`'s query count. The framework now
expects **callers that catch-and-continue to manage their own savepoint**. If your app
was last green against an *older* frappe, this regresses the moment you build against a
frappe that has the removal — verify your frappe version.

**Detect.**
```bash
# catch-and-continue around an insert/save/submit (the dangerous shape)
grep -rnE "except .*(UniqueValidationError|DuplicateEntryError|IntegrityError)" --include="*.py" <app>
```
Then read each: does it **re-raise**, or **swallow and keep doing DB work**?

**Is a given site actually unsafe? Use this matrix:**

| Situation | Aborts txn on PG? | Safe to catch-and-continue? |
|---|---|---|
| `except …: frappe.throw(...)` (re-raise, no DB call in between) | yes, but request then `ROLLBACK`s | **safe** — nothing runs a new stmt in the aborted txn |
| `except …: x = frappe.db.get_value(...); frappe.throw(x)` | yes | **UNSAFE** — the `get_value` runs in the aborted txn and dies |
| `insert(ignore_if_duplicate=True)` on a **name** collision | no — frappe emits `ON CONFLICT (name) DO NOTHING` (a no-op, no error) | **safe** |
| doctype with `autoname="hash"` | no — also gets `ON CONFLICT (name) DO NOTHING` | **safe** |
| plain `insert()` / `submit()`, **name** collision, no `ignore_if_duplicate` | **yes** — real duplicate-PK error → `DuplicateEntryError` | **UNSAFE** if it continues |
| plain insert, **non-name** unique index violation | **yes** — `UniqueValidationError` | **UNSAFE** if it continues |

Key subtlety: **`DuplicateEntryError` is not automatically pre-DB.** It is only a no-op
(no abort) when `ignore_if_duplicate=True` *or* `autoname=="hash"` (which trigger
`ON CONFLICT (name) DO NOTHING`). A *plain* insert with a name collision is a real failed
statement → the txn is aborted *and then* `DuplicateEntryError` is raised. So an
`except DuplicateEntryError:` block, when it fires on a plain insert, is already sitting
on an aborted transaction.

**Fix — wrap the fallible insert in a savepoint, roll back to it in the handler.** This is
the pattern frappe#40075 prescribes (and the same one `_create_bin` already uses):

```python
for row in rows:
    frappe.db.savepoint("row_insert")
    try:
        doc.insert()
        results.append(doc.name)
    except frappe.UniqueValidationError:
        frappe.db.rollback(save_point="row_insert")   # preserve transaction on postgres
        frappe.msgprint(...)
```

A constant savepoint name reused each iteration is fine (re-declaring shadows the prior
one). On MariaDB, taking a savepoint and rolling back a no-op failed insert changes
nothing — output is identical.

**There is also a contextmanager** — `from frappe.database.database import savepoint` →
`with savepoint(catch=frappe.DuplicateError): doc.insert()`. It rolls back to the
savepoint and **swallows** the caught exception. That's perfect for production
swallow-and-continue, but it is **wrong for a test that asserts the error** — see §5.

---

## 2. `set_value(<Check field>, True)` — bool vs smallint

Frappe **Check** fields are `smallint`/`bigint` columns. `frappe.db.set_value(dt, dn,
check_field, True)` renders `SET check_field=true` — a *boolean* literal. Postgres rejects
assigning a boolean to an integer column: `DatatypeMismatch: column "x" is of type
smallint but expression is of type boolean`. MariaDB silently coerces `true→1`.

This is the **direct-DB** path; the ORM path (`doc.field = True; doc.save()`) is safe
because docfield typing casts it. So the bug is specifically `frappe.db.set_value` /
`db_set` with a Python `bool` on a Check field.

- **Detect:** `grep -rnE "set_value\([^)]*, *(True|False)\)|db_set\([^)]*, *(True|False)\)" --include="*.py" <app>`
- **Fix:** pass `1`/`0`, not `True`/`False`.
- **MariaDB-identical:** it already stored `True` as `1`.

---

## 3. Case sensitivity in DB *name/identifier* lookups (not just `=`/`IN`)

`03-silent-divergences.md` covers case-sensitive `==`/`IN` on text. A sharper, easy-to-miss
variant: **lower-casing a value and then using it as a document name** in a lookup.

```python
# BUG (works on MariaDB, wrong on Postgres):
item = item.lower()
... frappe.db.get_value("Item", item, "variant_of") ...   # name lookup with a lowercased name
```

`get_value("Item", name, ...)`/`get_doc("Item", name)` resolve by the **`name`** column,
which is **case-sensitive on Postgres**. A lowercased name matches no row → `None` → wrong
behaviour (in the real case, a variant's Work Order was rejected because its template BOM
"didn't belong"). MariaDB's case-insensitive name match hid it.

- **Fix:** keep the **original case** for anything used as a name/identifier in a DB
  lookup; only lower-case the *operands of explicit case-insensitive comparisons*.
- **Detect:** look for `x = x.lower()` (or `cstr(...).lower()`) whose result is then passed
  as the `name`/filter to `get_value`/`get_doc`/`exists`/`db.get_value`.

---

## 4. `UnixTimestamp(date)` / date→epoch is timezone-dependent

`UnixTimestamp(posting_date)` on Postgres compiles to roughly
`CAST(EXTRACT(EPOCH FROM posting_date) AS BIGINT)` — the **midnight epoch in the DB session
timezone**. When the app/DB timezone is *ahead of UTC*, that epoch for a `today()` row can
land a little **ahead of** the Python `time.time()` wall-clock instant. MariaDB's
`UNIX_TIMESTAMP` differs subtly too.

Symptom: a test asserting a strict `timestamp <= now` upper bound on date-derived epochs is
**flaky/failing on Postgres** (passes when the CI runner happens to be UTC).

- **Fix (test):** allow a day of slack on the bound (`<= now + 86400`). MariaDB's value
  stays `<= now`, so its pass/fail is unchanged.
- **General lesson:** any cross-engine comparison of *date→epoch* values needs tolerance;
  the two engines don't agree to the second.

---

## 5. Savepoints in tests that *assert* the error

A test often wants to **both** assert an insert raises **and** keep the transaction usable
for later assertions:

```python
frappe.db.savepoint("dup")
with self.assertRaises(frappe.UniqueValidationError):
    dup_doc.insert()
frappe.db.rollback(save_point="dup")   # preserve transaction on postgres
# ... more assertions that hit the DB ...
```

Use the **manual** savepoint+rollback here — **not** the `savepoint(catch=...)`
contextmanager. The contextmanager *swallows* the exception, which would defeat
`assertRaises`; and if `assertRaises` consumes the exception first, the contextmanager sees
no exception, *releases* (doesn't roll back), and the txn stays aborted. Manual
savepoint+explicit rollback is the only shape that satisfies both needs.

---

## 6. Runtime & process lessons (what made failures visible — or invisible)

- **The framework version is part of your contract.** A frappe change (e.g. #40075
  removing the blanket savepoint) can *withdraw a safety net* your app relied on. Build and
  test against the **exact** frappe your users will run (clean `develop`), not a local
  frappe carrying compatibility shims. A green suite against a patched frappe lies.
- **Bring Redis up before running tests.** Without the bench's `redis_cache`/`redis_queue`
  running, `frappe.cache.lpush` in `global_search` raises `ConnectionError` and tests assert
  `Should not fail silently in tests` — a local-env red herring unrelated to Postgres.
  `redis-server config/redis_cache.conf --daemonize yes` (and `redis_queue.conf`).
- **Cascade failures are one bug, not many.** When a test aborts the transaction on
  Postgres, *every later test in the same shard* can fail with `InFailedSqlTransaction`.
  Don't triage each red as independent — find the test that first poisoned the txn; fixing
  it (or its savepoint) clears the cascade. (Reproduce the suspect test **in isolation** to
  tell a real failure from a cascade victim.)
- **Reading parallel-test CI logs.** `run-parallel-tests` output doesn't print the tidy
  unittest `Ran N / FAILED` summary. To list the real failures, grep the tracebacks for the
  test identity: `grep -oE "<app\\.[A-Za-z0-9_.]+ testMethod=test_[A-Za-z0-9_]+>"`. A wall of
  same-timestamp `✖` marks usually means a class `setUpClass`/`setUp` failure cascaded.
- **Reconcile a long-lived porting branch comprehensively, once.** If PG fixes live on a
  staging branch and are ported to `develop` piecemeal (only the files that failed this CI
  round), you *will* miss fixes in files that haven't failed yet. Do a full
  `git diff <develop> <staging>` (filter to PG-fix markers: `savepoint`, `Lower(`/`.lower()`,
  `CombineDatetime`/`timestamp(`, `db_type`, `pg_index`/`SHOW INDEX`, `on conflict`,
  `groupby`, `ILIKE`) and compare each file's two versions, rather than discovering gaps one
  CI round at a time.
- **The payments (prerequisite-app) gotcha.** Fetching a prerequisite app into the bench
  (`bench get-app payments`) does **not** install it on the test *site*. The site installs
  only what's in its `site_config` `install_apps`. If the Postgres CI site_config lists only
  your app but the MariaDB one lists `["payments", "app"]`, prerequisite-app tables are
  absent on PG (`relation "tabPayment Gateway" does not exist`). Keep both engines'
  `install_apps` in sync.

---

## Index-introspection nuance (supplement to the cookbook)

`frappe.db.get_column_index(table, field, unique=...)` only finds a **single-column unique**
index on Postgres (it filters `indisunique AND indnkeyatts = 1`). It does **not** answer
"is this field the *leading column* of *some* (possibly composite) index" — which is what
`SHOW INDEX … Seq_in_index = 1` checks. For that, branch by engine:

```python
if frappe.db.db_type == "postgres":
    frappe.db.sql("""
        SELECT 1 FROM pg_index i
        JOIN pg_class t ON t.oid = i.indrelid
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = i.indkey[0]
        WHERE t.relname = %(table)s AND a.attname = %(field)s LIMIT 1
    """, {"table": table, "field": field})
else:
    frappe.db.sql(f"SHOW INDEX FROM `{table}` WHERE Column_name = %s AND Seq_in_index = 1", field)
```
(`i.indkey[0]` is the first key column; this matches `Seq_in_index = 1` semantics.)
