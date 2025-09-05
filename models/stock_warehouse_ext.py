# -*- coding: utf-8 -*-
from odoo import models, fields

class StockWarehouseMonta(models.Model):
    _inherit = 'stock.warehouse'

    x_monta_inbound_warehouse_name = fields.Char(
        string="Monta Warehouse Display Name",
        help="Exact display name of the warehouse in Monta (as shown in Monta UI). "
             "If empty, service will use global ICP key 'monta.inbound_warehouse_display_name'."
    )
