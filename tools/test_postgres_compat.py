# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt
"""Unit tests for the .github/helper/postgres_compat.py pre-commit checker.

This file is excluded from the postgres-compat hook itself (see .pre-commit-config.yaml)
because the fixtures below intentionally contain MySQL-only SQL.
"""

import importlib.util
import os
import tempfile
import unittest

_HELPER = os.path.join(os.path.dirname(__file__), "postgres_compat.py")
_spec = importlib.util.spec_from_file_location("postgres_compat", _HELPER)
pgc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pgc)


def violations(code: str) -> list[str]:
	with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
		f.write(code)
		path = f.name
	try:
		return pgc.check_file(path)
	finally:
		os.unlink(path)


class TestPostgresCompat(unittest.TestCase):
	def _assert_flag(self, code: str, needle: str):
		out = violations(code)
		self.assertTrue(
			any(needle in v for v in out), f"expected '{needle}' to be flagged in:\n{code}\ngot: {out}"
		)

	def _assert_clean(self, code: str):
		self.assertEqual(violations(code), [], f"expected no flags in:\n{code}")

	# --- catches the mechanical breaks ---
	def test_timestamp_two_arg(self):
		self._assert_flag(
			'frappe.db.sql("select timestamp(posting_date, posting_time) from `tabSLE`")',
			"timestamp(date, time)",
		)

	def test_show_index(self):
		self._assert_flag('frappe.db.sql("show index from `tabItem`")', "SHOW INDEX")

	def test_update_join(self):
		self._assert_flag(
			'frappe.db.sql("update `tabA` a join `tabB` b on a.x=b.x set a.y=b.y")', "UPDATE ... JOIN"
		)

	def test_single_quoted_alias(self):
		self._assert_flag(
			"frappe.db.sql(\"select substr(x,1,3) as 'foo' from `tabA`\")", "single-quoted column alias"
		)

	def test_group_concat(self):
		self._assert_flag('frappe.db.sql("select group_concat(name) from `tabA`")', "group_concat()")

	def test_set_value_bool(self):
		self._assert_flag('frappe.db.set_value("Company", c, "some_check", True)', "bool")

	def test_db_set_bool(self):
		self._assert_flag('doc.db_set("is_default", False)', "bool")

	def test_mysql_result_key(self):
		self._assert_flag('row.get("Column_name")', "Column_name")

	def test_fstring_sql(self):
		self._assert_flag("frappe.db.sql(f\"select date_format(d, '%Y') from `tab{dt}`\")", "date_format()")

	# --- does not false-positive on safe shapes ---
	def test_ifnull_is_auto_translated(self):
		self._assert_clean('frappe.db.sql("select ifnull(qty, 0) from `tabBin`")')

	def test_like_is_ilike(self):
		self._assert_clean('frappe.db.get_all("Item", filters={"item_name": ["like", "%x%"]})')

	def test_prose_with_sql_words(self):
		# a translatable message that merely contains "select" and "as '...'"
		self._assert_clean(
			"frappe.throw(_(\"Cannot select charge type as 'On Previous Row' for first row\"))"
		)

	def test_docstring_describing_rule(self):
		self._assert_clean(
			'def f():\n\t"""Avoid MariaDB-only DATE_FORMAT(); read from the pg_index catalog instead."""\n\treturn 1\n'
		)

	def test_qb_is_clean(self):
		self._assert_clean("frappe.qb.from_(sle).select(CombineDatetime(sle.posting_date, sle.posting_time))")

	def test_pg_ok_suppresses(self):
		self._assert_clean('frappe.db.sql(  # pg-ok\n\t"show index from `tabItem`"\n)')


if __name__ == "__main__":
	unittest.main()
