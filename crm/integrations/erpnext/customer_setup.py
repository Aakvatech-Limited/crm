# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe

from crm.integrations.erpnext.customer import ensure_local_customer_organization_field


def after_migrate():
	"""Provision the reciprocal Customer link for same-site ERPNext integrations."""
	if not frappe.db.exists("DocType", "ERPNext CRM Settings"):
		return

	settings = frappe.get_single("ERPNext CRM Settings")
	if not settings.enabled or settings.is_erpnext_in_different_site:
		return
	if "erpnext" not in frappe.get_installed_apps():
		return

	ensure_local_customer_organization_field()
