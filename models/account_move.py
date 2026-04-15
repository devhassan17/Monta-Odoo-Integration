# -*- coding: utf-8 -*-
from odoo import fields, models

class AccountMove(models.Model):
    """
    Deprecated fields for Monta Subscription Integration.
    These are kept as placeholders to prevent Odoo Studio or custom database views 
    from breaking after the migration to Delivery-based tracking.
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
