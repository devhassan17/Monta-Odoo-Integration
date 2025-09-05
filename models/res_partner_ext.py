# -*- coding: utf-8 -*-
from odoo import models, fields

class ResPartnerMonta(models.Model):
    _inherit = 'res.partner'

    x_monta_supplier_code = fields.Char(
        string="Monta Supplier Code",
        help="Exact supplier code as known by Monta (e.g. FAIR-CH). "
             "If empty, service will try ICP maps or vendor Ref/VAT."
    )
