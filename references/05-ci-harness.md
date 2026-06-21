# 05 — Set up the source of truth FIRST (the dual-engine validation harness)

> Before you change a single query, build the thing that tells you whether a query is correct
> on the target engine. The source of truth is **the full app test suite, run on a real
> Postgres instance, against upstream `frappe develop`** — not your reading of the SQL, not a
> linter, not the suite on your existing MariaDB box. You fix nothing until you can *reproduce*
> a failure on the target engine and then *prove* the fix passes on both. Examples cite a real
> ERPNext CI setup — adapt names/apps to yours.

---

## 1. Two local benches, one per engine — run the full suite on both

A Frappe site is bound to one DB engine (`db_type` in `sites/<site>/site_config.json`), fixed
at creation. Keep two test sites side by side and check every fix on both.

```bash
# From the bench root. Postgres test site:
bench new-site pg.localhost \
  --db-type postgres \
  --db-host 127.0.0.1 --db-port 5432 \
  --db-root-username postgres --db-root-password <pg_root_pw> \
  --admin-password admin
bench --site pg.localhost install-app <app>      # plus prerequisite apps (e.g. payments)

# MariaDB test site:
bench new-site mariadb.localhost \
  --db-type mariadb \
  --db-root-password <maria_root_pw> \
  --admin-password admin
bench --site mariadb.localhost install-app <app>
```

A site is test-runnable only if `allow_tests` is set; the CI site_config sets
`"allow_tests": true`, so set it on your local sites too. Confirm engine with
`grep db_type sites/<site>/site_config.json`.

**Run the WHOLE app suite on each engine** (the gate; `--lightmode` skips slow/optional bits
but still runs the suite):
```bash
bench --site pg.localhost      run-tests --app <app> --lightmode   # target engine
bench --site mariadb.localhost run-tests --app <app> --lightmode   # must stay green
```

**Run a single module** while iterating (fast inner loop), then re-run the full suite before
calling it done:
```bash
bench --site pg.localhost run-tests \
  --module <app>.<module>.doctype.<doctype>.test_<doctype> --lightmode
```

The discipline: **every fix is checked on BOTH engines.** PG proves the fix works; MariaDB
proves you didn't break the engine that already worked. A fix that only passes on PG is **not done**.

> **Worktree note:** if you run from a git worktree rather than the bench's own `apps/<app>`,
> the bench's Python won't find your checkout unless `PYTHONPATH` points at it — invoke via the
> bench's env (`cd sites && ../env/bin/python -m frappe.utils.bench_helper frappe run-tests ...`)
> or just work inside the bench's primary app dir.

---

## 2. The Postgres CI workflow shape — mirror MariaDB, but keep it NON-required

Add a Postgres workflow that is a near-copy of the MariaDB one, with three deliberate differences:

```yaml
name: Server (Postgres)          # distinct workflow name
on:
  pull_request:
    paths-ignore: [ '**.js', '**.md', '**.html', ... ]
    types: [opened, labelled, synchronize, reopened]   # 'labelled' included
jobs:
  test:
    if: ${{ contains(github.event.pull_request.labels.*.name, 'postgres') }}   # label-gated
    runs-on: ubuntu-latest
    name: Postgres Unit Tests     # DISTINCT job name — do NOT reuse the MariaDB job name
    services:
      postgres:
        image: postgres:13.3
        env: { POSTGRES_PASSWORD: <pw> }
        options: >-
          --health-cmd pg_isready --health-interval 10s
          --health-timeout 5s --health-retries 5
        ports: [ "5432:5432" ]
    steps:
      - uses: actions/checkout@v6
      # ...identical python/node/cache setup to the MariaDB workflow...
      - name: Install
        run: bash ${GITHUB_WORKSPACE}/.github/helper/install.sh
        env:
          DB: postgres          # the env difference that matters for install
          TYPE: server
      - name: Run Tests
        run: cd ~/frappe-bench/ && bench --site test_site run-parallel-tests --lightmode --app <app> ...
```

The three deliberate differences:

- **Distinct job/workflow `name`.** GitHub derives a *status-check context* from
  `<workflow name> / <job name (matrix)>`. A distinct name keeps the PG contexts *separate*
  from MariaDB's — that separation is what lets you make MariaDB required while Postgres stays
  advisory. **Do not name the PG job identically to the MariaDB job**, or you collide contexts
  (and effectively make PG required by accident).
