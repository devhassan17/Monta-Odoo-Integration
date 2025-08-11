import logging
import json
from odoo import models
from odoo.addons.queue_job.decorators import job

_logger = logging.getLogger(__name__)

class SaleOrder(models.Model):
    _inherit = 'sale.order'
    _inherit = ['sale.order', 'monta.api.mixin']  # Include Monta API methods

    @job
    def job_send_to_monta(self, payload):
        """Background job to send order to Monta API."""
        self.ensure_one()
        monta_response = self._send_to_monta(payload)
        self._create_monta_log(monta_response, level='info' if 'error' not in monta_response else 'error')
        return monta_response

    def _create_monta_log(self, payload, level='info'):
        vals = {
            'sale_order_id': self.id,
            'log_data': json.dumps(payload, indent=2, default=str),
            'level': level,
            'name': f'Monta {self.name} - {level}',
        }
        self.env['monta.sale.log'].sudo().create(vals)

    def action_confirm(self):
        res = super(SaleOrder, self).action_confirm()
        for order in self:
            partner = order.partner_id

            # Log basic order info
            _logger.info(f"✅ Order Confirmed: {order.name} for {partner.name}")
            _logger.info(f"Total: {order.amount_total}, Email: {partner.email}")

            # Prepare Monta payload
            payload = order._prepare_monta_order_payload()
            order._create_monta_log(payload, level='info')

            # Schedule background job
            order.with_delay(priority=10, max_retries=5, retry_pattern=[10, 60, 300]).job_send_to_monta(payload)

        return res
