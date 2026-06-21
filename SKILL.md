---
name: frappe-postgres-migration
description: >-
  Migrate any Frappe/ERPNext-style app so its full server test suite passes
  identically on PostgreSQL and MariaDB from ONE codebase. Use this when porting
  a Frappe app to Postgres, auditing queries for cross-engine breakage or
  result-divergence, converting raw `frappe.db.sql` to `frappe.qb`/ORM, or
  setting up dual-engine (Postgres + MariaDB) test CI. App-agnostic — works for
  HRMS, Healthcare, Lending, custom apps, or ERPNext itself.
---

# Frappe App → PostgreSQL Parity Migration

A battle-tested playbook for making a MariaDB-first Frappe app run **identically**
on PostgreSQL. Distilled from a real, completed ERPNext→Postgres migration (~4,200
queries audited, ~60 PRs shipped). Every technical claim in the reference files is
cited to actual `frappe`/app source with `file:line`.

> This skill is **app-agnostic**. Examples cite real ERPNext files because that is
> where the patterns were proven; substitute your own app. **Line numbers drift
> between framework versions — grep for the named identifier (function/pattern),
> never trust a line number.**

---

## The one rule that governs everything

> **MariaDB behaviour is the contract and MUST NOT change. Postgres is bent to
> match MariaDB — never the reverse.**

