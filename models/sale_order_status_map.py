# -*- coding: utf-8 -*-
from odoo import models, fields

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    monta_status_normalized = fields.Selection([
        ('processing','Processing'),
        ('received','Received'),
        ('picked','Picked'),
        ('shipped','Shipped'),
        ('delivered','Delivered'),
        ('backorder','Backorder'),
        ('cancelled','Cancelled'),
        ('error','Error'),
        ('unknown','Unknown'),
    ], string="Monta Status (Normalized)", copy=False, index=True)