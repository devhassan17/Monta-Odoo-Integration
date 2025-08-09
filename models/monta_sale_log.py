from odoo import models, fields, api
import logging
import json
import re
import requests
from requests.auth import HTTPBasicAuth

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
        """Split street + house number (Dutch style)."""
        full = (street or '') + ' ' + (street2 or '')
        full = full.strip()
        m = re.match(r'^(?P<street>.*?)[\s,]+(?P<number>\d+)(?P<suffix>\s*\w*)$', full)
        if m:
            return m.group('street').strip(), m.group('number').strip(), (m.group('suffix') or '').strip()
        return full, '', ''

    def _prepare_monta_order_payload(self):
        """Prepare payload for Monta NL API."""
        self.ensure_one()
        partner = self.partner_id
        street, house_number, house_suffix = self._split_street(partner.street or '', partner.street2 or '')

        # Compute total weight
        total_weight = sum([(l.product_id.weight or 0.0) * (l.product_uom_qty or 0.0) for l in self.order_line]) or 0.5

        # Order lines
        lines = [{
            "Sku": l.product_id.default_code or f"product_{l.product_id.id}",
            "OrderedQuantity": int(l.product_uom_qty or 0)
        } for l in self.order_line]

        payload = {
            "WebshopOrderId": self.name,
            "Reference": self.client_order_ref or "",
            "Origin": "odoo",
            "ConsumerDetails": {
                "DeliveryAddress": {
                    "Company": partner.company_name or partner.name or "",
                    "FirstName": partner.name.split(' ')[0] if partner.name else "",
                    "LastName": " ".join(partner.name.split(' ')[1:]) if len((partner.name or "").split(' ')) > 1 else "",
                    "Street": street,
                    "HouseNumber": house_number,
                    "HouseNumberAddition": house_suffix,
                    "PostalCode": partner.zip or "",
                    "City": partner.city or "",
                    "CountryCode": partner.country_id.code if partner.country_id else "",
                    "PhoneNumber": partner.phone or "",
                    "EmailAddress": partner.email or ""
                },
                "InvoiceAddress": {
                    "Company": partner.company_name or partner.name or "",
                    "FirstName": partner.name.split(' ')[0] if partner.name else "",
                    "LastName": " ".join(partner.name.split(' ')[1:]) if len((partner.name or "").split(' ')) > 1 else "",
                    "Street": street,
                    "HouseNumber": house_number,
                    "HouseNumberAddition": house_suffix,
                    "PostalCode": partner.zip or "",
                    "City": partner.city or "",
                    "CountryCode": partner.country_id.code if partner.country_id else "",
                    "PhoneNumber": partner.phone or "",
                    "EmailAddress": partner.email or ""
                }
            },
            "Lines": lines,
            "Invoice": {
                "PaymentMethodDescription": self.payment_term_id.name if self.payment_term_id else "",
                "AmountInclTax": float(self.amount_total or 0.0),
                "TotalTax": float(sum(line.price_tax for line in self.order_line)),
                "WebshopFactuurID": f"INV-{self.name}",
                "Currency": self.currency_id.name
            }
        }
        return payload

    def _create_monta_log(self, payload, level='info'):
        """Create a log entry for Monta API payload or response."""
        vals = {
            'sale_order_id': self.id,
            'log_data': json.dumps(payload, indent=2, default=str),
            'level': level,
            'name': f'Monta {self.name} - {level}',
        }
        self.env['monta.sale.log'].sudo().create(vals)  # sudo to bypass ACLs if needed
        if level == 'info':
            _logger.info(json.dumps(payload, indent=2, default=str))
        else:
            _logger.error(json.dumps(payload, indent=2, default=str))

    def _send_to_monta(self, payload):
        """Direct implementation with logging for testing"""
        _logger.info("=== Starting Monta API Request ===")
        
        # API Configuration
        monta_url = "https://api-v6.monta.nl/order"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        
        # Credentials (for testing only)
        username = "testmoyeeMONTAODOOCONNECTOR"
        password = "91C4%@$=VL42"  # Will be masked in logs
        
        try:
            _logger.info(f"Preparing request to: {monta_url}")
            _logger.debug(f"Request payload: {json.dumps(payload, indent=2)}")
            
            # Mask password in logs
            _logger.info(f"Using username: {username}")
            _logger.info("Using password: [REDACTED]")
            
            start_time = fields.Datetime.now()
            response = requests.post(
                monta_url,
                headers=headers,
                json=payload,
                auth=HTTPBasicAuth(username, password),
                timeout=10
            )
            response_time = (fields.Datetime.now() - start_time).total_seconds()
            
            _logger.info(f"Response received in {response_time:.2f}s")
            _logger.info(f"HTTP Status: {response.status_code}")
            _logger.debug(f"Response content: {response.text[:500]}")  # First 500 chars
            
            if response.status_code == 201:
                _logger.info("Monta API request successful")
                return response.json()
            else:
                _logger.error(f"API Error {response.status_code}: {response.text}")
                return {
                    "error": f"API Error {response.status_code}",
                    "details": response.text
                }

        except requests.exceptions.RequestException as e:
            _logger.error(f"Request failed: {type(e).__name__}")
            _logger.error(f"Error details: {str(e)}")
            return {"error": str(e)}
        
        except Exception as e:
            _logger.error("Unexpected error in _send_to_monta")
            _logger.error(f"Error type: {type(e).__name__}")
            _logger.error(f"Error details: {str(e)}")
            return {"error": str(e)}
        
        finally:
            _logger.info("=== Monta API Request Completed ===")

    def action_confirm(self):
        res = super(SaleOrder, self).action_confirm()
        for order in self:
            partner = order.partner_id

            # Log basic order info
            _logger.info("✅ Order Confirmed:")
            _logger.info(f"📄 Order: {order.name}")
            _logger.info(f"👤 Customer: {partner.name}")
            _logger.info(f"✉️ Email: {partner.email}")
            _logger.info(f"💰 Total: {order.amount_total}")
            _logger.info(f"🛍️ Order Lines: {[(l.product_id.name, l.product_uom_qty) for l in order.order_line]}")

            # Prepare Monta payload
            payload = order._prepare_monta_order_payload()
            order._create_monta_log(payload, level='info')

            # Send to Monta
            monta_response = order._send_to_monta(payload)
            order._create_monta_log(monta_response, level='info' if 'error' not in monta_response else 'error')

        return res
