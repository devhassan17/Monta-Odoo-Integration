import requests
from odoo import models, fields

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    monta_order_id = fields.Char("Monta Order ID")
    tracking_ref = fields.Char("Tracking Number")
    delivery_status = fields.Char("Delivery Status")

    def action_send_to_monta(self):
        config = self.env['monta.config'].search([('active', '=', True)], limit=1)
        if not config:
            return

        auth = (config.username, config.password)
        for order in self:
            payload = {
                "order_id": order.name,
                "recipient": {
                    "Company": order.partner_id.name,
                    "Street": order.partner_id.street,
                    "PostalCode": order.partner_id.zip,
                    "City": order.partner_id.city,
                    "CountryCode": order.partner_id.country_id.code,
                    "EmailAddress": order.partner_id.email,
                    "PhoneNumber": order.partner_id.phone
                },
                "lines": [
                    {
                        "product_id": line.product_id.default_code,
                        "quantity": line.product_uom_qty
                    } for line in order.order_line
                ]
            }
            response = requests.post(f"{config.endpoint}/orders", json=payload, auth=auth)
            if response.status_code == 200:
                data = response.json()
                order.monta_order_id = data.get("order_id")

    def fetch_shipment_status(self):
        config = self.env['monta.config'].search([('active', '=', True)], limit=1)
        if not self.monta_order_id or not config:
            return

        auth = (config.username, config.password)
        url = f"{config.endpoint}/orders/{self.monta_order_id}/status"
        response = requests.get(url, auth=auth)

        if response.status_code == 200:
            data = response.json()
            self.delivery_status = data.get('status')
            self.tracking_ref = data.get('tracking_number')
