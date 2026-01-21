# -*- coding: utf-8 -*-
from odoo import fields, models


class SaleOrder(models.Model):
    _inherit = "sale.order"

    # Existing Monta mirrors
    monta_order_ref = fields.Char(
        string="Monta Order Ref",
        copy=False,
        index=True,
        help="Reference of the order in Monta.",
    )
    monta_delivery_message = fields.Char(
        string="Monta Delivery Message",
        copy=False,
    )
    monta_delivery_date = fields.Date(
        string="Monta Delivery Date",
        copy=False,
    )
    monta_status_raw = fields.Text(
        string="Monta Status Raw (JSON)",
        copy=False,
    )

    # Mirror of 'Available on Monta'
    monta_on_monta = fields.Boolean(
        string="Available on Monta",
        copy=False,
        index=True,
        help="Checked if this order is known in Monta (monta_order_ref exists).",
    )
