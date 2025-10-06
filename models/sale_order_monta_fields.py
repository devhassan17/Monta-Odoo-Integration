# -*- coding: utf-8 -*-
from odoo import fields, models

class SaleOrder(models.Model):
    _inherit = "sale.order"

    # Already existed: monta_status, monta_status_code, monta_status_source, monta_track_trace, monta_last_sync
    # New mirror fields so you can show them anywhere:
    monta_order_ref = fields.Char(string="Monta Order Ref", copy=False, index=True)
    monta_delivery_message = fields.Char(string="Monta Delivery Message", copy=False)
    monta_delivery_date = fields.Date(string="Monta Delivery Date", copy=False)
    monta_status_raw = fields.Text(string="Monta Status Raw (JSON)", copy=False)
