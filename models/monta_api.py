import logging
import json
import re
import requests
from requests.auth import HTTPBasicAuth
from odoo import models, fields

_logger = logging.getLogger(__name__)

class MontaAPI(models.AbstractModel):
    _name = 'monta.api.mixin'
    _description = 'Monta API Helper Methods'

    def _split_street(self, street, street2=''):
        """Split street + house number (Dutch style)."""
        full = (street or '') + ' ' + (street2 or '')
        full = full.strip()
        m = re.match(r'^(?P<street>.*?)[\s,]+(?P<number>\d+)(?P<suffix>\s*\w*)$', full)
        if m:
            return m.group('street').strip(), m.group('number').strip(), (m.group('suffix') or '').strip()
        return full, '', ''

    def _prepare_monta_order_payload(self):
        self.ensure_one()
        partner = self.partner_id
        street, house_number, house_suffix = self._split_street(partner.street or '', partner.street2 or '')

        lines = []
        for l in self.order_line:
            sku = l.product_id.default_code or f"TESTSKU-{l.product_id.id}"
            lines.append({
                "Sku": sku,
                "OrderedQuantity": int(l.product_uom_qty or 0)
            })

        payload = {
            "WebshopOrderId": self.name,
            "Reference": self.client_order_ref or "",
            "Origin": "Moyee_Odoo",
            "ConsumerDetails": {
                "DeliveryAddress": {
                    "Company": partner.company_name or partner.name or "",
                    "FirstName": partner.name.split(' ')[0] if partner.name else "",
                    "LastName": " ".join(partner.name.split(' ')[1:]) if len((partner.name or "").split(' ')) > 1 else "",
                    "Street": street,
                    "HouseNumber": house_number or "1",
                    "HouseNumberAddition": house_suffix or "",
                    "PostalCode": partner.zip or "0000AA",
                    "City": partner.city or "TestCity",
                    "CountryCode": partner.country_id.code if partner.country_id else "NL",
                    "PhoneNumber": partner.phone or "0000000000",
                    "EmailAddress": partner.email or "test@example.com"
                },
                "InvoiceAddress": {
                    "Company": partner.company_name or partner.name or "",
                    "FirstName": partner.name.split(' ')[0] if partner.name else "",
                    "LastName": " ".join(partner.name.split(' ')[1:]) if len((partner.name or "").split(' ')) > 1 else "",
                    "Street": street,
                    "HouseNumber": house_number or "1",
                    "HouseNumberAddition": house_suffix or "",
                    "PostalCode": partner.zip or "0000AA",
                    "City": partner.city or "TestCity",
                    "CountryCode": partner.country_id.code if partner.country_id else "NL",
                    "PhoneNumber": partner.phone or "0000000000",
                    "EmailAddress": partner.email or "test@example.com"
                }
            },
            "Lines": lines,
            "Invoice": {
                "PaymentMethodDescription": "Odoo Test Order",
                "AmountInclTax": float(self.amount_total or 0.0),
                "TotalTax": float(sum(line.price_tax for line in self.order_line)),
                "WebshopFactuurID": int(re.sub(r'\D', '', self.name)) or 9999,
                "Currency": self.currency_id.name or "EUR"
            }
        }
        return payload

    def _send_to_monta(self, payload):
        """Send request to Monta API."""
        monta_url = "https://api-v6.monta.nl/order"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        username = "testmoyeeMONTAODOOCONNECTOR"
        password = "91C4%@$=VL42"

        try:
            _logger.info(f"Sending Monta request for order {self.name}")
            response = requests.post(
                monta_url,
                headers=headers,
                json=payload,
                auth=HTTPBasicAuth(username, password),
                timeout=10
            )
            if response.status_code == 201:
                return response.json()
            return {"error": f"API Error {response.status_code}", "details": response.text}
        except requests.exceptions.RequestException as e:
            return {"error": str(e)}
