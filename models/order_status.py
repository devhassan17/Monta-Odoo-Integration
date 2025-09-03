# -*- coding: utf-8 -*-
from odoo import models, fields


class MontaOrderStatus(models.Model):
    _name = "monta.order.status"
    _description = "Monta Order Status snapshots"
    _order = "id desc"

    sale_order_id = fields.Many2one(
        "sale.order", string="Sale Order", required=True, index=True, ondelete="cascade"
    )
    source = fields.Char(string="Source", default="monta", readonly=True)
    fetched_at = fields.Datetime(string="Fetched At", default=lambda self: fields.Datetime.now(), index=True, required=True)

    status_raw = fields.Char(string="Status (raw)", index=True)
    status_normalized = fields.Char(string="Status (normalized)", index=True)
    delivered_at = fields.Datetime(string="Delivered At")
    notes = fields.Text(string="Notes")
