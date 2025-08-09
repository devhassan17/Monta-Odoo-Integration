from odoo import models, fields
import logging
import json
import re

_logger = logging.getLogger(__name__)

class MontaSaleLog(models.Model):
    _name = 'monta.sale.log'
    _description = 'Monta API logs'

    name = fields.Char('Log Name')
    sale_order_id = fields.Many2one('sale.order', string='Sale Order', ondelete='cascade')
    log_data = fields.Text('Log JSON')
    level = fields.Selection([('info', 'Info'), ('error', 'Error')], default='info')
    create_date = fields.Datetime('Created on', readonly=True)


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    def _split_street(self, street, street2=''):
        """Split street into name, number, suffix (Dutch style)."""
        full = (street or '') + ' ' + (street2 or '')
        full = full.strip()
        m = re.match(r'^(?P<street>.*?)[\s,]+(?P<number>\d+)(?P<suffix>\s*\w*)$', full)
        if m:
            return m.group('street').strip(), m.group('number').strip(), (m.group('suffix') or '').strip()
        return full, '', ''

    def _prepare_monta_order_payload(self):
        """Prepare payload matching Monta /order schema."""
        self.ensure_one()

        shipping_partner = self.partner_shipping_id or self.partner_id
        invoice_partner = self.partner_invoice_id or self.partner_id

        ship_street, ship_number, ship_suffix = self._split_street(shipping_partner.street or '', shipping_partner.street2 or '')
        inv_street, inv_number, inv_suffix = self._split_street(invoice_partner.street or '', invoice_partner.street2 or '')

        payload = {
            "WebshopOrderId": self.name,
            "Reference": self.client_order_ref or "",
            "Origin": "odoo",  # TODO: replace with Monta portal origin code
            "ConsumerDetails": {
                "DeliveryAddress": {
                    "Company": shipping_partner.company_name or "",
                    "FirstName": shipping_partner.name.split(" ")[0],
                    "LastName": " ".join(shipping_partner.name.split(" ")[1:]) or "",
                    "Street": ship_street,
                    "HouseNumber": ship_number,
                    "HouseNumberAddition": ship_suffix,
                    "PostalCode": shipping_partner.zip or "",
                    "City": shipping_partner.city or "",
                    "CountryCode": shipping_partner.country_id.code or "",
                    "PhoneNumber": shipping_partner.phone or shipping_partner.mobile or "",
                    "EmailAddress": shipping_partner.email or ""
                },
                "InvoiceAddress": {
                    "Company": invoice_partner.company_name or "",
                    "FirstName": invoice_partner.name.split(" ")[0],
                    "LastName": " ".join(invoice_partner.name.split(" ")[1:]) or "",
                    "Street": inv_street,
                    "HouseNumber": inv_number,
                    "HouseNumberAddition": inv_suffix,
                    "PostalCode": invoice_partner.zip or "",
                    "City": invoice_partner.city or "",
                    "CountryCode": invoice_partner.country_id.code or "",
                    "PhoneNumber": invoice_partner.phone or invoice_partner.mobile or "",
                    "EmailAddress": invoice_partner.email or ""
                }
            },
            "Lines": [
                {
                    "Sku": line.product_id.default_code or f"product_{line.product_id.id}",
                    "OrderedQuantity": int(line.product_uom_qty)
                } for line in self.order_line
            ],
            "Invoice": {
                "PaymentMethodDescription": self.payment_term_id.name if self.payment_term_id else "",
                "AmountInclTax": float(self.amount_total),
                "TotalTax": float(self.amount_tax),
                "WebshopFactuurID": f"INV-{self.name}",
                "Currency": self.currency_id.name
            }
        }
        return payload

    def _create_monta_log(self, payload, level='info'):
        vals = {
            'sale_order_id': self.id,
            'log_data': json.dumps(payload, default=str),
            'level': level,
            'name': f'Monta {self.name} - {level}',
        }
        self.env['monta.sale.log'].create(vals)
        if level == 'info':
            _logger.info(json.dumps(payload, indent=2, default=str))
        else:
            _logger.error(json.dumps(payload, indent=2, default=str))

    def action_confirm(self):
        res = super(SaleOrder, self).action_confirm()
        for order in self:
            partner = order.partner_id

            # Human-readable pretty logs in Odoo.sh
            _logger.info("✅ Order Confirmed:")
            _logger.info(f"📄 Order: {order.name}")
            _logger.info(f"👤 Customer: {partner.name}")
            _logger.info(f"✉️ Email: {partner.email}")
            _logger.info(f"💰 Total: {order.amount_total}")
            _logger.info(f"🛍️ Order Lines: {[(l.product_id.name, l.product_uom_qty) for l in order.order_line]}")

            # Full Monta payload
            payload = order._prepare_monta_order_payload()

            _logger.info("📦 Monta Payload Details:")
            _logger.info(f"🔹 WebshopOrderId: {payload.get('WebshopOrderId')}")
            _logger.info(f"🔹 Reference: {payload.get('Reference')}")
            _logger.info(f"🔹 Origin: {payload.get('Origin')}")
            _logger.info(f"📬 Delivery Address: {payload['ConsumerDetails']['DeliveryAddress']}")
            _logger.info(f"📮 Invoice Address: {payload['ConsumerDetails']['InvoiceAddress']}")
            _logger.info(f"📦 Order Lines: {payload.get('Lines')}")
            _logger.info(f"🧾 Invoice: {payload.get('Invoice')}")

            # Store log in DB
            order._create_monta_log(payload, level='info')

        return res