- **Build against upstream `frappe develop`, not your local frappe.** `install.sh` clones
  frappe fresh from `https://github.com/<frappeuser>/frappe` at `${FRAPPE_BRANCH:-$GITHUB_BASE_REF}`
  — by default CI builds against the base branch (`develop`). Intentional — see §4.
- **Keep it OFF the required-checks list** until the suite is actually green (see §5). The job
  is gated `if: contains(labels, 'postgres')`, so it only runs when you opt a PR in with a
  `postgres` label.

Mirror, don't reinvent: copy the MariaDB workflow's caching, host setup, and Python/Node steps
verbatim so the two runs differ only in engine.

---

## 3. `install.sh` Postgres speed-up — durability off for a disposable DB

The CI database is thrown away after every run, so crash-safety is wasted I/O. The MariaDB
service already disables durability via container flags and re-asserts at runtime:
```yaml
# MariaDB service:
MARIADB_EXTRA_FLAGS: --innodb-flush-log-at-trx-commit=0 --sync-binlog=0 --innodb-doublewrite=0
```
```bash
# install.sh re-asserts at runtime:
SET GLOBAL innodb_flush_log_at_trx_commit=0; SET GLOBAL sync_binlog=0;
```

Give Postgres the equivalent — turn `synchronous_commit`, `fsync`, `full_page_writes` **off**
for the throwaway CI DB, right where `install.sh` creates the test database over `psql`:
```bash
# In install.sh, under the [ "$DB" == "postgres" ] branch, alongside CREATE DATABASE/CREATE USER:
echo "<pw>" | psql -h 127.0.0.1 -p 5432 -U postgres \
  -c "ALTER SYSTEM SET synchronous_commit = off;" \
  -c "ALTER SYSTEM SET fsync = off;" \
  -c "ALTER SYSTEM SET full_page_writes = off;" \
  -c "SELECT pg_reload_conf();"
```
`synchronous_commit = off` reloads live; `fsync`/`full_page_writes` off may need a server
restart to fully take effect — in the GitHub `services` container, the cleanest analogue to
`MARIADB_EXTRA_FLAGS` is to pass them as the container `command`/args so they're set at boot.
**Never do this on a real database** — `fsync` off means a crash can corrupt the cluster. It's
correct *only* because this DB is created and dropped within one CI run.

---

## 4. The core process rule (learned the hard way)

This is the rule the whole harness exists to enforce:

1. **Run the WHOLE suite on the target engine.** Source-only reasoning misses things — a query
   that parses fine can still return wrong rows on Postgres (case-sensitivity, GROUP BY
   strictness, empty-string-vs-NULL, integer-vs-boolean). Only executing the tests on a real PG
   instance surfaces those.
2. **Against `frappe develop`, NOT a local frappe carrying extra PG patches.** If you test
   against a frappe checkout with uncommitted compatibility shims, you're validating against a
   framework that doesn't exist for anyone else; your "passing" suite lies. `install.sh`
   deliberately fetches frappe fresh from upstream at the base branch. Reproduce that locally:
   point your test bench's frappe at clean `develop` (or `git stash` local frappe changes)
   before you trust a result.
3. **Run the full suite, not only per-module.** Per-module runs are the fast inner loop; they
   miss cross-module ordering effects and shared fixtures. The gate is the full
   `run-tests --app <app>`.
4. **SWEEP THE TEST FILES TOO.** The one thing source-only audits always miss: broken SQL hides
   in `test_*.py` helpers and `setUp`/fixture code, not in shipped controllers. A source-only
   grep over non-test code passes while the suite explodes on a Postgres-invalid query inside a
   test helper. Audit `test_*.py` with the same rigor as production code. (In the reference
   migration the final harness commit was literally
   `test(postgres): make test-helper SQL Postgres-valid across the suite`.)
5. **Verify-before-fix, prove-after-fix.** For each finding: (a) *reproduce* the failure on PG
   — run the module, see it red; (b) fix; (c) run on PG — green; (d) run on MariaDB — still
   green. A fix you never saw fail, or never confirmed on both engines, is unverified and
   doesn't ship.