A fix is acceptable only if, on MariaDB, the query returns *exactly what it returned
before*. Every fix is constructed so the MariaDB result is byte-identical and only
the Postgres result moves (toward MariaDB). If "fixing" Postgres would change what
MariaDB returns (e.g. adding a tiebreaker that flips MariaDB's pick), **do not fix
it — document it** as an accepted, undefined-order tie.

The proof of every fix is a test that runs on **both** engines and asserts the
concrete value: MariaDB-green proves you didn't regress; Postgres-green proves you
closed the gap.

---

## Why MariaDB-first apps break on Postgres (the mental model)

MariaDB is permissive; PostgreSQL is strict and standards-bound. Four failure classes:

1. **Hard breaks** — the query *errors* on Postgres but runs on MariaDB (MySQL-only
   functions, loose `GROUP BY`, `HAVING` on a SELECT alias, single-quoted aliases,
   `UPDATE…JOIN`, capital-cased identifiers, `set_value(<Check>, True)`, …). CI on
   MariaDB is green; Postgres raises an exception. → `references/02-hard-breaks.md`
2. **Silent divergences** — the query *succeeds on both* engines but returns
   **different rows/values** (case-sensitivity incl. *name* lookups, empty-string vs
   NULL, NULL ordering, non-unique `ORDER BY … LIMIT 1`, arbitrary `GROUP BY` picks,
   date→epoch timezone skew). The most dangerous class: nothing fails, MariaDB CI
   stays green, the bug only shows on a Postgres site. → `references/03-silent-divergences.md`
3. **Transaction-abort semantics** — Postgres aborts the *entire transaction* on any
   failed statement, so **catch-and-continue** code after an insert/update failure dies
   on the next statement with `InFailedSqlTransaction`. MariaDB doesn't. Now relevant
   because frappe dropped its blanket per-insert savepoint (frappe#40075) — callers must
   savepoint themselves. → `references/06-transaction-and-runtime.md`
4. **False positives** — constructs that *look* MySQL-specific but the framework
   transparently rewrites (`ifnull→coalesce`, backticks, `locate→strpos`,
   `REGEXP→~*`, `.like()→ILIKE`, dict-aggregate `fields`, `has_index`). "Fixing"
   these wastes effort and can *introduce* divergence. **Learn these first so you
   don't chase ghosts.** → `references/01-false-positives.md`

---

## The process — five phases, in order

### Phase 0 — Stand up the source of truth FIRST (before touching any query)

Do not change a single query until you can *reproduce a failure on Postgres* and
*prove a fix on both engines*. The source of truth is **the full app test suite,
run on a real Postgres instance, against upstream `frappe develop`** — not your
reading of the SQL, not a linter, not the suite on your existing MariaDB box.

- Create two local test sites: one `--db-type postgres`, one `--db-type mariadb`
  (both with `allow_tests: true`). A site is bound to one engine at creation.
- Establish the **MariaDB baseline green** — that is your no-regression contract.
- Wire a Postgres CI workflow that mirrors the MariaDB one (`DB: postgres`,
  **distinct job name**, label-gated, **NON-required**), building against clean
  `frappe develop`.

Full setup, exact `bench`/CI/`install.sh` commands, and the GitHub branch-protection
gotchas: **read `references/05-ci-harness.md`.**

### Phase 1 — Inventory (audit the whole app, drop the false positives)

Run the detection sweep over the **entire app, including `test_*.py`** (see Phase 3).
Then triage each hit against `references/01-false-positives.md` and discard the
framework-handled ones. One-shot grep sweep lives at the end of
`references/02-hard-breaks.md`; the per-pattern `grep` lines are in each reference.

Classify every real hit as **hard break** (02), **silent divergence** (03), or
**leave-it** (a documented, MariaDB-preserving non-fix).

### Phase 2 — The fix loop (per finding)

For each finding, this exact loop — never skip a step:

1. **Reproduce red on Postgres.** Run the touched module on the PG site; *see it
   fail* (error for a hard break, wrong assertion for a divergence). A fix you never
   saw fail is unverified.
2. **Fix it portably.** Reach for a `frappe.qb`/ORM construct from
   `references/04-portable-cookbook.md` — these render correctly on both engines.
   Prefer ORM over raw SQL.
3. **Green on Postgres.** Re-run the module on PG.
4. **Still green on MariaDB.** Re-run the module on MariaDB — this is the
   no-regression check. A fix that only passes on PG is **not done**.
5. **Ship a both-engine test** asserting the concrete value (not a tautology).
   If the change can move real numbers (a genuine MariaDB behaviour change), add a
   release-note line and get sign-off — per the governing rule, that should be rare.

Dead code that's flagged? **Remove it, don't fix it.**

### Phase 3 — Sweep the test files too (THE blind spot — non-negotiable)

The single mistake that produces false-green: source-only audits. Broken SQL hides
in `test_*.py` helpers, `setUp`/fixture code, and inline `frappe.db.sql` inside
tests — code that *only executes when the suite runs*. A source-only grep passes
while the suite explodes on a Postgres-invalid query in a test helper.

**Audit `test_*.py` with the same rigor as production code.** Run the *whole* suite
(`run-tests --app <app>`), not just per-module, against clean `frappe develop`.
In the reference migration the final harness commit was literally
`test(postgres): make test-helper SQL Postgres-valid across the suite`.

### Phase 4 — Promote CI to required (LAST)

Only once the **full** suite is reliably green on Postgres against `frappe develop`,
promote the Postgres check to a required status check on the base branch. Doing this
earlier hangs un-labelled PRs — see the branch-protection mechanics in
`references/05-ci-harness.md`.

---

## Decision rules (the judgement calls)

- **Loose `GROUP BY` column** — is it functionally dependent (one value per group,
  typically because the WHERE/joins pin it to one row)? Then add it to `GROUP BY`
  or wrap in `Max()`/`Min()` → identical on both engines, MariaDB unchanged. If it's
  *genuinely* multi-valued, MariaDB's old pick was undefined → widen the GROUP BY,
  pick a deterministic representative, or drop the column — and treat it as a
  behaviour change (rare, needs a note).
- **`ORDER BY … LIMIT 1` / `[0]` tie** — add a unique tiebreaker (`creation`/`name`)
  **only if it doesn't change MariaDB's current pick.** If the tied rows are equal on
  every column you read (the choice is invisible), or a tiebreaker would flip MariaDB,
  **leave it and document it.**
- **`.like()` / `["like", …]`** — already `ILIKE` on Postgres. **Not** a
  case-sensitivity bug. Don't touch it. (False positive — see 01.)
- **Raw `ifnull` / backticks / `locate` / `REGEXP` inside `frappe.db.sql()`** — auto
  rewritten. **Not** breaks. (False positives — see 01.) Everything *else* in a raw
  string is on you.
- **`except DuplicateEntryError` / `UniqueValidationError` that keeps going** — on
  Postgres the failed insert already aborted the txn. Safe only if it re-`throw`s (no DB
  call before the throw) or the insert used `ignore_if_duplicate=True` / `autoname="hash"`
  (→ `ON CONFLICT DO NOTHING`, no error). Otherwise wrap the insert in
  `frappe.db.savepoint(...)` + `rollback(save_point=...)`. See 06.
- **`set_value(<Check field>, True/False)`** — Check columns are integers; pass `1`/`0`,
  not Python bools (PG `DatatypeMismatch`). See 06.
- **Lower-casing a value used as a doc *name*** in `get_value`/`get_doc`/`exists` — names
  are case-sensitive on PG; keep original case for the lookup. See 06 §3.
- **A wall of same-shard failures** — likely one aborted-txn cascade, not many bugs.
  Reproduce the suspect test in isolation before "fixing" each. See 06 §6.
- **When unsure whether MariaDB changes** — write the both-engine test first. If
  MariaDB's asserted value moves, your fix is wrong for this codebase.

---

## Reference files — load on demand (do NOT read all at once)

| When you are… | Read |
|---|---|
| Triaging the audit / about to "fix" something that looks MySQL-only | `references/01-false-positives.md` |
| Resolving a query that **errors** on Postgres | `references/02-hard-breaks.md` |
| Chasing a query that returns **different results** on the two engines | `references/03-silent-divergences.md` |
| Writing the actual fix (portable `qb`/ORM recipes) | `references/04-portable-cookbook.md` |
| Setting up PG/MariaDB sites, CI, `install.sh`, branch protection | `references/05-ci-harness.md` |
| `InFailedSqlTransaction` / savepoints / `set_value(bool)` / name-case / TZ epochs / reading parallel CI logs | `references/06-transaction-and-runtime.md` |

---

## Definition of done

- [ ] Full app suite green on **Postgres** against clean `frappe develop`.
- [ ] Full app suite still green on **MariaDB** (no regression — the contract).
- [ ] Every fix shipped with a both-engine test asserting concrete values.
- [ ] Test-helper / fixture SQL swept, not just production code.
- [ ] Genuine MariaDB behaviour changes (rare) are documented with a release note.
- [ ] Postgres CI promoted to a required check — as the final step.
