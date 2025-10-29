# -*- coding: utf-8 -*-
from odoo import fields, models

class SaleOrder(models.Model):
    _inherit = "sale.order"

    # Existing Monta mirrors (if you already added them, keep them)
    monta_order_ref = fields.Char(string="Monta Order Ref", copy=False, index=True)
    monta_delivery_message = fields.Char(string="Monta Delivery Message", copy=False)
    monta_delivery_date = fields.Date(string="Monta Delivery Date", copy=False)
    monta_status_raw = fields.Text(string="Monta Status Raw (JSON)", copy=False)

    # NEW: mirror of "Available on Monta" (boolean)
    # This corresponds to monta.order.status.on_monta (true when monta_order_ref is set)
    monta_on_monta = fields.Boolean(string="Available on Monta", copy=False, index=True)
