# -*- coding: utf-8 -*-
import logging
from odoo import fields, models

_logger = logging.getLogger(__name__)

class SkuTestLog(models.Model):
    _name = "sku_test.log"
    _description = "SKU Test – Component SKU Log"

    date = fields.Datetime(default=lambda self: fields.Datetime.now(), required=True)
    order_id = fields.Many2one("sale.order", string="Sale Order", index=True, ondelete="cascade")
    order_line_id = fields.Many2one("sale.order.line", string="Order Line", index=True, ondelete="cascade")
    pack_product_id = fields.Many2one("product.product", string="Pack/Kit Product", index=True)
    component_product_id = fields.Many2one("product.product", string="Component Product", index=True)
    sku = fields.Char(string="Component SKU", help="Real SKU captured at confirmation/update time")
