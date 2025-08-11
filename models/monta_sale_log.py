# -*- coding: utf-8 -*-
from odoo import models, fields, api
import logging, json, re, requests
from requests.auth import HTTPBasicAuth

_logger = logging.getLogger(__name__)

# =========================
# MONTA CONFIG (one place)
# =========================
MONTA_BASE_URL = "https://api-v6.monta.nl"  # change to param later if needed
MONTA_USERNAME = "testmoyeeMONTAODOOCONNECTOR"  # TODO: move to ir.config_parameter
MONTA_PASSWORD = "91C4%@$=VL42"               # TODO: move to ir.config_parameter
MONTA_TIMEOUT  = 20

# =========================
# LOG MODEL
# =========================
class MontaSaleLog(models.Model):
    _name = 'monta.sale.log'
    _description = 'Monta API logs'

    name = fields.Char('Log Name')
    sale_order_id = fields.Many2one('sale.order', string='Sale Order', ondelete='cascade')
    log_data = fields.Text('Log JSON')
    level = fields.Selection([('info','Info'),('error','Error')], default='info')
    create_date = fields.Datetime('Created on', readonly=True)


# =========================
# SALE ORDER EXTENSION
# =========================
class SaleOrder(models.Model):
    _inherit = 'sale.order'

    # Track upstream identity/state
    monta_order_id = fields.Char('Monta WebshopOrderId', copy=False, index=True)
    monta_sync_state = fields.Selection([
        ('draft', 'Draft'),
        ('sent', 'Sent'),
        ('updated', 'Updated'),
        ('cancelled', 'Cancelled'),
        ('error', 'Error'),
    ], default='draft', copy=False)
    monta_last_push = fields.Datetime('Last Push to Monta', copy=False)
    monta_needs_sync = fields.Boolean('Needs Monta Sync', default=False, copy=False)

    # -------------------------
    # Helpers
    # -------------------------
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
            qty = int(l.product_uom_qty or 0)
            if qty <= 0:
                continue
            sku = l.product_id.default_code or f"TESTSKU-{l.product_id.id}"
            lines.append({"Sku": sku, "OrderedQuantity": qty})

        payload = {
            "WebshopOrderId": self.name,  # Monta's path id equals our SO name
            "Reference": self.client_order_ref or "",
            "Origin": "Moyee_Odoo",
            "ConsumerDetails": {
                "DeliveryAddress": {
                    "Company": partner.company_name or partner.name or "",
                    "FirstName": partner.name.split(' ')[0] if partner.name else "",
                    "LastName": " ".join((partner.name or "").split(' ')[1:]) if len((partner.name or "").split(' ')) > 1 else "",
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
                    "LastName": " ".join((partner.name or "").split(' ')[1:]) if len((partner.name or "").split(' ')) > 1 else "",
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
                "PaymentMethodDescription": "Odoo Order",
                "AmountInclTax": float(self.amount_total or 0.0),
                "TotalTax": float(sum(line.price_tax for line in self.order_line)),
                # just a numeric for test; Monta doesn't require uniqueness here
                "WebshopFactuurID": int(re.sub(r'\D', '', self.name)) or 9999,
                "Currency": self.currency_id.name or "EUR"
            }
        }
        return payload

    def _create_monta_log(self, payload, level='info'):
        self.ensure_one()
        vals = {
            'sale_order_id': self.id,
            'log_data': json.dumps(payload, indent=2, default=str),
            'level': level,
            'name': f'Monta {self.name} - {level}',
        }
        self.env['monta.sale.log'].sudo().create(vals)
        if level == 'info':
            _logger.info(vals['log_data'])
        else:
            _logger.error(vals['log_data'])

    def _monta_request(self, method, path, payload=None, headers=None):
        """Low-level HTTP with basic auth + logging."""
        base = MONTA_BASE_URL.rstrip('/')
        url = f"{base}/{path.lstrip('/')}"
        headers = headers or {"Content-Type": "application/json", "Accept": "application/json"}

        self._create_monta_log({'request': {'method': method, 'url': url, 'payload': payload or {}}}, 'info')

        try:
            resp = requests.request(
                method=method,
                url=url,
                headers=headers,
                json=payload,
                auth=HTTPBasicAuth(MONTA_USERNAME, MONTA_PASSWORD),
                timeout=MONTA_TIMEOUT
            )
            try:
                body = resp.json()
            except Exception:
                body = {'raw': (resp.text or '')[:1000]}
            self._create_monta_log({'response': {'status': resp.status_code, 'body': body}}, 'info' if resp.ok else 'error')
            return resp.status_code, body
        except requests.RequestException as e:
            self._create_monta_log({'exception': str(e)}, 'error')
            return 0, {'error': str(e)}

    # -------------------------
    # CREATE / UPDATE / DELETE
    # -------------------------
    def _monta_create(self):
        """POST /order to create the order in Monta."""
        self.ensure_one()
        payload = self._prepare_monta_order_payload()
        status, body = self._monta_request('POST', '/order', payload)
        if status in (200, 201):
            self.write({
                'monta_order_id': self.name,  # WebshopOrderId is our SO name
                'monta_sync_state': 'sent',
                'monta_last_push': fields.Datetime.now(),
                'monta_needs_sync': False,
            })
        else:
            self.write({'monta_sync_state': 'error', 'monta_needs_sync': True})
        return status, body

    def _monta_update(self):
        """PUT /order/{webshoporderid} to update."""
        self.ensure_one()
        webshop_id = self.monta_order_id or self.name
        payload = self._prepare_monta_order_payload()
        path = f"/order/{webshop_id}"
        status, body = self._monta_request('PUT', path, payload)
        if 200 <= status < 300:
            self.write({
                'monta_order_id': webshop_id,
                'monta_sync_state': 'updated' if self.monta_sync_state != 'sent' else 'sent',
                'monta_last_push': fields.Datetime.now(),
                'monta_needs_sync': False,
            })
        else:
            self.write({'monta_sync_state': 'error', 'monta_needs_sync': True})
        return status, body

    def _monta_delete(self, note="Cancelled from Odoo"):
        """
        DELETE /order/{webshoporderid} with JSON body:
        { "Note": "Cancelled" } and Content-Type: application/json-patch+json
        """
        self.ensure_one()
        webshop_id = self.monta_order_id or self.name
        if not webshop_id:
            return 204, {'note': 'No webshoporderid stored; nothing to delete.'}

        headers = {
            "Content-Type": "application/json-patch+json",
            "Accept": "application/json"
        }
        path = f"/order/{webshop_id}"
        status, body = self._monta_request('DELETE', path, {"Note": note}, headers=headers)
        if status in (200, 204):
            self.write({'monta_sync_state': 'cancelled'})
        else:
            self._create_monta_log({'delete_failed': body}, 'error')
        return status, body

    # -------------------------
    # HOOKS
    # -------------------------
    def action_confirm(self):
        res = super(SaleOrder, self).action_confirm()
        for order in self:
            # first push to Monta (create)
            if not order.monta_order_id:
                order._monta_create()
            else:
                order._monta_update()
        return res

    def write(self, vals):
        # mark for sync when relevant fields change
        tracked_fields = {'partner_id', 'order_line', 'client_order_ref', 'validity_date', 'commitment_date'}
        if any(f in vals for f in tracked_fields):
            vals.setdefault('monta_needs_sync', True)

        res = super(SaleOrder, self).write(vals)

        # auto-push updates for confirmed orders
        for order in self.filtered(lambda o: o.state in ('sale', 'done') and o.monta_needs_sync and o.state != 'cancel'):
            order._monta_update()
        return res

    def action_cancel(self):
        res = super(SaleOrder, self).action_cancel()
        for order in self:
            order._monta_delete(note="Cancelled")
        return res

    def unlink(self):
        for order in self:
            if order.state in ('sale', 'done') and (order.monta_order_id or order.name):
                order._monta_delete(note="Deleted from Odoo (unlink)")
        return super(SaleOrder, self).unlink()

    # -------------------------
    # Your original direct sender (kept for reference; not used now)
    # -------------------------
    def _send_to_monta(self, payload):
        """Kept for compatibility/testing – use _monta_create/_monta_update/_monta_delete instead."""
        monta_url = f"{MONTA_BASE_URL.rstrip('/')}/order"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        try:
            response = requests.post(
                monta_url, headers=headers, json=payload,
                auth=HTTPBasicAuth(MONTA_USERNAME, MONTA_PASSWORD),
                timeout=MONTA_TIMEOUT
            )
            if response.status_code in (200, 201):
                return response.json()
            else:
                return {"error": f"API Error {response.status_code}", "details": response.text}
        except requests.exceptions.RequestException as e:
            return {"error": str(e)}
