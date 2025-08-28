# -*- coding: utf-8 -*-
from odoo import models, fields

class MontaOrderBatchTrace(models.Model):
    _name = "monta.order.batch.trace"
    _description = "Monta Batch / Expiry per Order Line"

    sale_order_id = fields.Many2one('sale.order', ondelete='cascade', index=True, required=True)
    sale_order_line_id = fields.Many2one('sale.order.line', ondelete='cascade', index=True)
    product_id = fields.Many2one('product.product', index=True)
    sku = fields.Char()
    batch_number = fields.Char()
    expiry_date = fields.Date()
    qty = fields.Float()
    carrier = fields.Char()
    tracking_number = fields.Char()
    raw_payload = fields.Text(help="Optional raw JSON excerpt for troubleshooting")