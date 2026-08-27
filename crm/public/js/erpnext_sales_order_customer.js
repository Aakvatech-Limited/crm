frappe.ui.form.on('Sales Order', {
	refresh(frm) {
		// When a Sales Order is created from a CRM Deal quotation that has no
		// customer yet, resolve/create the customer using CRM Organization identity.
		if (frm.doc.docstatus !== 0 || frm.doc.customer || frm.__crm_customer_checked) return
		const item = (frm.doc.items || []).find((i) => i.prevdoc_docname)
		if (!item) return
		frm.__crm_customer_checked = true
		frappe.call({
			method: 'crm.integrations.erpnext.customer.check_customer_for_quotation',
			args: { quotation: item.prevdoc_docname },
			callback: (r) => {
				if (r.message) frm.set_value('customer', r.message)
			},
		})
	},
})
