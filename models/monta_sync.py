import requests
from odoo import models, fields

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    monta_order_id = fields.Char('Monta Order ID')
    delivery_status = fields.Char('Delivery Status')
    tracking_ref = fields.Char('Tracking Reference')

    def send_order_to_monta(self):
        config = self.env['monta.config'].search([('active', '=', True)], limit=1)
        if not config:
            return

        headers = {
            'Authorization': f'Bearer {config.api_key}',
            'Content-Type': 'application/json'
        }

        order_payload = {
            "order_reference": self.name,
            "customer": {
                "name": self.partner_id.name,
                "address": self.partner_id.street,
                "city": self.partner_id.city,
                "zipcode": self.partner_id.zip,
                "country": self.partner_id.country_id.code
            },
            "items": [{
                "sku": line.product_id.default_code,
                "quantity": line.product_uom_qty
            } for line in self.order_line]
        }

        response = requests.post(f"{config.endpoint}/orders", json=order_payload, headers=headers)
        if response.status_code == 200:
            self.monta_order_id = response.json().get('order_id')

    def fetch_shipment_status(self):
        config = self.env['monta.config'].search([('active', '=', True)], limit=1)
        if not self.monta_order_id or not config:
            return

        headers = {'Authorization': f'Bearer {config.api_key}'}
        url = f"{config.endpoint}/orders/{self.monta_order_id}/status"
        response = requests.get(url, headers=headers)

        if response.status_code == 200:
            data = response.json()
            self.delivery_status = data.get('status')
            self.tracking_ref = data.get('tracking_number')
