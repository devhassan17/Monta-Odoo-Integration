from odoo import models, api
import logging

_logger = logging.getLogger(__name__)

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    @api.model
    def create(self, vals):
        order = super().create(vals)

        partner = order.partner_id
        _logger.info("🛒 New Sales Order Created:")
        _logger.info(f"📄 Order: {order.name}")
        _logger.info(f"👤 Customer: {partner.name}")
        _logger.info(f"✉️ Email: {partner.email}")
        _logger.info(f"💰 Total: {order.amount_total}")
        _logger.info(f"🛍️ Order Lines: {[(l.product_id.name, l.product_uom_qty) for l in order.order_line]}")

        return order
