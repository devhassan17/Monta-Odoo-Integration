# -*- coding: utf-8 -*-
from odoo import fields, models


class AccountMove(models.Model):
    """
    Deprecated fields kept so database views don't break.
    The Monta integration works exclusively through stock.picking (deliveries).
    """
    _inherit = "account.move"

    monta_renewal_pushed = fields.Boolean(
        string="Pushed to Monta (Deprecated)",
        copy=False,
    )
    monta_renewal_webshop_order_id = fields.Char(
        string="Monta Webshop Order ID (Deprecated)",
        copy=False,
    )
    monta_renewal_last_push = fields.Datetime(
        string="Monta Last Push (Deprecated)",
        copy=False,
    )
