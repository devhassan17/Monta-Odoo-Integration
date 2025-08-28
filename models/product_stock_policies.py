# -*- coding: utf-8 -*-
from odoo import models, fields, api
import logging
_logger = logging.getLogger(__name__)

class ProductTemplate(models.Model):
    _inherit = 'product.template'

    x_monta_min_stock = fields.Float(string="Monta Min Stock", default=0.0)
    x_monta_last_stock = fields.Float(string="Monta Last Stock", readonly=True)
    x_monta_autosoldout = fields.Boolean(string="Auto Sold Out at 0", default=True,
        help="When synced stock is <= 0, set product sale_ok=False automatically.")

    def _apply_soldout_policy(self):
        for tmpl in self:
            if tmpl.x_monta_autosoldout:
                if tmpl.x_monta_last_stock <= 0 and tmpl.sale_ok:
                    tmpl.write({'sale_ok': False})
                elif tmpl.x_monta_last_stock > 0 and not tmpl.sale_ok:
                    tmpl.write({'sale_ok': True})