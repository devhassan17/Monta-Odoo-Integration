import logging
import requests
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    monta_order_id = fields.Char(string="Monta Order ID", copy=False)
    monta_order_sent = fields.Boolean(string="Sent to Monta", default=False, copy=False)
    monta_shipment_ids = fields.One2many(
        'stock.picking',
        compute='_compute_monta_shipments',
        string="Monta Shipments"
    )

    def _compute_monta_shipments(self):
        for order in self:
            order.monta_shipment_ids = order.picking_ids.filtered(
                lambda p: p.carrier_tracking_ref
            )

    def action_confirm(self):
        res = super().action_confirm()
        for order in self:
            if not order.monta_order_sent:
                order._push_to_monta()
        return res

    def action_cancel(self):
        res = super().action_cancel()
        for order in self:
            if order.monta_order_id:
                order._cancel_monta_order()
        return res

    def action_push_to_monta(self):
        self.ensure_one()
        if self.monta_order_sent:
            raise UserError(_("This order has already been sent to Monta."))
        return self._push_to_monta()

    def _push_to_monta(self):
        """Push order data to Monta API"""
        self.ensure_one()
        
        config = self.env['res.config.settings'].sudo()
        try:
            order_data = self._prepare_monta_order_data()
            response = config._make_monta_request('POST', 'orders', order_data)
            
            if response.status_code in (200, 201):
                response_data = response.json()
                self.write({
                    'monta_order_id': response_data.get('id'),
                    'monta_order_sent': True
                })
                message = _("Order successfully sent to Monta with ID %s") % self.monta_order_id
                self.message_post(body=message)
                return {
                    'effect': {
                        'fadeout': 'slow',
                        'message': message,
                        'type': 'rainbow_man',
                    }
                }
            else:
                error_msg = _("Error pushing order to Monta: %s - %s") % (response.status_code, response.text)
                _logger.error(error_msg)
                raise UserError(error_msg)
        except requests.exceptions.RequestException as e:
            error_msg = _("Failed to connect to Monta: %s") % str(e)
            _logger.error(error_msg)
            raise UserError(error_msg) from e

    def _prepare_monta_order_data(self):
        """Prepare order data for Monta API"""
        self.ensure_one()
        shipping_partner = self.partner_shipping_id
        return {
            'externalOrderNo': self.name,
            'customer': {
                'name': shipping_partner.name,
                'email': shipping_partner.email or self.partner_id.email,
                'phone': shipping_partner.phone or self.partner_id.phone,
                'address': {
                    'street': shipping_partner.street or '',
                    'street2': shipping_partner.street2 or '',
                    'city': shipping_partner.city or '',
                    'state': shipping_partner.state_id.name or '',
                    'country': shipping_partner.country_id.code or '',
                    'zip': shipping_partner.zip or '',
                }
            },
            'lines': [{
                'sku': line.product_id.default_code or line.product_id.barcode or line.product_id.name,
                'quantity': line.product_uom_qty,
                'price': line.price_unit,
                'description': line.name,
            } for line in self.order_line],
            'notes': self.note or '',
        }

    def _cancel_monta_order(self):
        """Cancel order in Monta"""
        self.ensure_one()
        config = self.env['res.config.settings'].sudo()
        try:
            response = config._make_monta_request(
                'POST', 
                f'orders/{self.monta_order_id}/cancel'
            )
            
            if response.status_code in (200, 204):
                message = _("Order successfully canceled in Monta")
                self.message_post(body=message)
                return True
            else:
                error_msg = _("Error canceling order in Monta: %s - %s") % (response.status_code, response.text)
                _logger.error(error_msg)
                return False
        except requests.exceptions.RequestException as e:
            _logger.error(_("Failed to connect to Monta for cancellation: %s"), str(e))
            return False