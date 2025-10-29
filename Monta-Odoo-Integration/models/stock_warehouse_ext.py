# -*- coding: utf-8 -*-
from odoo import models, fields

class StockWarehouseMonta(models.Model):
    _inherit = "stock.warehouse"

    x_monta_inbound_warehouse_name = fields.Char(
        string="Monta Warehouse Display Name",
        help="Exact display name of the target warehouse in Monta UI. "
             "If empty, service will use ICP 'monta.inbound_warehouse_display_name'."
    )