> **War story.** A real migration ran a source-centric audit + ~60 PRs and looked done — until
> the first true full-suite run on `develop`-against-`frappe develop` exposed ~15 **test files**
> with their own raw SQL (`timestamp(posting_date, posting_time)`, `db_set("Status")`, inline
> `frappe.db.sql`) that the source audit never touched. The false-green came from (a) a local
> bench built against a frappe branch *with* PG patches, and (b) differential CI running off a
> staging branch that already carried the test fixes. Lesson: items 2–4 above are not optional.

---

## 5. GitHub gotchas that make a label-gated PG check dangerous if it's "required"

- **A fork PR cannot run a workflow version it modifies.** For `pull_request` events, GitHub
  runs the workflow definition from the **base** branch, not the PR's head. A PR that *adds or
  edits* `server-tests-postgres.yml` will not run the new version — it only takes effect once
  merged to base. (If you have admin on the repo, push the workflow as a same-repo branch rather
  than a fork PR so it can run.)
- **Triggers AND required-status contexts are read from the BASE branch.** Branch protection's
  required-checks list lives on the base branch; a context becomes blocking only after the
  workflow (with that exact context name) is present on base and added to protection there.
- **A skipped matrix job still reports a bare job-name context — and a "required" context that
  never arrives hangs the PR.** The PG workflow is gated `if: contains(labels, 'postgres')`. On
  a PR *without* the label the job is skipped. If you've marked the PG context required, GitHub
  waits forever for a status the skipped job may report as the bare job name — and on un-labelled
  PRs that's either pending-forever or collides with the MariaDB job of the same name. Either
  way: **non-labelled PRs hang.**

**Therefore:** keep the Postgres check **NON-required** until the suite is genuinely green on
`develop`. While migrating it's an advisory, opt-in (`postgres` label) signal — distinct
workflow name, off the required list. Only when PG reliably passes do you deliberately promote
it: add its context to the base branch's required-checks list as the *last* step. If you must
make it required earlier, add a **faux always-passing job** with the required context name so
skipped/un-labelled PRs still report green and don't hang. Give the PG job a *unique*
matrix/name so it never collides with MariaDB's context.

---

## Two CI gotchas worth pre-empting

- **Prerequisite apps must be in the PG site's `install_apps`, not just fetched.**
  `bench get-app <dep>` puts the app in the bench; the *site* installs only what its
  `site_config` `install_apps` lists. If the MariaDB site_config lists `["payments", "app"]`
  but the Postgres one lists only `["app"]`, the prerequisite's tables are absent on PG
  (`relation "tabPayment Gateway" does not exist`) and a swathe of tests error. Keep both
  engines' `install_apps` in sync.
- **The frappe version is part of your contract.** A frappe change can *withdraw* a safety
  net your app relied on (e.g. frappe#40075 removed the blanket per-insert savepoint, so
  catch-and-continue inserts now need explicit savepoints — see `06`). Build/test against the
  exact frappe your users run. A suite green against a patched local frappe lies.

For the rest of the "run-it-for-real" lessons — Redis-must-be-up, cascade-failure triage,
reading `run-parallel-tests` CI logs, and comprehensive staging↔develop reconciliation — see
**`references/06-transaction-and-runtime.md` §6**.

---

## Enforce the mechanical breaks with a pre-commit hook (the gate is label-gated)

