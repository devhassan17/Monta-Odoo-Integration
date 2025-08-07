from odoo import models, fields, api, _
from odoo.exceptions import UserError
import requests
import logging

_logger = logging.getLogger(__name__)

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    monta_order_id = fields.Char(string='Monta Order ID', copy=False)
    monta_sync_date = fields.Datetime(string='Last Sync Date', copy=False)

    def action_confirm(self):
        """Override confirm to push to Monta"""
        res = super().action_confirm()
        for order in self:
            order._push_to_monta()
        return res

    def _push_to_monta(self):
        """Push order data to Monta API"""
        config = self.env['monta.config'].get_config()
        if not config:
            raise UserError(_('Monta configuration not found'))

        for order in self:
            try:
                order_data = {
                    'externalOrderNo': order.name,
                    'customer': {
                        'name': order.partner_id.name,
                        'email': order.partner_id.email,
                        'address': {
                            'street': order.partner_shipping_id.street or '',
                            'city': order.partner_shipping_id.city or '',
                            'zip': order.partner_shipping_id.zip or '',
                            'country': order.partner_shipping_id.country_id.code or '',
                        }
                    },
                    'lines': [{
                        'sku': line.product_id.default_code or line.product_id.barcode or str(line.product_id.id),
                        'quantity': line.product_uom_qty,
                        'price': line.price_unit,
                    } for line in order.order_line]
                }

                response = requests.post(
                    f"{config.endpoint.rstrip('/')}/orders",
                    json=order_data,
                    auth=(config.username, config.password),
                    timeout=10
                )

                if response.status_code in (200, 201):
                    order.write({
                        'monta_order_id': response.json().get('id'),
                        'monta_sync_date': fields.Datetime.now()
                    })
                else:
                    _logger.error("Monta API Error: %s - %s", response.status_code, response.text)
                    raise UserError(_('Error pushing order to Monta: %s') % response.text)

            except Exception as e:
                _logger.exception("Failed to push order to Monta")
                raise UserError(_('Failed to connect to Monta: %s') % str(e))