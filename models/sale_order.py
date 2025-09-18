# -*- coding: utf-8 -*-
import json, re, logging
from collections import defaultdict
from odoo import models, fields
from odoo.exceptions import ValidationError

from ..services.monta_client import MontaClient
from ..utils.address import split_street
from ..utils.sku import resolve_sku
from ..utils.pack import expand_to_leaf_components, is_pack_like, get_pack_components

_logger = logging.getLogger(__name__)

# Only send orders from this instance (guards against staging/live duplicates)
ALLOWED_INSTANCE_URL = "https://moyeecoffee-odoo-monta-plugin-22993258.dev.odoo.com/"


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    monta_order_id = fields.Char(copy=False, index=True)
    monta_sync_state = fields.Selection([
        ('draft', 'Draft'),
        ('sent', 'Sent'),
        ('updated', 'Updated'),
        ('cancelled', 'Cancelled'),
        ('error', 'Error'),
    ], default='draft', copy=False)
    monta_last_push = fields.Datetime(copy=False)
    monta_needs_sync = fields.Boolean(default=False, copy=False)

    # ---------------- helpers ----------------
    def _split_street(self, street, street2=''):
        return split_street(street, street2)

    def _should_push_now(self, min_gap_seconds=2):
        if not self.monta_last_push:
            return True
        delta = fields.Datetime.now() - self.monta_last_push
        try:
            return delta.total_seconds() >= min_gap_seconds
        except Exception:
            return True

    def _is_allowed_instance(self):
        ICP = self.env['ir.config_parameter'].sudo()
        web_url = (ICP.get_param('web.base.url') or '').strip().rstrip('/') + '/'
        allowed = (ALLOWED_INSTANCE_URL or '').strip().rstrip('/') + '/'
        ok = web_url.lower() == allowed.lower()
        if not ok:
            _logger.warning("[Monta Guard] Not sending order %s. web.base.url=%s expected=%s",
                            self.name, web_url, allowed)
            self._create_monta_log(
                {'guard': {'web_base_url': web_url, 'allowed': allowed, 'blocked': True}},
                level='info', tag='Monta Guard', console_summary='[Monta Guard] blocked by instance URL'
            )
        return ok

    # ------------- EDD immediate pull + step logs -------------
    def _edd_pretty(self, dt_str):
        """Return dd/MM/YYYY HH:mm:ss for log readability."""
        try:
            if not dt_str:
                return ''
            y, m, d = int(dt_str[0:4]), int(dt_str[5:7]), int(dt_str[8:10])
            hh, mm, ss = int(dt_str[11:13]), int(dt_str[14:16]), int(dt_str[17:19])
            return f"{d:02d}/{m:02d}/{y:04d} {hh:02d}:{mm:02d}:{ss:02d}"
        except Exception:
            return dt_str or ''

    def _pull_and_apply_edd_now(self):
        """
        Pull Monta order and apply ETA to commitment_date immediately
        so the sales page shows Expected Delivery right after order is sent/updated.
        """
        try:
            # Step 1: Request sent
            self._create_monta_log(
                {'edd_auto': {'step': 'Request Sent To Monta'}},
                level='info', tag='Monta EDD', console_summary='[EDD] Request Sent To Monta'
            )
            before = self.commitment_date
            self.action_monta_pull_now()
            after = self.commitment_date
            # Step 2..5: summarize
            pretty = self._edd_pretty(after)
            msg = {
                'edd_auto': {
                    'step': 'EDD Result',
                    'Date Get': True,
                    'Date is that': after,
                    'Date is added to Commitment date': (before or '') != (after or ''),
                    'Date is showing': bool(after),
                    'pretty_for_ui': pretty,
                    'order_url_hint': f"/odoo/sales/{self.id}",
                }
            }
            self._create_monta_log(msg, level='info', tag='Monta EDD',
                                   console_summary=f"[EDD] Set {after} (pretty {pretty})")
            try:
                self.message_post(body=(
                    "<b>EDD Auto</b><br/>"
                    "✓ Request Sent To Monta<br/>"
                    "✓ Date Get<br/>"
                    f"• Date is that: <code>{after or '-'}</code><br/>"
                    f"• Date is added to Commitment date: <b>{'Yes' if (before or '') != (after or '') else 'No'}</b><br/>"
                    f"✓ Date is showing: <b>{pretty or '-'}</b>"
                ))
            except Exception:
                pass
        except Exception as e:
            _logger.error("[Monta EDD] Immediate pull failed for %s: %s", self.name, e, exc_info=True)

    # ---------------- logging ----------------
    def _log_pack_variant_skus_for_order(self):
        packs_scanned = 0
        for line in self.order_line:
            p = line.product_id
            if not p or not is_pack_like(self.env, p, self.company_id.id):
                continue
            qty = float(line.product_uom_qty or 0.0)
            comps = get_pack_components(self.env, self.company_id.id, p, qty)
            _logger.info("[Monta Pack Debug] ORDER %s | PACK %s | VARIANT %s | Qty=%s | comps=%s",
                         self.name, p.product_tmpl_id.display_name, p.display_name, qty, len(comps))
            for comp, cqty in comps:
                sku, src = resolve_sku(comp, env=self.env, allow_synthetic=False)
                _logger.info("[Monta Pack Debug]    - %s | qty=%s | sku=%s | src=%s",
                             comp.display_name, cqty, sku or 'EMPTY', src)
                try:
                    self.env['sku_test.log'].create({
                        'order_id': self.id,
                        'order_line_id': line.id,
                        'pack_product_id': p.id,
                        'component_product_id': comp.id,
                        'sku': sku or '',
                    })
                except Exception:
                    pass
            packs_scanned += 1
        _logger.info("[Monta Pack Debug] %s pack line(s) scanned", packs_scanned)

    def _log_all_skus_now(self):
        rows = []
        for l in self.order_line:
            p = l.product_id
            if not p:
                continue
            qty = float(l.product_uom_qty or 0.0)
            leaves = expand_to_leaf_components(self.env, self.company_id.id, p, qty)
            for comp, q in leaves:
                sku, _ = resolve_sku(comp, env=self.env, allow_synthetic=False)
                _logger.info("[Monta Order SKUs] %s → %s | qty=%s | sku=%s",
                             self.name, comp.display_name, q, sku or 'EMPTY')
                rows.append({'line_id': l.id, 'product_id': comp.id, 'qty': q, 'sku': sku or 'EMPTY'})
        self._create_monta_log({'sku_log': rows}, level='info', tag='Monta SKU Log',
                               console_summary=f"[Monta] Logged {len(rows)} SKU row(s)")

    # ---------------- Lines (ONLY child product SKUs) ----------------
    def _prepare_monta_lines(self):
        from math import isfinite
        sku_qty = defaultdict(float)
        missing = []

        for l in self.order_line:
            p = l.product_id
            if not p:
                continue
            qty = float(l.product_uom_qty or 0.0)
            if qty <= 0:
                continue

            leaves = expand_to_leaf_components(self.env, self.company_id.id, p, qty)
            if not leaves:
                missing.append(f"'{p.display_name}' has no resolvable components.")
                continue

            for comp, q in leaves:
                sku, _ = resolve_sku(comp, env=self.env, allow_synthetic=False)
                if not sku:
                    missing.append(f"Component '{comp.display_name}' is missing a real SKU.")
                    continue
                try:
                    qv = float(q or 0.0)
                    if not isfinite(qv):
                        qv = 0.0
                except Exception:
                    qv = 0.0
                sku_qty[sku] += qv

        if missing:
            self._create_monta_log({'missing_skus': missing}, level='error', tag='Monta SKU check',
                                   console_summary=f"[Monta SKU check] {len(missing)} missing")
            raise ValidationError("Cannot push to Monta:\n- " + "\n- ".join(missing))

        lines = [{"Sku": sku, "OrderedQuantity": int(q)} for sku, q in sku_qty.items() if int(q) > 0]
        if not lines:
            raise ValidationError("Order lines expanded to empty/zero quantities.")

        self._create_monta_log({'lines': lines}, level='info', tag='Monta Lines',
                               console_summary=f"[Monta] Prepared {len(lines)} line(s)")
        _logger.info("[Monta Payload] %s -> Lines: %s", self.name, json.dumps(lines))
        return lines

    # ---------------- payload ----------------
    def _prepare_monta_order_payload(self):
        self.ensure_one()
        partner = self.partner_id
        street, house_number, house_suffix = self._split_street(partner.street or '', partner.street2 or '')
        lines = self._prepare_monta_lines()
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

    # ---------------- logging to DB + server ----------------
    def _create_monta_log(self, payload, level='info', tag='Monta API', console_summary=None):
        self.ensure_one()
        vals = {
            'sale_order_id': self.id,
            'log_data': json.dumps(payload, indent=2, default=str),
            'level': level,
            'name': f'{tag} {self.name} - {level}',
        }
        self.env['monta.sale.log'].sudo().create(vals)
        (_logger.info if level == 'info' else _logger.error)(f"[{tag}] {console_summary or vals['name']}")

    # ---------------- HTTP plumbing with instance guard ----------------
    def _monta_request(self, method, path, payload=None, headers=None):
        if not self._is_allowed_instance():
            return 0, {'note': 'Blocked by instance URL guard'}
        client = MontaClient(self.env)
        return client.request(self, method, path, payload=payload, headers=headers)

    # ---------------- API calls ----------------
    def _monta_create(self):
        """Minimal change: POST to /orders (v6)."""
        self.ensure_one()
        _logger.info("[Monta] Creating order %s", self.name)
        status, body = self._monta_request('POST', '/orders', self._prepare_monta_order_payload())
        if 200 <= status < 300:
            self.write({
                'monta_order_id': self.name,
                'monta_sync_state': 'sent',
                'monta_last_push': fields.Datetime.now(),
                'monta_needs_sync': False,
            })
            self._create_monta_log({'status': status, 'body': body}, 'info', 'Monta Create', '[Monta] order created')
            # Pull ETA now so commitment_date is set immediately
            self._pull_and_apply_edd_now()
        else:
            self.write({'monta_sync_state': 'error', 'monta_needs_sync': True})
            self._create_monta_log({'status': status, 'body': body}, 'error', 'Monta Create', '[Monta] create failed')
            try:
                self.message_post(body=f"<b>Monta create failed.</b><br/><pre>{json.dumps(body, indent=2, ensure_ascii=False)}</pre>")
            except Exception:
                pass
        return status, body

    def _monta_update(self):
        """
        Minimal change: resolve Monta internal Id via /orders?clientReference=...,
        then PUT /orders/{Id}. Keep your fallback POST and all logging/states.
        """
        self.ensure_one()
        webshop_id = self.monta_order_id or self.name
        _logger.info("[Monta] Updating order %s (idempotent by name)", webshop_id)

        # 1) Try to find order by clientReference/search
        order_id = None
        g_status, g_body = self._monta_request('GET', f"/orders?clientReference={webshop_id}")
        if 200 <= g_status < 300 and isinstance(g_body, list) and g_body:
            order_id = g_body[0].get('Id') or g_body[0].get('id')

        if not order_id:
            # last resort: generic search param
            g2_status, g2_body = self._monta_request('GET', f"/orders?search={webshop_id}")
            if 200 <= g2_status < 300 and isinstance(g2_body, list) and g2_body:
                order_id = g2_body[0].get('Id') or g2_body[0].get('id')

        # 2) If found -> PUT by Id, else try POST (keeps your behavior)
        if order_id:
            path = f"/orders/{order_id}"
            status, body = self._monta_request('PUT', path, self._prepare_monta_order_payload())
            if 200 <= status < 300:
                self.write({
                    'monta_order_id': webshop_id,
                    'monta_sync_state': 'updated' if self.monta_sync_state != 'sent' else 'sent',
                    'monta_last_push': fields.Datetime.now(),
                    'monta_needs_sync': False,
                })
                self._create_monta_log({'status': status, 'body': body}, 'info', 'Monta Update', '[Monta] order updated')
                self._pull_and_apply_edd_now()
                return status, body
        else:
            # no Monta Id found, go straight to create
            status, body = self._monta_request('POST', '/orders', self._prepare_monta_order_payload())
            if 200 <= status < 300:
                self.write({
                    'monta_order_id': webshop_id,
                    'monta_sync_state': 'sent',
                    'monta_last_push': fields.Datetime.now(),
                    'monta_needs_sync': False,
                })
                self._create_monta_log({'status': status, 'body': body}, 'info', 'Monta Create',
                                       '[Monta] order created (no prior record)')
                self._pull_and_apply_edd_now()
                return status, body
            # fall through to your original error handling below
        # ---------------- keep ALL your existing error logic ----------------
        reason_codes = []
        try:
            reason_codes = [r.get('Code') for r in (body or {}).get('OrderInvalidReasons', [])]
        except Exception:
            pass
        if status == 400 and 42 in reason_codes:
            self.write({'monta_sync_state': 'error', 'monta_needs_sync': True})
            self._create_monta_log({'status': status, 'body': body}, 'error', 'Monta Update',
                                   '[Monta] order under verification; will retry later')
            try:
                self.message_post(body=f"<b>Monta update deferred (verification).</b><br/><pre>{json.dumps(body, indent=2, ensure_ascii=False)}</pre>")
            except Exception:
                pass
            return status, body

        if status in (400, 404):
            _logger.warning("[Monta] Update failed (%s) for %s; attempting create...", status, webshop_id)
            c_status, c_body = self._monta_request('POST', '/orders', self._prepare_monta_order_payload())
            if 200 <= c_status < 300:
                self.write({
                    'monta_order_id': webshop_id,
                    'monta_sync_state': 'sent',
                    'monta_last_push': fields.Datetime.now(),
                    'monta_needs_sync': False,
                })
                self._create_monta_log({'status': c_status, 'body': c_body}, 'info', 'Monta Create',
                                       '[Monta] order created (after update fallback)')
                self._pull_and_apply_edd_now()
                return c_status, c_body

        self.write({'monta_sync_state': 'error', 'monta_needs_sync': True})
        self._create_monta_log({'status': status, 'body': body}, 'error', 'Monta Update',
                               '[Monta] update failed (will retry)')
        try:
            self.message_post(body=f"<b>Monta update failed.</b><br/><pre>{json.dumps(body, indent=2, ensure_ascii=False)}</pre>")
        except Exception:
            pass
        return status, body

    def _monta_delete(self, note="Cancelled from Odoo"):
        """
        Minimal change: delete via Monta Id when possible: DELETE /orders/{Id}.
        Fallback to your previous /order/{webshop_id} endpoint if not found.
        """
        self.ensure_one()
        webshop_id = self.monta_order_id or self.name
        if not webshop_id:
            return 204, {'note': 'No webshoporderid stored; nothing to delete.'}

        # Try resolve Monta Id
        order_id = None
        g_status, g_body = self._monta_request('GET', f"/orders?clientReference={webshop_id}")
        if 200 <= g_status < 300 and isinstance(g_body, list) and g_body:
            order_id = g_body[0].get('Id') or g_body[0].get('id')

        if order_id:
            status, body = self._monta_request('DELETE', f"/orders/{order_id}", {"Note": note},
                                               headers={"Content-Type": "application/json", "Accept": "application/json"})
        else:
            # fallback to legacy path you used before
            headers = {"Content-Type": "application/json-patch+json", "Accept": "application/json"}
            status, body = self._monta_request('DELETE', f"/order/{webshop_id}", {"Note": note}, headers=headers)

        if status in (200, 204):
            self.write({'monta_sync_state': 'cancelled'})
            self._create_monta_log({'status': status, 'body': body}, 'info', 'Monta Delete', '[Monta] order deleted')
        else:
            self._create_monta_log({'status': status, 'body': body}, 'error', 'Monta Delete', '[Monta] delete failed (logged)')
        return status, body

    # ---------------- actions / hooks ----------------
    def action_monta_check_skus(self):
        self.ensure_one()
        try:
            self._log_pack_variant_skus_for_order()
            self._log_all_skus_now()
            _ = self._prepare_monta_lines()
            self.message_post(body="Monta SKU check complete. Packs flattened; all lines built.")
        except ValidationError as e:
            self.message_post(body=f"<pre>{e.name or str(e)}</pre>")
        return True

    def action_confirm(self):
        res = super(SaleOrder, self).action_confirm()
        for order in self:
            try:
                order._log_pack_variant_skus_for_order()
                order._log_all_skus_now()
            except Exception as e:
                _logger.error("[Monta] SKU logging failed for %s: %s", order.name, e, exc_info=True)
            if not order.monta_order_id:
                order._monta_create()
            else:
                order._monta_update()
        return res

    def write(self, vals):
        tracked_fields = {'partner_id', 'order_line', 'client_order_ref', 'validity_date', 'commitment_date'}
        if any(f in vals for f in tracked_fields):
            vals.setdefault('monta_needs_sync', True)
        res = super(SaleOrder, self).write(vals)

        # NEW: If order just got a monta id or was marked sent/updated via some other flow, pull EDD now
        keys = set(vals.keys())
        if {'monta_order_id', 'monta_sync_state'} & keys:
            for order in self:
                try:
                    if vals.get('monta_order_id') or vals.get('monta_sync_state') in ('sent', 'updated'):
                        order._pull_and_apply_edd_now()
                except Exception as e:
                    _logger.error("[Monta EDD] write-trigger failed for %s: %s", order.name, e, exc_info=True)

        # push updates automatically for confirmed orders
        for order in self.filtered(lambda o: o.state in ('sale', 'done') and o.monta_needs_sync and o.state != 'cancel'):
            try:
                order._log_all_skus_now()
                if order._should_push_now():
                    order._monta_update()
            except Exception as e:
                _logger.error("[Monta Sync] Order %s write-sync failed: %s", order.name, e, exc_info=True)
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
