# 03 — Silent divergences (query SUCCEEDS on both engines, returns DIFFERENT results)

> The dangerous class. The query runs successfully on both MariaDB and Postgres but
> **returns different rows/values**. Nothing fails; MariaDB CI stays green; the bug only
> shows on a Postgres site (or in a both-engine differential test). Examples cite real
> ERPNext files — substitute your app, grep for the construct not the line number.

## The governing rule

> **MariaDB behaviour is the contract and MUST NOT change. Postgres is bent to match
> MariaDB, never the reverse.**

A fix is acceptable only if, on MariaDB, the query returns *exactly what it returned
before*. Every fix below is constructed so the MariaDB result is byte-identical and only
Postgres moves (toward MariaDB). Ship each fix with a test that runs on **both** engines
and asserts the concrete value — that test proves MariaDB didn't move and Postgres now agrees.

**When to LEAVE IT:** if the only difference is row *order* among rows the query never
promised to order (an undefined-order tie), and adding a tiebreaker would change which row
MariaDB happens to pick, do **not** add the tiebreaker — document it. Changing MariaDB to
fix a cosmetic Postgres-order difference violates the rule.

---

## 1. Case sensitivity on text equality / IN

**How it diverges.** MariaDB's default collation (e.g. `utf8mb4_general_ci`) is
case-insensitive, so `col = 'abc'`, `col IN (...)`, `col LIKE '...'` match regardless of
case. Postgres compares `text` **case-sensitively**. So a raw `==`, `.isin(...)`, an ORM
`["in", ...]` filter, or a `Strpos`/`Locate` position search on a text column silently
misses differently-cased rows on Postgres that MariaDB returns.

**`.like()` is NOT divergent.** The query builder patches `Term.like`/`not_like` to render
`ILIKE`/`NOT ILIKE` on Postgres (`patch_like_operators` in `frappe/query_builder/utils.py`),
so `.like()` / `["like", ...]` is already case-insensitive on both engines. Do **not** "fix"
`.like()` sites — false positives (see `01`). Raw `frappe.db.sql` is partly covered: the PG
driver rewrites `REGEXP` → `~*` (case-insensitive) and `locate` → `strpos`. But `strpos`
itself is **case-sensitive**, and `==`/`IN` are untouched.

**Detect** (query-builder / ORM, NOT `.like` sites):
```bash
grep -rnE '\.isin\(|== *["'\'']|\["[a-z_]+", *"(in|=|!=)"|Strpos\(|Locate\(' <app> \
  --include='*.py' | grep -v '\.like\|Lower('
```
Then judge whether the column is free text the user may type in any case (serial no, batch
no, attribute value, email, code). `Locate`/`Strpos` without a surrounding `Lower()` is a
case-sensitive *ranking/position* divergence even when the row set is the same (it reorders
paginated autocomplete results).

**Fix — `Lower()` on BOTH sides.** Wrap the column and the literal (`Python .lower()` on the
literal). MariaDB was already case-insensitive so its result is unchanged; Postgres now matches.

```python
# Before (case-sensitive on PG):
.where((iva.attribute == attribute)
       & (iva.attribute_value.isin([cstr(v) for v in attribute_values])))
# After (Lower both sides; MariaDB unchanged, PG now matches):
.where((Lower(iva.attribute) == cstr(attribute).lower())
       & (Lower(iva.attribute_value).isin([cstr(v).lower() for v in attribute_values])))
```
Same shape for an `==` exact match, an `.isin([...])` list against a child table, an email
lookup (`Lower(contact.email_id) == sender.lower()`), and a `Strpos`/`Locate` relevance rank
where both args get wrapped. For an exact match keep `==` under `Lower()`; only widen to
`LIKE`/substring if the original was a substring match.

---

## 2. Empty string vs NULL (and `Concat_ws`)

**How it diverges.** Postgres stores a blank submitted value as `NULL` for many nullable
text columns; MariaDB stores it as `''`. They then behave differently in:
- **String concatenation:** `Concat_ws(sep, a, b, c)` *skips NULL arguments* but *includes
  empty strings*. A blank middle component → `'John  Doe'` (double sep) on MariaDB (`''`
  kept) but `'John Doe'` on Postgres (`NULL` skipped). The engines build different strings.
