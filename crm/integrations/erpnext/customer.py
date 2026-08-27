# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields as _create_custom_fields

from crm.fcrm.doctype.erpnext_crm_settings.erpnext_crm_settings import (
	get_contacts,
	get_erpnext_site_client,
	get_organization_address,
)


CUSTOMER_ORGANIZATION_FIELD = "crm_organization"
ORGANIZATION_CUSTOMER_FIELD = "erpnext_customer"


def create_customer_in_erpnext(doc, method=None):
	"""Create/link the ERPNext Customer for a CRM Deal using Organization as identity.

	Every Deal update first inherits the Organization's Customer link. Customer creation
	still happens only when the configured status is reached.
	"""
	erpnext_crm_settings = frappe.get_single("ERPNext CRM Settings")
	if not erpnext_crm_settings.enabled:
		return

	customer = get_customer_for_deal(doc, erpnext_crm_settings)
	if customer:
		set_deal_customer(doc.name, customer)
		return customer

	if (
		not erpnext_crm_settings.create_customer_on_status_change
		or doc.status != erpnext_crm_settings.deal_status
	):
		return

	return create_customer_from_deal(doc, erpnext_crm_settings)


def create_customer_on_sales_order(doc, method=None):
	if doc.customer:
		return

	crm_deal = get_deal_from_sales_order(doc)
	customer = check_customer_for_deal(crm_deal) if crm_deal else None
	if customer:
		doc.customer = customer


def check_customer_for_deal(crm_deal: str):
	"""Return/create the Customer for a Deal, preferring Organization identity."""
	erpnext_crm_settings = frappe.get_single("ERPNext CRM Settings")
	if not erpnext_crm_settings.enabled or erpnext_crm_settings.is_erpnext_in_different_site:
		return None
	if not crm_deal or not frappe.db.exists("CRM Deal", crm_deal):
		return None

	deal = frappe.get_cached_doc("CRM Deal", crm_deal)
	customer = get_customer_for_deal(deal, erpnext_crm_settings)
	if not customer:
		customer = create_customer_from_deal(deal, erpnext_crm_settings)
	return customer


@frappe.whitelist()
def check_customer_for_quotation(quotation: str):
	"""Resolve the Customer for the CRM Deal behind an ERPNext Quotation."""
	crm_deal = frappe.db.get_value("Quotation", quotation, "crm_deal")
	if not crm_deal:
		return None
	return check_customer_for_deal(crm_deal)


def get_deal_from_sales_order(doc):
	for item in doc.items:
		quotation = item.get("prevdoc_docname")
		if quotation:
			crm_deal = frappe.db.get_value("Quotation", quotation, "crm_deal")
			if crm_deal:
				return crm_deal
	return None


def get_customer_for_deal(doc, erpnext_crm_settings):
	"""Resolve Customer in Organization-first order with legacy Deal fallback."""
	if doc.organization:
		customer = frappe.db.get_value(
			"CRM Organization", doc.organization, ORGANIZATION_CUSTOMER_FIELD
		)
		if customer:
			return customer

	# Backward-compatibility path for pre-cutover Deal records. When an Organization
	# exists, opportunistically establish the new authoritative mapping.
	customer = frappe.db.get_value("CRM Deal", doc.name, "erpnext_customer")
	if not customer:
		customer = get_legacy_customer_for_deal(doc.name, erpnext_crm_settings)

	if customer and doc.organization:
		link_customer_to_organization(customer, doc.organization, erpnext_crm_settings)

	return customer


def get_legacy_customer_for_deal(crm_deal, erpnext_crm_settings):
	if not erpnext_crm_settings.is_erpnext_in_different_site:
		if frappe.db.has_column("Customer", "crm_deal"):
			return frappe.db.get_value("Customer", {"crm_deal": crm_deal}, "name")
		return None

	client = get_erpnext_site_client(erpnext_crm_settings)
	try:
		customers = client.get_list("Customer", filters={"crm_deal": crm_deal}, fields=["name"])
		return customers[0].get("name") if customers else None
	except Exception:
		# Legacy lookup must never prevent the Organization-centric flow.
		return None


def create_customer_from_deal(doc, erpnext_crm_settings):
	"""Create/reuse a Customer without writing CRM Deal identity onto new Customers."""
	customer = get_customer_for_deal(doc, erpnext_crm_settings)
	if customer:
		set_deal_customer(doc.name, customer)
		return customer

	contacts = get_contacts(doc)
	address = get_organization_address(doc.organization)

	if doc.organization:
		customer_title = doc.organization
		customer_type = "Company"
	else:
		primary_contact = next((c for c in contacts if c.get("is_primary")), None)
		customer_title = (primary_contact or {}).get("full_name") or doc.lead_name
		if not customer_title:
			frappe.throw(_("Organization or a primary Contact is required to create a customer"))
		customer_type = "Individual"

	customer_data = {
		"customer_name": customer_title,
		"customer_type": customer_type,
		"territory": doc.territory,
		"default_currency": doc.currency,
		"industry": doc.industry,
		"website": doc.website,
		"contacts": json.dumps(contacts),
		"address": json.dumps(address) if address else None,
	}

	try:
		if not erpnext_crm_settings.is_erpnext_in_different_site:
			try:
				from erpnext.crm.frappe_crm_api import create_customer
			except ImportError:
				frappe.throw(_("ERPNext is not installed in the current site"))

			if doc.territory and not frappe.db.exists("Territory", doc.territory):
				customer_data["territory"] = ""
			if doc.industry and not frappe.db.exists("Industry Type", doc.industry):
				customer_data["industry"] = ""

			customer_name = create_customer(customer_data)
		else:
			client = get_erpnext_site_client(erpnext_crm_settings)
			if doc.territory and not client.get_list("Territory", filters={"name": doc.territory}):
				customer_data["territory"] = ""
			if doc.industry and not client.get_list("Industry Type", filters={"name": doc.industry}):
				customer_data["industry"] = ""

			customer_name = client.post_api("erpnext.crm.frappe_crm_api.create_customer", customer_data)

		if not customer_name:
			_log_and_throw(
				"Error while creating customer in ERPNext, check error log for more details",
				f"Error while creating customer in ERPNext for CRM Deal: {doc.name}",
			)
	except (frappe.ValidationError, frappe.PermissionError):
		raise
	except Exception:
		_log_and_throw("Error while creating customer in ERPNext, check error log for more details")

	if customer_name:
		if doc.organization:
			link_customer_to_organization(customer_name, doc.organization, erpnext_crm_settings)
		set_deal_customer(doc.name, customer_name)
		frappe.publish_realtime("crm_customer_created")

	return customer_name


