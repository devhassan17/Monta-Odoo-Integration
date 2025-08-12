# -*- coding: utf-8 -*-
from odoo import models, fields, api
from odoo.exceptions import ValidationError
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
# PRODUCT EXTENSION (SKU MAP + auto-resync)
# =========================
class ProductProduct(models.Model):
    _inherit = 'product.product'

    monta_sku = fields.Char(
        string="Monta SKU",
        help="Explicit SKU used when sending orders to Monta. "
             "If empty, connector tries: default_code → first supplier code → barcode."
    )

    def write(self, vals):
        res = super().write(vals)

        # If identifiers changed, trigger resync for related open orders
        sku_related = {'monta_sku', 'default_code', 'barcode', 'seller_ids'}
        if sku_related.intersection(vals.keys()):
            try:
                self._trigger_monta_resync_for_open_orders()
            except Exception as e:
                _logger.error(f"[Monta Resync] Failed to trigger resync after product write: {e}")
        return res

    def _trigger_monta_resync_for_open_orders(self):
        """Mark related open orders for sync and push update immediately."""
        if not self:
            return
        SOL = self.env['sale.order.line']
        # find sale/done orders (not cancelled) that include these products
        lines = SOL.search([
            ('product_id', 'in', self.ids),
            ('order_id.state', 'in', ('sale', 'done')),
        ])
        orders = lines.mapped('order_id').filtered(lambda o: o.state != 'cancel')
        if not orders:
            return

        # avoid hammering: update flag in batch, then push with small debounce at order level
        orders.write({'monta_needs_sync': True})
        for o in orders:
            try:
                if hasattr(o, '_should_push_now'):
                    if o._should_push_now():
                        o._monta_update()
                else:
                    o._monta_update()
            except Exception as e:
                _logger.error(f"[Monta Resync] Order {o.name} update after SKU fix failed: {e}")


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

    # Optional debounce to avoid many rapid PUTs
    def _should_push_now(self, min_gap_seconds=2):
        if not self.monta_last_push:
            return True
        delta = fields.Datetime.now() - self.monta_last_push
        try:
            return delta.total_seconds() >= min_gap_seconds
        except Exception:
            return True

    # -------- SKU RESOLUTION --------
    def _get_sku_for_monta(self, product):
        """
        Resolve Monta SKU with fallbacks:
        1) product.monta_sku
        2) product.default_code
        3) first supplierinfo code
        4) product.barcode
        Returns (sku or '', source_str)
        """
        sku = getattr(product, 'monta_sku', False)
        if sku:
            return sku.strip(), 'monta_sku'
        if product.default_code:
            return product.default_code.strip(), 'default_code'
        # supplier code (first)
        seller = product.seller_ids[:1]
        if seller and seller.product_code:
            return seller.product_code.strip(), 'supplier_code'
        if product.barcode:
            return product.barcode.strip(), 'barcode'
        return '', 'missing'

    # -------- PACK / BUNDLE EXPANSION --------
    def _get_pack_components_from_bom(self, product, qty):
        """Return [(product, qty)] for phantom BoM components, else [] (MRP)."""
        components = []
        try:
            Bom = self.env['mrp.bom']
            bom = Bom._bom_find(product=product, company_id=self.company_id.id)
            if bom and bom.type == 'phantom':
                bom_lines, _ops = bom.explode(product, qty, picking_type=False)
                for line, line_data in bom_lines:
                    comp = line.product_id
                    comp_qty = line_data.get('qty', 0.0)
                    if comp and comp_qty:
                        components.append((comp, comp_qty))
        except Exception:
            pass
        return components

    def _get_pack_components_from_oca_pack(self, product, qty):
        """Return [(product, qty)] for OCA product packs, else []."""
        components = []
        try:
            tmpl = product.product_tmpl_id
            if hasattr(tmpl, 'pack_line_ids') and tmpl.pack_line_ids:
                for pl in tmpl.pack_line_ids:
                    if pl.product_id and pl.quantity:
                        components.append((pl.product_id, pl.quantity * qty))
        except Exception:
            pass
        return components

    def _expand_line_into_components(self, line):
        """
        Return (components, pack_info)
        components: list[(product, qty)]
        pack_info: None OR dict describing pack & its components (for logging)
        """
        product = line.product_id
        qty = line.product_uom_qty or 0.0
        if qty <= 0:
            return [], None

        # 1) Phantom BoM packs
        comps = self._get_pack_components_from_bom(product, qty)
        source = 'mrp_phantom'
        if not comps:
            # 2) OCA packs
            comps = self._get_pack_components_from_oca_pack(product, qty)
            source = 'oca_pack' if comps else None

        if comps:
            # build pack_info for logging with resolved SKUs
            comp_list = []
            for p, q in comps:
                sku, _src = self._get_sku_for_monta(p)
                comp_list.append({
                    'product_id': p.id,
                    'name': p.display_name or p.name,
                    'qty': q,
                    'sku': sku or '',
                })
            pack_info = {
                'line_id': line.id,
                'pack_product_id': product.id,
                'pack_name': product.display_name or product.name,
                'qty': qty,
                'source': source,
                'components': comp_list,
            }
            return comps, pack_info

        # 3) Not a pack
        return [(product, qty)], None

    def _prepare_monta_lines(self):
        """Build Monta 'Lines' from order lines, expanding packs and merging by SKU.
        - Validates SKU presence and shows the resolved SKU (or EMPTY) in ValidationError.
        - Logs pack expansion with component SKUs.
        """
        sku_qty = {}
        missing = []
        pack_logs = []

        for l in self.order_line:
            comps, pack_info = self._expand_line_into_components(l)
            if pack_info:
                pack_logs.append(pack_info)

            for prod, q in comps:
                if q <= 0:
                    continue
                sku, source = self._get_sku_for_monta(prod)
                if not sku:
                    missing.append({
                        'product_id': prod.id,
                        'product_display_name': prod.display_name or prod.name or f'ID {prod.id}',
                        'resolved_sku': sku or 'EMPTY',
                        'line_id': l.id,
                    })
                    continue
                # aggregate by sku
                sku_qty[sku] = sku_qty.get(sku, 0) + q

        if pack_logs:
            self._create_monta_log(
                {'pack_expansion': pack_logs},
                level='info',
                tag='Monta Pack Expansion',
                console_summary=f"[Monta Pack Expansion] {len(pack_logs)} pack line(s) expanded"
            )

        if missing:
            # Save full detail to logs
            self._create_monta_log(
                {'missing_skus': missing},
                level='error',
                tag='Monta SKU check',
                console_summary=f"[Monta SKU check] {len(missing)} product(s) missing SKU mapping"
            )
            # Build a readable error including resolved SKU values
            msg_lines = ["Cannot push to Monta: some products have no mapped SKU."]
            for m in missing:
                msg_lines.append(f"- {m['product_display_name']} → SKU: {m['resolved_sku']}")
            raise ValidationError("\n".join(msg_lines))

        # Monta typically expects integers; remove int() if you use fractional UoM
        lines = [{"Sku": sku, "OrderedQuantity": int(qty)} for sku, qty in sku_qty.items() if int(qty) > 0]
        if not lines:
            raise ValidationError("Cannot push to Monta: order lines expanded to empty/zero quantities.")
        return lines

    def _prepare_monta_order_payload(self):
        self.ensure_one()
        partner = self.partner_id
        street, house_number, house_suffix = self._split_street(partner.street or '', partner.street2 or '')

        lines = self._prepare_monta_lines()

        # Safe numeric fallback for invoice id
        invoice_id_digits = re.sub(r'\D', '', self.name or '')
        webshop_factuur_id = int(invoice_id_digits) if invoice_id_digits else 9999

        payload = {
            "WebshopOrderId": self.name,
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

            log_line = f"[Monta API] {method.upper()} {url} | Status: {resp.status_code} | Time: {elapsed:.2f}s"
            (_logger.info if resp.ok else _logger.error)(log_line)

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
                'monta_order_id': self.name,
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
            if order._should_push_now():
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