Because the Postgres job is label-gated (it doesn't run on every PR), add an **always-on
pre-commit hook** as the first line of defence. A ready-made, app-agnostic, dependency-free
checker ships with this skill at **`tools/postgres_compat.py`** (with `tools/test_postgres_compat.py`).

It statically flags the *mechanical* breaks — MySQL-only functions (`timestamp(date,time)`,
`timediff`, `str_to_date`, `date_format`/`date_add`/`date_sub`, `group_concat`, SQL `IF()`),
`SHOW INDEX`/`TABLES`/`COLUMNS` and their result keys, single-quoted aliases, `UPDATE…JOIN`,
f-string/format SQL carrying those MySQL-isms, and `set_value`/`db_set(<Check>, bool)`. It does
**not** flag the framework auto-translations (see `01`) or the *semantic* divergences (loose
`GROUP BY`, case-sensitivity, NULL order, tiebreakers) — the gated **test suite stays the
backstop** for those. AST + SQL-structure-gated regex keep false positives near zero (docstrings
and prose are skipped); `# pg-ok` exempts an intentional MariaDB-only branch.

Wire it into `.pre-commit-config.yaml` (drop the helper into your repo, e.g. `.github/helper/`):

```yaml
  - repo: local
    hooks:
      - id: postgres-compat
        name: "PostgreSQL compatibility (static check)"
        entry: .github/helper/postgres_compat.py
        language: script
        files: ^<your_app>/.*\.py$
        exclude: ^<your_app>/patches/   # historical, version-gated migrations
```

Validate against your already-clean tree: `pre-commit run postgres-compat --all-files` should
report **Passed** (it found a real missed `set_value(..., True)` the first time it ran on the
reference app — a good sign it earns its keep).

### Know its boundaries (so you don't over-trust it)

It is a high-precision net for **textual MySQL-isms in SQL-shaped string literals + two AST
call patterns** — not a correctness proof. Be explicit with your team about the line:

**Covers**
- In string literals that have real SQL *structure* (`SELECT…FROM`, `UPDATE…SET`, `DELETE FROM`,
  `INSERT INTO`, `SHOW INDEX/TABLES/COLUMNS`, `FROM \`tab…\``) — incl. `.format()`/`%` templates
  and the static text of f-strings: `timestamp(date,time)`, `timediff`, `str_to_date`,
  `date_format`, `date_add/sub`, `group_concat`, `period_diff`, SQL `IF()`, `SHOW INDEX/TABLES/COLUMNS`,
  single-quoted aliases, `UPDATE … JOIN`.
- AST: `set_value`/`db_set(<Check>, True/False)` (literal bool); `SHOW INDEX` result keys
  (`row["Column_name"]` / `row.get("Column_name")`).
- Correctly *ignores* the framework auto-translations (`ifnull`, backtick, `locate`, `REGEXP`,
  `.like()→ILIKE`) and docstrings/prose.

**Does NOT cover (→ the gated test suite is the backstop)**
- The whole **semantic-divergence** class (ref `03`): loose `GROUP BY`, case-sensitive `==`/`IN`,
  name-lookup case, empty-string↔NULL, NULL ordering, `LIMIT 1` tiebreakers, `Max()` pick,
  integer division, `UnixTimestamp(date)` TZ skew.
- Hard breaks needing semantics: **capital-cased identifiers** (`get_value(dt, dn, "Status")`),
  `HAVING`-on-alias, `SELECT DISTINCT … ORDER BY`-not-in-select, `varchar | varchar` boolean strictness.
- **Transaction-abort / savepoint discipline** (ref `06` §1) — control-flow, not detectable here.
- **Query-builder-expressed** MySQL-isms (`CustomFunction("timestamp", …)`, pypika) — only SQL
  *strings* are scanned, not qb constructs.
- Blind spots within its own checks: MySQL-isms inside an **interpolated value** (not the static
  template), SQL assembled from **fragments/variables** where no single literal has SQL structure,
  `timestamp(fn(x), y)` (nested paren), bool passed as a **variable**.
- Scope: a **new patch** under `patches/` (excluded), non-`<app>` files, and anything carrying
  `# pg-ok`.

---

## Summary checklist

- [ ] Two local test sites: one `--db-type postgres`, one `--db-type mariadb`, both `allow_tests`.
- [ ] Full suite green on MariaDB *before* you start (baseline / no-regression contract).
- [ ] PG CI workflow added: mirrors MariaDB, `DB: postgres`, distinct name, label-gated, **non-required**.
- [ ] `install.sh` turns `synchronous_commit`/`fsync`/`full_page_writes` off for the disposable PG DB.
- [ ] **Prerequisite apps present in the PG site's `install_apps`** (in sync with MariaDB).
- [ ] CI (and local verification) build against clean upstream `frappe develop` (version is part of the contract).
- [ ] Every fix: reproduced-red on PG → fixed → green on PG → still-green on MariaDB.
- [ ] Test-helper SQL swept, not just production code.
- [ ] Catch-and-continue insert paths savepoint-guarded (see `06`).
- [ ] `tools/postgres_compat.py` wired into pre-commit (always-on guard for the mechanical breaks).
- [ ] PG context promoted to required only *after* the full suite is reliably green — last step.
