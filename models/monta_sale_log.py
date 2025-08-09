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
        """Send payload to Monta using Basic Auth with enhanced error handling."""
        monta_url = "https://api-v6.monta.nl/orders"
        monta_username = "testmoyeeMONTAODOOCONNECTOR"  # TODO: Move to config parameters
        monta_password = "<91C4%@$=VL42"  # TODO: Move to config parameters
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Odoo-Monta-Integration/1.0"
        }

        try:
            # Pre-request logging
            _logger.info("🌐 Initiating Monta API Connection...")
            _logger.info(f"🔗 Endpoint: {monta_url}")
            _logger.info(f"📦 Payload Size: {len(json.dumps(payload))} bytes")
            
            # API Request
            start_time = fields.Datetime.now()
            response = requests.post(
                monta_url,
                headers=headers,
                json=payload,
                auth=HTTPBasicAuth(monta_username, monta_password),
                timeout=(5, 10)  # Connect timeout 5s, read timeout 10s
            )
            response_time = (fields.Datetime.now() - start_time).total_seconds()
            
            # Response logging
            _logger.info(f"📤 Monta API Response: HTTP {response.status_code}")
            _logger.info(f"⏱️ Response Time: {response_time:.2f}s")
            _logger.debug(f"📄 Response Content: {response.text[:500]}...")  # Log first 500 chars
            
            # Handle non-2xx responses
            if not response.ok:
                _logger.warning(f"⚠️ Monta API Warning: {response.status_code} - {response.reason}")
                return {
                    "error": f"API Error {response.status_code}",
                    "details": response.text,
                    "status_code": response.status_code
                }
            
            return response.json()
            
        except requests.exceptions.Timeout as e:
            _logger.error("⏱️ Monta API Timeout: The request timed out")
            return {
                "error": "Connection Timeout",
                "details": str(e),
                "suggestion": "Check your network connection or increase timeout"
            }
            
        except requests.exceptions.SSLError as e:
            _logger.error("🔐 SSL Certificate Error: Failed to verify Monta's SSL certificate")
            _logger.error(f"SSL Error Details: {str(e)}")
            return {
                "error": "SSL Verification Failed",
                "details": str(e),
                "suggestion": "Verify Monta's SSL certificate or add exception"
            }
            
        except requests.exceptions.ConnectionError as e:
            _logger.error("🔌 Connection Error: Failed to establish connection")
            _logger.error(f"Connection Error Details: {str(e)}")
            return {
                "error": "Connection Failed",
                "details": str(e),
                "suggestion": "Check network connectivity, firewall rules, and DNS resolution"
            }
            
        except requests.exceptions.RequestException as e:
            _logger.error("🚨 General Request Exception")
            _logger.error(f"Error Details: {str(e)}")
            return {
                "error": "API Request Failed",
                "details": str(e),
                "suggestion": "Review request parameters and try again"
            }
            
        except json.JSONDecodeError as e:
            _logger.error("📄 JSON Decode Error: Failed to parse Monta's response")
            _logger.error(f"JSON Error: {str(e)}")
            return {
                "error": "Invalid API Response",
                "details": str(e),
                "suggestion": "Verify Monta API is returning valid JSON"
            }
            
        except Exception as e:
            _logger.error("💥 Unexpected Error in _send_to_monta")
            _logger.error(f"Error Type: {type(e).__name__}")
            _logger.error(f"Error Details: {str(e)}")
            return {
                "error": "Unexpected Error",
                "details": str(e),
                "type": type(e).__name__
            }

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
