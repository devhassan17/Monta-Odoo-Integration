from odoo import models, api
import logging

_logger = logging.getLogger(__name__)

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    def action_confirm(self):
        res = super(SaleOrder, self).action_confirm()
        for order in self:
            partner = order.partner_id

            _logger.info("✅ Order Confirmed:")
            _logger.info(f"📄 Order: {order.name}")
            _logger.info(f"👤 Customer: {partner.name}")
            _logger.info(f"✉️ Email: {partner.email}")
            _logger.info(f"💰 Total: {order.amount_total}")
            _logger.info(f"🛍️ Order Lines: {[(l.product_id.name, l.product_uom_qty) for l in order.order_line]}")

            # Prepare full Monta payload
            payload = order._prepare_monta_order_payload()

            _logger.info("📦 Monta Payload Details:")
            _logger.info(f"🔹 WebshopOrderId: {payload.get('WebshopOrderId')}")
            _logger.info(f"🔹 Reference: {payload.get('Reference')}")
            _logger.info(f"🔹 Origin: {payload.get('Origin')}")
            _logger.info(f"📬 Delivery Address: {payload['ConsumerDetails']['DeliveryAddress']}")
            _logger.info(f"📮 Invoice Address: {payload['ConsumerDetails']['InvoiceAddress']}")
            _logger.info(f"📦 Order Lines (Lines): {payload.get('Lines')}")
            _logger.info(f"🧾 Invoice: {payload.get('Invoice')}")

            # Save in monta.sale.log model
            order._create_monta_log(payload, level='info')

        return res
