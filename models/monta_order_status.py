# -*- coding: utf-8 -*-
import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class MontaOrderStatus(models.Model):
    _name = "monta.order.status"
    _description = "Monta Order Status snapshot"
    _order = "last_sync desc, id desc"

    sale_order_id = fields.Many2one(
        "sale.order", string="Sales Order", index=True, ondelete="cascade", required=True
    )
    order_name = fields.Char(string="Order Name", index=True, required=True)

    monta_order_ref = fields.Char(string="Monta Order Id/Number", index=True)
    order_status = fields.Char(string="Order Status")
    delivery_message = fields.Char(string="Delivery Message")
    track_trace_url = fields.Char(string="Track & Trace URL")
    delivery_date = fields.Datetime(string="Delivery Date")
    last_sync = fields.Datetime(string="Last Sync Time (UTC)", default=fields.Datetime.now, index=True)

    _sql_constraints = [
        ("monta_order_name_unique", "unique(order_name)", "Monta order name must be unique."),
    ]

    def name_get(self):
        res = []
        for rec in self:
            title = rec.order_name or f"Status #{rec.id}"
            if rec.order_status:
                title = f"{title} — {rec.order_status}"
            res.append((rec.id, title))
        return res

    @api.model
    def upsert_for_order(self, so, **vals):
        if not so or not so.name:
            return False
        base_vals = {
            "sale_order_id": so.id,
            "order_name": so.name,
            "monta_order_ref": vals.get("monta_order_ref"),
            "order_status": vals.get("order_status"),
            "delivery_message": vals.get("delivery_message"),
            "track_trace_url": vals.get("track_trace_url"),
            "delivery_date": vals.get("delivery_date"),
            "last_sync": vals.get("last_sync") or fields.Datetime.now(),
        }
        rec = self.search([("order_name", "=", so.name)], limit=1)
        if rec:
            rec.sudo().write(base_vals)
            return rec
        return self.sudo().create(base_vals)