- **`= ''` filters:** on Postgres a stored-`NULL` column does not satisfy `col = ''`; on
  MariaDB it does. (The framework filter `["fieldname", "is", "not set"]` renders
  `ifnull(col,'')=''` on both engines — that abstraction is parity-safe; raw `col = ''` is not.)

**Detect:**
```bash
grep -rnE 'Concat_ws|concat_ws|= *["'\'']{2}|== *["'\'']{2}' <app> --include='*.py'
```
Flag any `Concat_ws`/`concat_ws` over user-editable, individually-nullable text parts, and
any raw `col = ''` / `col == ''`.

**Fix** (prefer the first):
- **Use a stored, already-assembled value** instead of rebuilding from parts. For a person's
  name use the stored `full_name`, not `concat_ws(first, middle, last)` — both engines then
  return the identical stored string. (In the reference migration, restoring `concat_ws` was
  explicitly rejected after confirming it diverges.)
- **`Coalesce(col, '')` each nullable argument** before concatenation so a NULL is treated as
  `''` (matching MariaDB's stored `''`).

For filters, replace raw `col = ''` with `["fieldname", "is", "not set"]` (renders
`ifnull(col,'')=''` on both engines) or `Coalesce(col, '') == ''` in the query builder.

---

## 3. NULL ordering (NULLs sort first vs last)

**How it diverges.** Default NULL placement in `ORDER BY` is opposite: MariaDB sorts `NULL`
**first** on ascending (last on descending); Postgres sorts `NULL` **last** on ascending
(first on descending). A nullable sort key puts NULL-keyed rows in a different position,
which changes the result whenever the query is paginated or reduced to one row
(`LIMIT 1`, `[0]`).

**Detect:**
```bash
grep -rnE '\.orderby\(|order_by *=' <app> --include='*.py'
```
For each, ask: is the sort column nullable, and does the query take a slice (limit/first row)
where NULL position matters?

**Fix — make NULL position explicit so Postgres reproduces MariaDB's placement.**
- **`Coalesce`/`IfNull` sentinel:** replace the bare key with `IfNull(col, <sentinel>)` where
  the sentinel forces the same position MariaDB used. For a NULL-first asc sort:
  `.orderby(IfNull(todo.date, "1000-01-01"))`. For a desc sort where undated rows should land
  at the bottom on both engines: `IfNull(cc.to_date, "0001-01-01")`.
- **`isnotnull()` guard for `LIMIT 1`:** if the intent is "the earliest non-NULL value",
  filter NULLs out before ordering so the slice can't pick a NULL-keyed row on either engine:
  `.where(comm.communication_date.isnotnull()).orderby(comm.communication_date).limit(1)`, and
  treat an empty result as `None` (`first_contact[0][0] if first_contact else None`).

Choose the sentinel/guard so that on MariaDB the picked row is the *same one it picked before*
— that constraint keeps MariaDB unchanged.

---

## 4. `ORDER BY ... LIMIT 1` (or `[0]`) without a unique tiebreaker

**How it diverges.** When the sort key is non-unique, the engine may return any tied row.
MariaDB and Postgres can break the tie differently (and `DISTINCT` + `ORDER BY` interacts with
Postgres's requirement that ordered columns appear in the select list). So a
`latest-by-date LIMIT 1` read can return a different row per engine when two rows share the date.

**Detect:** same grep as §3, focused on `.limit(1)` / `[0]` / `[0][0]` over a non-unique key
(a date, posting time, non-PK column).

**Fix — add a deterministic tiebreaker, but ONLY when it doesn't change MariaDB's pick.**
Append a unique-ish key (`creation`, `name`, the PK) so both engines converge:
```python
# latest SLE for a serial no, tied posting dates broken deterministically
.orderby(stock_ledger_entry.posting_datetime).orderby(stock_ledger_entry.creation)
# creation also added to SELECT so DISTINCT is PG-valid
```
Add `creation`/`name` to the select list when the query is `DISTINCT` (PG requires ORDER BY
columns in the select list under DISTINCT) — `creation`/PK is unique so it doesn't change the
distinct row set.

**When to LEAVE IT (document, don't fix).** If the rows are *fully equal on every column you
read* (the choice is invisible), or if adding a tiebreaker would make MariaDB return a
*different* row than today, do not add one — that would change MariaDB. Record it as an
accepted undefined-order tie. Litmus test: *does any consumer observe which tied row is
returned?* If no, it's cosmetic; leave it.

> In the reference migration, ~11 tiebreaker findings were **deliberately not fixed** because
> adding `.orderby(name)` changed MariaDB's pick and the engines already agreed (no parity
> benefit). One was even shipped then reverted once this rule was applied. Respect it.

---

## 5. `Max()`-wrapped arbitrary pick under `GROUP BY`

Arises when fixing the *errors-on-Postgres* loose-GROUP-BY case (`02` §1). A bare
non-aggregated column under `GROUP BY` is a `GroupingError` on Postgres but an arbitrary pick
on MariaDB. Wrapping it in `Max()`/`Min()` makes it Postgres-valid — but classify whether
that *changes MariaDB*:

- **Functionally dependent → ACCEPT.** If the wrapped column has exactly one value per group
  (one `account` per `mode_of_payment`; a child column grouped by the parent PK), `Max()`
  returns that single value — identical on both engines, MariaDB unchanged. The safe,
  preferred fix.
  ```python
  # one account per mode_of_payment -> Max is the only value
  .groupby(SalesInvoicePayment.mode_of_payment)
  .select(SalesInvoicePayment.mode_of_payment,
          fn.Max(SalesInvoicePayment.account).as_("account"),
          fn.Sum(SalesInvoicePayment.amount).as_("amount"))
  ```
- **Genuinely varying → DECIDE (this is a real behaviour change).** If the column can hold
  multiple values per group, MariaDB used to return an *arbitrary* (≈first) row and `Max()`
  now returns the lexical maximum — MariaDB's output changes for that duplicate edge case.
  Options, in order:
  1. **Add the column to `GROUP BY`** (one row per distinct value) when the row split is the
     correct semantics.
  2. **Pick a deterministic representative** (e.g. prefer the default/phantom row) when one
     row per group is required but "max" is wrong.
  3. **Drop the column** if it's dead on the grouped path.
  4. **Accept `Max()` and document it**, noting MariaDB's arbitrary→max edge-case change,
     *only* when the duplicate case is practically unreachable.

  Each behaviour change ships a both-engine test that fails on the old code, plus a release-note
  line if it can move real numbers (e.g. `Sum(qty) * <arbitrary rate>` → `Sum(qty * rate)`
  changes any multi-rate aggregate).

**Detect:**
```bash
grep -rnE '\.groupby\(|GROUP BY' <app> --include='*.py'
```
For each grouped query, list every selected column neither in the GROUP BY nor inside an
aggregate. Each is a Postgres error AND a potential MariaDB-behaviour decision. (MariaDB CI
will not catch these; run the touched path on a Postgres site.)

---

## 6. Case sensitivity in DB *name* lookups (sharper than §1)

A subtle variant of §1: lower-casing a value and then using it as a **document name** in
`get_value("Doctype", name, …)` / `get_doc` / `exists`. The `name` column is case-sensitive
on Postgres, so a lowercased name matches no row → `None` → wrong result (MariaDB's
case-insensitive name match hides it). **Keep original case for anything used as a
name/identifier in a lookup**; only lower-case the operands of explicit case-insensitive
comparisons. Detail + real example in `06` §3.

## 7. `UnixTimestamp(date)` / date→epoch is timezone-dependent

On Postgres a date's epoch is its midnight in the **DB session timezone**, which can be a day
ahead of the wall clock when the app TZ is ahead of UTC (MariaDB differs subtly too). A test
with a strict `epoch <= now` bound on date-derived values is flaky on PG. Allow tolerance
(e.g. `<= now + 86400`); MariaDB stays `<= now` so its result is unchanged. Detail in `06` §4.

## 8. `DISTINCT` + `ORDER BY` column ordering (frappe drops ORDER BY on PG; Python `sorted()` is case-sensitive)

Two compounding traps when a query produces an **ordered list of distinct text values** (e.g. the
dynamic account columns of a financial report):

1. **frappe silently drops `ORDER BY` for `distinct` queries on Postgres.** `db_query` (frappe)
   blanks `order_by` when `distinct=True and db_type=="postgres"` (Postgres requires DISTINCT
   ORDER-BY exprs to be in the SELECT list, and it sidesteps that by dropping the order). So
   `frappe.get_all(doctype, pluck="x", distinct=True, order_by="x")` is **ordered on MariaDB but
   arbitrary on Postgres** → a real cross-engine divergence whenever the list order is user-visible.
2. **Python `sorted()` is case-sensitive; MariaDB's default collation is not.** Replacing the SQL
   `ORDER BY <text>` with `sorted(list)` orders by raw Unicode codepoint (`'Z'`=0x5A before
   `'a'`=0x61), but MariaDB's `utf8mb4_*_ci` collation orders case-**insensitively**. So bare
   `sorted()` changes MariaDB's historical order.

**Fix — sort in Python with a casefold key, dropping the (ignored) `order_by`:**
```python
# Before — unordered on PG (order_by dropped for distinct):
accounts = frappe.get_all("Sales Invoice Item", filters={...}, pluck="income_account",
                          distinct=True, order_by="income_account")
# After — deterministic, case-insensitive, identical on MariaDB and Postgres:
accounts = sorted(
    frappe.get_all("Sales Invoice Item", filters={...}, pluck="income_account", distinct=True),
    key=str.casefold,
)
```
`key=str.casefold` reproduces MariaDB's collation order on both engines. Test with two
case-colliding values (e.g. `"aaa …"` and `"ZZZ …"`) and assert the casefold order; bare
`sorted()` fails it (`['ZZZ…','aaa…']`), the fix passes on both engines.

## 9. Number-suffix extraction: mirror MariaDB's `SUBSTRING_INDEX … AS UNSIGNED` exactly

A common name-deduplication idiom is `CAST(SUBSTRING_INDEX(name, ' ', -1) AS UNSIGNED)` — the
**leading digits of the last whitespace token** (`"X - 3a" → 3`, `"X - 1.5" → 1`, `"X - Foo" → 0`).
A Postgres rewrite that grabs the **pure trailing digits** (`regexp_replace(name,'^.*?(\d*)$','\1')`)
diverges: `"X - 3a"` → `''`→NULL→0 on PG but 3 on MariaDB, so the next generated number differs.
**Mirror MariaDB:** isolate the last token, then take its leading digits:
```python
last_token   = regexp_replace(name, r"^.*\s", "")          # drop up to the last whitespace
leading_nums = regexp_replace(last_token, r"^(\d*).*$", r"\1")
extracted    = nullif(leading_nums, "")                     # '' → NULL, skipped by MAX(), COALESCE→0
casted       = Cast(extracted, "INTEGER")
```
General rule: when an engine branch reimplements a string/number function, **diff the two against
literal rows on both live engines** (`SELECT <expr> FROM (VALUES …)`) before trusting it — a regex
that looks equivalent often isn't on the edge cases.

---

## Quick reference

| Divergence | Detect | MariaDB-preserving fix | Leave-it case |
|---|---|---|---|
| Case-sensitive `==`/`IN`/`isin`/`Strpos` on text | `==`/`.isin`/`Strpos`/`Locate` on free-text cols (skip `.like`) | `Lower()` both sides | n/a (`.like()` already ILIKE) |
| `Concat_ws` / `= ''` (empty vs NULL) | `Concat_ws`, raw `col=''` | stored full value, or `Coalesce(col,'')` per arg / `"is","not set"` filter | n/a |
| NULL ordering | `.orderby` on nullable key | `IfNull(col, sentinel)`; `isnotnull()` before `LIMIT 1` | n/a |
| `ORDER BY … LIMIT 1` non-unique | `.limit(1)`/`[0]` on non-unique key | add `creation`/`name`/PK tiebreaker (+ to SELECT under DISTINCT) | tied rows equal on read cols, or tiebreaker flips MariaDB |
| `Max()` arbitrary pick under GROUP BY | selected col not in GROUP BY nor aggregate | `Max()` if functionally-dependent; else widen GROUP BY / representative / drop | functionally-dependent (one value/group) → accept silently |
| `distinct` list order | `get_all(distinct=True, order_by=…)` / `SELECT DISTINCT … ORDER BY` | `sorted(get_all(distinct=True), key=str.casefold)` (PG drops the ORDER BY; bare `sorted` is case-sensitive) | order never user-visible |
| number-suffix extract | PG regex branch reimplementing `SUBSTRING_INDEX(name,' ',-1) AS UNSIGNED` | mirror exactly: last token → leading digits → `NULLIF` → `Cast` | n/a (diff both engines on literal rows first) |

Every fix lands with a both-engine test asserting the concrete value — that test is the proof
that MariaDB stayed put and Postgres caught up.
