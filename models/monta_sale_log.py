# -*- coding: utf-8 -*-
from odoo import models, fields, api
import logging, json, re, requests, time
from requests.auth import HTTPBasicAuth

_logger = logging.getLogger(__name__)

# =========================
# MONTA CONFIG (one place)
# =========================
MONTA_BASE_URL = "https://api-v6.monta.nl"   # TODO: move to ir.config_parameter
MONTA_USERNAME = "testmoyeeMONTAODOOCONNECTOR"  # TODO: move to ir.config_parameter
MONTA_PASSWORD = "91C4%@$=VL42"                 # TODO: move to ir.config_parameter
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

    # -------- PACK / BUNDLE EXPANSION --------
    def _get_pack_components_from_bom(self, product, qty):
        """Return [(product, qty)] for phantom BoM components, else [].
        Works only if mrp is installed and a phantom BoM exists."""
        components = []
        try:
            Bom = self.env['mrp.bom']
            bom = Bom._bom_find(product=product, company_id=self.company_id.id)
            if bom and bom.type == 'phantom':
                # explode returns (components, operations)
                bom_lines, _ops = bom.explode(product, qty, picking_type=False)
                for line, line_data in bom_lines:
                    comp = line.product_id
                    comp_qty = line_data.get('qty', 0.0)
                    if comp and comp_qty:
                        components.append((comp, comp_qty))
        except Exception:
            # mrp may not be installed or explode not available; ignore gracefully
            pass
        return components

    def _get_pack_components_from_oca_pack(self, product, qty):
        """Return [(product, qty)] for OCA product packs, else []."""
        components = []
        try:
            tmpl = product.product_tmpl_id
            # OCA module: product.template has pack_line_ids; each line has product_id & quantity
            if hasattr(tmpl, 'pack_line_ids') and tmpl.pack_line_ids:
                for pl in tmpl.pack_line_ids:
                    if pl.product_id and pl.quantity:
                        components.append((pl.product_id, pl.quantity * qty))
        except Exception:
            pass
        return components

    def _expand_line_into_components(self, line):
        """Return list of (product, qty) for a sale order line, expanding packs if applicable."""
        product = line.product_id
        qty = line.product_uom_qty or 0.0
        if qty <= 0:
            return []

        # 1) Phantom BoM packs
        comps = self._get_pack_components_from_bom(product, qty)
        if comps:
            return comps

        # 2) OCA product packs
        comps = self._get_pack_components_from_oca_pack(product, qty)
        if comps:
            return comps

        # 3) Not a pack — return the product itself
        return [(product, qty)]

    def _prepare_monta_lines(self):
        """Build Monta 'Lines' from order lines, expanding packs and merging by SKU."""
        sku_qty = {}
        for l in self.order_line:
            for prod, q in self._expand_line_into_components(l):
                if q <= 0:
                    continue
                sku = prod.default_code or f"TESTSKU-{prod.id}"
                sku_qty[sku] = sku_qty.get(sku, 0) + q

        # Monta typically expects integers; adjust if you truly use fractional units
        lines = [{"Sku": sku, "OrderedQuantity": int(qty)} for sku, qty in sku_qty.items() if int(qty) > 0]
        return lines

    def _prepare_monta_order_payload(self):
        self.ensure_one()
        partner = self.partner_id
        street, house_number, house_suffix = self._split_street(partner.street or '', partner.street2 or '')

        # Build lines with pack expansion
        lines = self._prepare_monta_lines()

        # Safe numeric fallback for invoice id
        invoice_id_digits = re.sub(r'\D', '', self.name or '')
        webshop_factuur_id = int(invoice_id_digits) if invoice_id_digits else 9999

        payload = {
            "WebshopOrderId": self.name,  # Monta path id equals our SO name
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
                "TotalTax": float(sum((line.price_tax or 0.0) for line in self.order_line)),
                "WebshopFactuurID": webshop_factuur_id,
                "Currency": self.currency_id.name or "EUR"
            }
        }
        return payload

    def _create_monta_log(self, payload, level='info', tag='Monta API', console_summary=None):
        """Save payload to DB log table and also emit a concise console log line."""
        self.ensure_one()
        vals = {
            'sale_order_id': self.id,
            'log_data': json.dumps(payload, indent=2, default=str),
            'level': level,
            'name': f'{tag} {self.name} - {level}',
        }
        self.env['monta.sale.log'].sudo().create(vals)

        # concise console message
        if console_summary:
            msg = console_summary
        else:
            if isinstance(payload, dict) and payload:
                top_key = next(iter(payload.keys()))
                msg = f"{tag}: {top_key}"
            else:
                msg = f"{tag}: log entry"
        (_logger.info if level == 'info' else _logger.error)(msg)

    def _monta_request(self, method, path, payload=None, headers=None):
        """Low-level HTTP with basic auth + readable logging."""
        base = MONTA_BASE_URL.rstrip('/')
        url = f"{base}/{path.lstrip('/')}"
        headers = headers or {"Content-Type": "application/json", "Accept": "application/json"}

        masked_user = MONTA_USERNAME
        start_time = time.time()
        _logger.info(f"[Monta API] {method.upper()} {url} | User: {masked_user}")

        # full request details saved to DB log
        self._create_monta_log(
            {'request': {'method': method.upper(), 'url': url, 'headers': headers, 'auth_user': masked_user, 'payload': payload or {}}},
            'info', console_summary=f"[Monta API] queued request log for {method.upper()} {url}"
        )

        try:
            resp = requests.request(
                method=method,
                url=url,
                headers=headers,
                json=payload,
                auth=HTTPBasicAuth(MONTA_USERNAME, MONTA_PASSWORD),
                timeout=MONTA_TIMEOUT
            )
            elapsed = time.time() - start_time

            try:
                body = resp.json()
            except Exception:
                body = {'raw': (resp.text or '')[:1000]}

            # concise console line
            log_line = f"[Monta API] {method.upper()} {url} | Status: {resp.status_code} | Time: {elapsed:.2f}s"
            (_logger.info if resp.ok else _logger.error)(log_line)

            # persist full response
            self._create_monta_log(
                {'response': {'status': resp.status_code, 'time_seconds': round(elapsed, 2), 'body': body}},
                'info' if resp.ok else 'error',
                console_summary=f"[Monta API] saved response log for {method.upper()} {url}"
            )
            return resp.status_code, body

        except requests.RequestException as e:
            elapsed = time.time() - start_time
            _logger.error(f"[Monta API] {method.upper()} {url} | Request failed after {elapsed:.2f}s | {str(e)}")
            self._create_monta_log({'exception': str(e)}, 'error', console_summary="[Monta API] saved exception log")
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
        { "Note": "Cancelled" }  and  Content-Type: application/json-patch+json
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
            self._create_monta_log({'delete_failed': body}, 'error', console_summary="[Monta API] delete failed (logged)")
        return status, body

    # -------------------------
    # HOOKS
    # -------------------------
    def action_confirm(self):
        res = super(SaleOrder, self).action_confirm()
        for order in self:
            # first push to Monta (create). If already has an ID, update.
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
    # Legacy sender (kept for testing)
    # -------------------------
    def _send_to_monta(self, payload):
        """Kept for compatibility/testing – prefer _monta_create/_monta_update/_monta_delete."""
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