def link_customer_to_organization(customer_name, organization, erpnext_crm_settings):
	"""Persist CRM Organization ↔ ERPNext Customer without overwriting conflicts."""
	if not organization or not customer_name:
		return

	existing_customer = frappe.db.get_value(
		"CRM Organization", organization, ORGANIZATION_CUSTOMER_FIELD
	)
	if existing_customer and existing_customer != customer_name:
		frappe.throw(
			_("CRM Organization {0} is already linked to ERPNext Customer {1}").format(
				organization, existing_customer
			)
		)

	if not existing_customer:
		frappe.db.set_value(
			"CRM Organization", organization, ORGANIZATION_CUSTOMER_FIELD, customer_name
		)

	if erpnext_crm_settings.is_erpnext_in_different_site:
		_set_remote_customer_organization(customer_name, organization, erpnext_crm_settings)
	else:
		_set_local_customer_organization(customer_name, organization)


def _set_local_customer_organization(customer_name, organization):
	ensure_local_customer_organization_field()
	existing_organization = frappe.db.get_value(
		"Customer", customer_name, CUSTOMER_ORGANIZATION_FIELD
	)
	if existing_organization and existing_organization != organization:
		frappe.throw(
			_("ERPNext Customer {0} is already linked to CRM Organization {1}").format(
				customer_name, existing_organization
			)
		)
	if not existing_organization:
		frappe.db.set_value("Customer", customer_name, CUSTOMER_ORGANIZATION_FIELD, organization)


def ensure_local_customer_organization_field():
	if frappe.db.has_column("Customer", CUSTOMER_ORGANIZATION_FIELD):
		return
	_create_custom_fields(
		{
			"Customer": [
				{
					"fieldname": CUSTOMER_ORGANIZATION_FIELD,
					"fieldtype": "Data",
					"label": "Frappe CRM Organization",
					"read_only": 1,
					"no_copy": 1,
					"insert_after": "crm_deal",
				}
			]
		},
		ignore_validate=True,
	)


def _set_remote_customer_organization(customer_name, organization, erpnext_crm_settings):
	client = get_erpnext_site_client(erpnext_crm_settings)
	ensure_remote_customer_organization_field(client, erpnext_crm_settings)
	try:
		rows = client.get_list(
			"Customer",
			filters={"name": customer_name},
			fields=["name", CUSTOMER_ORGANIZATION_FIELD],
		)
		except Exception:
		_log_and_throw(
			"Could not read CRM Organization link from ERPNext Customer",
			f"Could not read Customer {customer_name} on {erpnext_crm_settings.erpnext_site_url}",
		)

	existing_organization = rows[0].get(CUSTOMER_ORGANIZATION_FIELD) if rows else None
	if existing_organization and existing_organization != organization:
		frappe.throw(
			_("ERPNext Customer {0} is already linked to CRM Organization {1}").format(
				customer_name, existing_organization
			)
		)
	if existing_organization:
		return

	try:
		client.post_api(
			"frappe.client.set_value",
			{
				"doctype": "Customer",
				"name": customer_name,
				"fieldname": CUSTOMER_ORGANIZATION_FIELD,
				"value": organization,
			},
		)
	except Exception:
		_log_and_throw(
			"Could not link ERPNext Customer to CRM Organization",
			f"Could not update Customer {customer_name} on {erpnext_crm_settings.erpnext_site_url}",
		)


def ensure_remote_customer_organization_field(client, erpnext_crm_settings):
	"""Create the reciprocal custom field on a remote ERPNext site if missing."""
	try:
		fields = client.get_list(
			"Custom Field",
			filters={"dt": "Customer", "fieldname": CUSTOMER_ORGANIZATION_FIELD},
			fields=["name"],
		)
		if fields:
			return
		client.insert(
			{
				"doctype": "Custom Field",
				"dt": "Customer",
				"fieldname": CUSTOMER_ORGANIZATION_FIELD,
				"label": "Frappe CRM Organization",
				"fieldtype": "Data",
				"read_only": 1,
				"no_copy": 1,
				"insert_after": "crm_deal",
			}
		)
	except Exception:
		_log_and_throw(
			"Could not create CRM Organization link field on ERPNext Customer",
			f"Could not create Customer.{CUSTOMER_ORGANIZATION_FIELD} on {erpnext_crm_settings.erpnext_site_url}",
		)


def set_deal_customer(crm_deal, customer_name):
	"""Compatibility/navigation field only; Organization remains authoritative."""
	if frappe.db.get_value("CRM Deal", crm_deal, "erpnext_customer") != customer_name:
		frappe.db.set_value("CRM Deal", crm_deal, "erpnext_customer", customer_name)


def _log_and_throw(message: str, title: str | None = None):
	frappe.log_error(frappe.get_traceback(), title or message)
	frappe.throw(_(message))
