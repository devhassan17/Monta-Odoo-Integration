# -*- coding: utf-8 -*-
from odoo import fields, models


class MontaSaleLog(models.Model):
    _name = "monta.sale.log"
    _description = "Monta API logs"

    name = fields.Char(string="Log Name")
    sale_order_id = fields.Many2one(
        "sale.order", string="Sale Order", ondelete="cascade"
    )
    log_data = fields.Text(string="Log JSON")
    level = fields.Selection(
        [("info", "Info"), ("error", "Error")],
        default="info",
        index=True,
    )
