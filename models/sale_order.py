# -*- coding: utf-8 -*-
import json, re, logging
from collections import defaultdict
from odoo import models, fields
from odoo.exceptions import ValidationError

from ..services.monta_client import MontaClient
from ..utils.address import split_street
from ..utils.sku import resolve_sku
from ..utils.pack import get_pack_components_from_bom

_logger = logging.getLogger(__name__)

ALLOWED_INSTANCE_URL = "https://moyeecoffee-03-july-2025-22548764.dev.odoo.com/"

class SaleOrder(models.Model):
    _inherit = 'sale.order'

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

    # ---------- helpers ----------
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
            _logger.warning("[Monta Guard] Not sending order %s. web.base.url=%s expected=%s", self.name, web_url, allowed)
            self._create_monta_log({'guard': {'web_base_url': web_url, 'allowed': allowed, 'blocked': True}},
                                   level='info', tag='Monta Guard',
                                   console_summary='[Monta Guard] blocked by instance URL')
        return ok

    # ---------- pack heuristics ----------
    def _is_pack_like(self, product):
        if getattr(product.product_tmpl_id, 'pack_line_ids', False) or getattr(product, 'pack_line_ids', False):
            return True
        Bom = self.env['mrp.bom']
        bom = Bom.search([
            ('product_tmpl_id', '=', product.product_tmpl_id.id),
            ('type', '=', 'phantom'),
            '|', ('product_id', '=', product.id), ('product_id', '=', False),
            '|', ('company_id', '=', self.company_id.id), ('company_id', '=', False),
        ], limit=1)
        return bool(bom)

    # ---------- logging ----------
    def _log_pack_variant_skus_for_order(self):
        """
        Always log pack expansion (components + real SKUs).
        """
        packs_scanned = 0
        for line in self.order_line:
            p = line.product_id
            if not p or not self._is_pack_like(p):
                continue
            qty = line.product_uom_qty or 0.0
            comps = get_pack_components_from_bom(self.env, self.company_id.id, p, qty)
            _logger.info("[Monta Pack Debug] ORDER %s | PACK %s | VARIANT %s | Qty=%s | comps=%s",
                         self.name, p.product_tmpl_id.display_name, p.display_name, qty, len(comps))
            for comp, cqty in comps:
                sku, src = resolve_sku(comp, env=self.env, allow_synthetic=False)
                _logger.info("[Monta Pack Debug]    - %s | qty=%s | sku=%s | src=%s",
                             comp.display_name, cqty, sku or 'EMPTY', src)
                self.env['sku_test.log'].create({
                    'order_id': self.id,
                    'order_line_id': line.id,
                    'pack_product_id': p.id,
                    'component_product_id': comp.id,
                    'sku': sku or '',
                })
            packs_scanned += 1
        _logger.info("[Monta Pack Debug] %s pack line(s) scanned", packs_scanned)

    def _log_all_skus_now(self):
        """
        Log every SKU we will send (incl. packs exploded to components).
        """
        rows = []
        for l in self.order_line:
            p = l.product_id
            if not p:
                continue
            if self._is_pack_like(p):
                comps = get_pack_components_from_bom(self.env, self.company_id.id, p, l.product_uom_qty or 0.0)
                for comp, q in comps:
                    sku, _ = resolve_sku(comp, env=self.env, allow_synthetic=False)
                    _logger.info("[Monta Order SKUs] Order %s → PACK %s comp %s | Qty %s | SKU %s",
                                 self.name, p.display_name, comp.display_name, q, sku or 'EMPTY')
                    rows.append({'line_id': l.id, 'product_id': comp.id, 'qty': q, 'sku': sku or 'EMPTY'})
            else:
                sku, _ = resolve_sku(p, env=self.env, allow_synthetic=False)
                _logger.info("[Monta Order SKUs] Order %s → SKU %s Qty %s (product %s)",
                             self.name, sku or 'EMPTY', l.product_uom_qty or 0.0, p.display_name)
                rows.append({'line_id': l.id, 'product_id': p.id, 'qty': l.product_uom_qty or 0.0, 'sku': sku or 'EMPTY'})
        self._create_monta_log({'sku_log': rows}, level='info', tag='Monta SKU Log',
                               console_summary=f"[Monta] Logged {len(rows)} SKU row(s)")

    # ---------- build Lines for Monta (expand packs to array of component SKUs) ----------
    def _prepare_monta_lines(self):
        """
        Returns a list of dicts: [{"Sku": <real sku>, "OrderedQuantity": <int>}]
        - Pack lines are expanded via phantom BoM into their components
        - All SKUs must be REAL (no synthetic); else we raise a single ValidationError listing them
        - Quantities are aggregated per SKU across all lines/components
        """
        sku_qty = defaultdict(float)
        missing = []

        for l in self.order_line:
            p = l.product_id
            if not p:
                continue
            line_qty = l.product_uom_qty or 0.0
            if line_qty <= 0:
                continue

            if self._is_pack_like(p):
                comps = get_pack_components_from_bom(self.env, self.company_id.id, p, line_qty)
                if not comps:
                    missing.append(f"Pack '{p.display_name}' has no resolvable components (phantom BoM missing).")
                    continue
                for comp, cqty in comps:
                    sku, _ = resolve_sku(comp, env=self.env, allow_synthetic=False)
                    if not sku:
                        missing.append(f"Component '{comp.display_name}' (from pack '{p.display_name}') is missing a real SKU.")
                        continue
                    sku_qty[sku] += float(cqty or 0.0)
            else:
                sku, _ = resolve_sku(p, env=self.env, allow_synthetic=False)
                if not sku:
                    missing.append(f"Product '{p.display_name}' is missing a real SKU.")
                else:
                    sku_qty[sku] += float(line_qty)

        if missing:
            # Log & raise a readable error
            self._create_monta_log({'missing_skus': missing}, level='error', tag='Monta SKU check',
                                   console_summary=f"[Monta SKU check] {len(missing)} missing SKU(s)")
            raise ValidationError("Cannot push to Monta:\n- " + "\n- ".join(missing))

        # Build lines (floats aggregated → send as int Monta wants whole quantities)
        lines = [{"Sku": sku, "OrderedQuantity": int(q)} for sku, q in sku_qty.items() if int(q) > 0]

        if not lines:
            raise ValidationError("Order lines expanded to empty/zero quantities.")

        self._create_monta_log({'lines': lines}, level='info', tag='Monta Lines',
                               console_summary=f"[Monta] Prepared {len(lines)} line(s)")
        return lines

    # ---------- payload / logging ----------
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
        # Extra visibility in server log:
        _logger.info("[Monta Payload] %s -> Lines: %s", self.name, json.dumps(lines))
        return payload

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

    def _monta_request(self, method, path, payload=None, headers=None):
        if not self._is_allowed_instance():
            return 0, {'note': 'Blocked by instance URL guard'}
        client = MontaClient(self.env)
        return client.request(self, method, path, payload=payload, headers=headers)

    # ---------- API calls (update→create fallback) ----------
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
            self._create_monta_log({'status': status, 'body': body}, 'info', 'Monta Create', console_summary='[Monta] order created')
        else:
            self.write({'monta_sync_state': 'error', 'monta_needs_sync': True})
            self._create_monta_log({'status': status, 'body': body}, 'error', 'Monta Create', console_summary='[Monta] create failed')
            # Post Monta error to chatter for quick debugging
            try:
                self.message_post(body=f"<b>Monta create failed.</b><br/><pre>{json.dumps(body, indent=2, ensure_ascii=False)}</pre>")
            except Exception:
                pass
        return status, body

    def _monta_update(self):
        self.ensure_one()
        webshop_id = self.monta_order_id or self.name
        _logger.info("[Monta] Updating order %s (idempotent by name)", webshop_id)
        path = f"/order/{webshop_id}"
        status, body = self._monta_request('PUT', path, self._prepare_monta_order_payload())
        if 200 <= status < 300:
            self.write({
                'monta_order_id': webshop_id,
                'monta_sync_state': 'updated' if self.monta_sync_state != 'sent' else 'sent',
                'monta_last_push': fields.Datetime.now(),
                'monta_needs_sync': False,
            })
            self._create_monta_log({'status': status, 'body': body}, 'info', 'Monta Update', console_summary='[Monta] order updated')
            return status, body

        if status in (400, 404):
            _logger.warning("[Monta] Update failed (%s) for %s; attempting create...", status, webshop_id)
            c_status, c_body = self._monta_request('POST', '/order', self._prepare_monta_order_payload())
            if 200 <= c_status < 300:
                self.write({
                    'monta_order_id': webshop_id,
                    'monta_sync_state': 'sent',
                    'monta_last_push': fields.Datetime.now(),
                    'monta_needs_sync': False,
                })
                self._create_monta_log({'status': c_status, 'body': c_body}, 'info', 'Monta Create',
                                       console_summary='[Monta] order created (after update fallback)')
                return c_status, c_body

        self.write({'monta_sync_state': 'error', 'monta_needs_sync': True})
        self._create_monta_log({'status': status, 'body': body}, 'error', 'Monta Update',
                               console_summary='[Monta] update failed (will retry)')
        try:
            self.message_post(body=f"<b>Monta update failed.</b><br/><pre>{json.dumps(body, indent=2, ensure_ascii=False)}</pre>")
        except Exception:
            pass
        return status, body

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
            self._create_monta_log({'status': status, 'body': body}, 'info', 'Monta Delete', console_summary='[Monta] order deleted')
        else:
            self._create_monta_log({'status': status, 'body': body}, 'error', 'Monta Delete', console_summary='[Monta] delete failed (logged)')
        return status, body

    # ---------- actions / hooks ----------
    def action_monta_check_skus(self):
        self.ensure_one()
        try:
            self._log_pack_variant_skus_for_order()
            self._log_all_skus_now()
            _ = self._prepare_monta_lines()
            self.message_post(body="Monta SKU check complete. All SKUs logged and lines built (packs expanded).")
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
