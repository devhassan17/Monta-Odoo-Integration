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
    level = fields.Selection([('info','Info'),('error','Error')], default='info')
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
        partner = self.partner_shipping_id or self.partner_id
        street, house_number, house_suffix = self._split_street(partner.street or '', partner.street2 or '')

        payload = {
            "WebshopOrderId": self.name,
            "Reference": self.client_order_ref or "",
            "Origin": "odoo",  # Change if Monta expects specific webshop code
            "ConsumerDetails": {
                "DeliveryAddress": {
                    "Company": partner.company_name or "",
                    "FirstName": partner.name.split(" ")[0],
                    "LastName": " ".join(partner.name.split(" ")[1:]) or "",
                    "Street": street,
                    "HouseNumber": house_number,
                    "HouseNumberAddition": house_suffix,
                    "PostalCode": partner.zip or "",
                    "City": partner.city or "",
                    "CountryCode": partner.country_id.code or "",
                    "PhoneNumber": partner.phone or partner.mobile or "",
                    "EmailAddress": partner.email or ""
                },
                "InvoiceAddress": {
                    "Company": partner.company_name or "",
                    "FirstName": partner.name.split(" ")[0],
                    "LastName": " ".join(partner.name.split(" ")[1:]) or "",
                    "Street": street,
                    "HouseNumber": house_number,
                    "HouseNumberAddition": house_suffix,
                    "PostalCode": partner.zip or "",
                    "City": partner.city or "",
                    "CountryCode": partner.country_id.code or "",
                    "PhoneNumber": partner.phone or partner.mobile or "",
                    "EmailAddress": partner.email or ""
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
            _logger.info(json.dumps(payload, default=str))
        else:
            _logger.error(json.dumps(payload, default=str))

    def action_confirm(self):
        res = super(SaleOrder, self).action_confirm()
        for order in self:
            payload = order._prepare_monta_order_payload()
            order._create_monta_log(payload, level='info')
        return res
