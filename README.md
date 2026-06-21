# frappe-postgres-migration — install & use

A Claude Code **skill** that gives Claude the full playbook for migrating a Frappe app to run
identically on PostgreSQL and MariaDB. App-agnostic (HRMS, Healthcare, custom apps, ERPNext).
Distilled from a completed ERPNext→Postgres migration; every technical claim is cited to real
`frappe` source.

## What's in here

```
frappe-postgres-migration/
├── SKILL.md                          # entry point: the rule, the 5-phase process, decision rules
├── README.md                         # this file (install notes — not loaded by Claude)
├── references/                       # loaded on demand by Claude when relevant
│   ├── 01-false-positives.md         # framework-handled forms — don't "fix" these
│   ├── 02-hard-breaks.md             # queries that ERROR on Postgres + portable fixes
│   ├── 03-silent-divergences.md      # queries that return DIFFERENT results + parity fixes
│   ├── 04-portable-cookbook.md       # copy-pasteable qb/ORM recipes
│   ├── 05-ci-harness.md              # dual-engine sites, CI workflow, install.sh, branch protection, pre-commit gate
│   └── 06-transaction-and-runtime.md # txn-abort/savepoints, set_value(bool), name-case, TZ epochs, run-it-for-real lessons
└── tools/                            # ready-to-use, app-agnostic
    ├── postgres_compat.py            # pre-commit checker for the mechanical MySQL-only breaks
    └── test_postgres_compat.py       # its unit tests (run: python -m unittest, no frappe needed)
```

## Install (the person receiving this)

Drop the whole `frappe-postgres-migration/` folder into either:

- **User-level** (available in every project): `~/.claude/skills/frappe-postgres-migration/`
- **Project-level** (committed with the app, shared with the team):
  `<your-app-repo>/.claude/skills/frappe-postgres-migration/`

Then start (or restart) Claude Code in that directory. Claude auto-discovers the skill from
its `SKILL.md` frontmatter — no config needed.

## Use

Just ask in natural language, e.g.:

- "Migrate this app to run on Postgres as well as MariaDB."
- "Audit our queries for Postgres breakage and parity issues."
- "Set up dual-engine Postgres + MariaDB test CI for this app."

Claude will invoke the skill, read `SKILL.md`, and pull in the relevant `references/*.md` as it
works. The intended order is: stand up the harness first (`05`), inventory while discarding
false positives (`01`), then fix in a reproduce-red → fix → green-on-both loop (`02`/`03`/`04`),
sweeping test files too, and promote CI to required last.

## The one rule to remember

**MariaDB behaviour must NOT change. Postgres is bent to match MariaDB, never the reverse.**
Every fix ships with a test that passes on *both* engines.

## A note on line numbers

Citations like `frappe/database/query.py:698` are from one `frappe develop` checkout and
**drift between versions**. The skill tells Claude to grep for the named identifier
(`FUNCTION_MAPPING`, `modify_query`, `patch_like_operators`, …), not the line number. If your
installed frappe is very old, confirm the auto-translation handlers in `01` exist before
trusting them.
