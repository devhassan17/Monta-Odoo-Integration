# -*- coding: utf-8 -*-
from odoo import models, fields, api, _

class MontaOrderStatus(models.Model):
    _name = "monta.order.status"
    _description = "Monta Order Status (external, read-only for users)"
    _order = "last_sync desc, id desc"

    sale_order_id = fields.Many2one(
        "sale.order", string="Sales Order", index=True, ondelete="cascade", required=True
    )
    order_name = fields.Char(string="Order Name", index=True, required=True)
    source = fields.Char(string="API Source")                     # e.g., shipments?/orders?/events?
    status = fields.Char(string="Status")                         # e.g., Delivered / Blocked / Shipped / etc.
    status_code = fields.Char(string="Status Code")               # numeric/text from Monta
    track_trace = fields.Char(string="Track & Trace URL")         # URL (if any)
    delivery_date = fields.Datetime(string="Delivery Date")       # NEW: from Monta
    last_sync = fields.Datetime(string="Last Sync (UTC)", index=True)

    _sql_constraints = [
        ("monta_order_unique", "unique(order_name)", "Monta order reference must be unique."),
    ]

    def name_get(self):
        out = []
        for rec in self:
            label = f"{rec.order_name} — {rec.status or '-'}"
            out.append((rec.id, label))
        return out
