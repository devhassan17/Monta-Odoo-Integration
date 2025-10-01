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

# Hard guard removed; control via monta.allowed_base_urls
ALLOWED_INSTANCE_URL = ""


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
        """
        Allowed when:
        - 'monta.allowed_base_urls' is empty (no blocking), or
        - current web.base.url matches one of the comma-separated URLs.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        web_url = (ICP.get_param('web.base.url') or '').strip().rstrip('/') + '/'
        allowed_conf = (ICP.get_param('monta.allowed_base_urls') or '').strip()
        if not allowed_conf:
            return True
        allowed_list = [u.strip().rstrip('/') + '/' for u in allowed_conf.split(',') if u.strip()]
        ok = (web_url.lower() in [a.lower() for a in allowed_list])
        if not ok:
            _logger.warning("[Monta Guard] Not sending order %s. web.base.url=%s allowed_list=%s",
                            self.name, web_url, allowed_list)
            self._create_monta_log(
                {'guard': {'web_base_url': web_url, 'allowed_list': allowed_list, 'blocked': True}},
                level='info', tag='Monta Guard', console_summary='[Monta Guard] blocked by instance URL'
            )
        return ok

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
        ICP = self.env['ir.config_parameter'].sudo()
        partner = self.partner_id
        street, house_number, house_suffix = self._split_street(partner.street or '', partner.street2 or '')
        lines = self._prepare_monta_lines()
        invoice_id_digits = re.sub(r'\D', '', self.name or '')
        webshop_factuur_id = int(invoice_id_digits) if invoice_id_digits else 9999

        # Origin: configurable via System Parameter 'monta.origin'
        origin_cfg = (ICP.get_param('monta.origin') or '').strip()

        payload = {
            "WebshopOrderId": self.name,
            "Reference": self.client_order_ref or "",
            # "Origin": "Moyee_Odoo",  # removed hard-code to avoid Code 2 errors
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

        # Only include Origin if you configured it (prevents "invalid or not provided")
        if origin_cfg:
            payload["Origin"] = origin_cfg
        else:
            _logger.info("[Monta Payload] No 'monta.origin' configured; omitting Origin field.")

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
        self.ensure_one()
        _logger.info("[Monta] Creating order %s", self.name)
        status, body = self._monta_request('POST', '/order', self._prepare_monta_order_payload())
        if 200 <= status < 300:
            self.write({
                'monta_order_id': self.name,
                'monta_sync_state': 'sent',
                'monta_last_push': fields.Datetime.now(),
                'monta_needs_sync': False,
            })
            self._create_monta_log({'status': status, 'body': body}, 'info', 'Monta Create', '[Monta] order created')
            # (Disabled) Immediate EDD pull to commitment_date is now disabled
        else:
            self.write({'monta_sync_state': 'error', 'monta_needs_sync': True})
            self._create_monta_log({'status': status, 'body': body}, 'error', 'Monta Create', '[Monta] create failed')
            try:
                self.message_post(body=f"<b>Monta create failed.</b><br/><pre>{json.dumps(body, indent=2, ensure_ascii=False)}</pre>")
            except Exception:
                pass
        return status, body

    def _monta_update(self):
        """Disabled. We only allow create (POST) and cancel (DELETE) currently."""
        self.ensure_one()
        msg = {"note": "Update disabled by configuration; skipping PUT."}
        try:
            self._create_monta_log(msg, level='info', tag='Monta Update', console_summary='[Monta] update disabled')
            self.message_post(body="<b>Monta update skipped</b><br/><pre>Update disabled; only create/cancel allowed.</pre>")
        except Exception:
            pass
        return 0, msg

    def _monta_delete(self, note="Cancelled from Odoo"):
        self.ensure_one()
        webshop_id = self.monta_order_id or self.name
        if not webshop_id:
            return 204, {'note': 'No webshoporderid stored; nothing to delete.'}
        headers = {"Content-Type": "application/json-patch+json", "Accept": "application/json"}
        path = f"/order/{webshop_id}"
        status, body = self._monta_request('DELETE', path, {"Note": note}, headers=headers)
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
            # Always create (POST). Update is disabled.
            order._monta_create()
        return res

    def write(self, vals):
        tracked_fields = {'partner_id', 'order_line', 'client_order_ref', 'validity_date', 'commitment_date'}
        if any(f in vals for f in tracked_fields):
            vals.setdefault('monta_needs_sync', True)
        res = super(SaleOrder, self).write(vals)

        # (Disabled) Old behavior triggered EDD pull here; no longer needed

        # push updates automatically for confirmed orders — now uses create (POST)
        for order in self.filtered(lambda o: o.state in ('sale', 'done') and o.monta_needs_sync and o.state != 'cancel'):
            try:
                order._log_all_skus_now()
                if order._should_push_now():
                    order._monta_create()
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
            if order.state in ('sale', 'done') and (order.monta_order_id or self.name):
                order._monta_delete(note="Deleted from Odoo (unlink)")
        return super(SaleOrder, self).unlink()
