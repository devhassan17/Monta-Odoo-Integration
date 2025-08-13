# -*- coding: utf-8 -*-
import json, re, logging
from odoo import models, fields
from odoo.exceptions import ValidationError

from ..services.monta_client import MontaClient
from ..utils.address import split_street
from ..utils.sku import resolve_sku
from ..utils.pack import get_pack_components_from_bom

_logger = logging.getLogger(__name__)

# Only this instance is allowed to send to Monta
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
        ('blocked_pack', 'Blocked (Pack)'),
    ], default='draft', copy=False)
    monta_last_push = fields.Datetime('Last Push to Monta', copy=False)
    monta_needs_sync = fields.Boolean('Needs Monta Sync', default=False, copy=False)

    # Helpers
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
            self._create_monta_log({'guard': {'web_base_url': web_url, 'allowed': allowed, 'blocked': True}},
                                   level='info', tag='Monta Guard',
                                   console_summary='[Monta Guard] blocked by instance URL')
        return ok

    # Pack detection
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

    def _order_has_pack(self):
        return any(self._is_pack_like(l.product_id) for l in self.order_line if l.product_id)

    # Pack logging (components only)
    def _log_pack_variant_skus_for_order(self):
        for line in self.order_line:
            product = line.product_id
            if not product:
                continue
            qty = line.product_uom_qty or 0.0
            if not self._is_pack_like(product):
                continue

            comps = get_pack_components_from_bom(self.env, self.company_id.id, product, qty)
            _logger.info("[SKU_TEST] ORDER %s | PACK %s (VARIANT %s) | Qty=%s | comps=%s",
                         self.name, product.product_tmpl_id.display_name, product.display_name, qty, len(comps))
            if not comps:
                _logger.info("[SKU_TEST]  -> No components resolved for %s", product.display_name)

            for comp, cqty in comps:
                sku, _ = resolve_sku(comp, env=self.env, allow_synthetic=False)
                _logger.info("[SKU_TEST]    - Component %s | qty=%s | SKU=%s",
                             comp.display_name, cqty, sku or 'EMPTY')
                self.env["sku_test.log"].create({
                    "order_id": self.id,
                    "order_line_id": line.id,
                    "pack_product_id": product.id,
                    "component_product_id": comp.id,
                    "sku": sku or "",
                })

    # Log all SKUs (strict)
    def _log_all_skus_now(self):
        rows = []
        for l in self.order_line:
            product = l.product_id
            if not product:
                continue

            if self._is_pack_like(product):
                comps = get_pack_components_from_bom(self.env, self.company_id.id, product, l.product_uom_qty or 0.0)
                for comp, q in comps:
                    sku, _ = resolve_sku(comp, env=self.env, allow_synthetic=False)
                    _logger.info("[Monta SKU LOG] %s → PACK %s comp %s | qty=%s | sku=%s",
                                 self.name, product.display_name, comp.display_name, q, sku or 'EMPTY')
                    rows.append({'line_id': l.id, 'product_id': comp.id, 'qty': q, 'sku': sku or 'EMPTY'})
            else:
                sku, _ = resolve_sku(product, env=self.env, allow_synthetic=False)
                _logger.info("[Monta SKU LOG] %s → SIMPLE %s | qty=%s | sku=%s",
                             self.name, product.display_name, l.product_uom_qty or 0.0, sku or 'EMPTY')
                rows.append({'line_id': l.id, 'product_id': product.id, 'qty': l.product_uom_qty or 0.0, 'sku': sku or 'EMPTY'})

        self._create_monta_log({'sku_log': rows}, level='info', tag='Monta SKU Log',
                               console_summary=f"[Monta] Logged {len(rows)} SKU row(s)")

    # Build Monta lines (non-pack only, strict SKU)
    def _prepare_monta_lines(self):
        sku_qty = {}
        for l in self.order_line:
            product = l.product_id
            if not product:
                continue
            if self._is_pack_like(product):
                continue  # never send packs

            sku, _ = resolve_sku(product, env=self.env, allow_synthetic=False)
            if not sku:
                raise ValidationError(f"Product '{product.display_name}' has no real SKU.")
            qty = int(l.product_uom_qty or 0)
            if qty > 0:
                sku_qty[sku] = sku_qty.get(sku, 0) + qty

        lines = [{"Sku": sku, "OrderedQuantity": int(q)} for sku, q in sku_qty.items() if int(q) > 0]
        if not lines:
            raise ValidationError("No non-pack products to send to Monta. Pack lines are log-only.")
        self._create_monta_log({'lines': lines}, level='info', tag='Monta Lines',
                               console_summary=f"[Monta] Prepared {len(lines)} line(s) for send")
        return lines

    # Payload / API plumbing
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

    def _create_monta_log(self, payload, level='info', tag='Monta API', console_summary=None):
        self.ensure_one()
        vals = {
            'sale_order_id': self.id,
            'log_data': json.dumps(payload, indent=2, default=str),
            'level': level,
            'name': f'{tag} {self.name} - {level}',
        }
        self.env['monta.sale.log'].sudo().create(vals)
        ( _logger.info if level == 'info' else _logger.error )(f"[{tag}] {console_summary or vals['name']}")

    def _monta_request(self, method, path, payload=None, headers=None):
        if not self._is_allowed_instance():
            return 0, {'note': 'Blocked by instance URL guard'}
        if self._order_has_pack():
            self._create_monta_log({'blocked': 'order contains pack lines (log-only policy)'},
                                   level='info', tag='Monta Block',
                                   console_summary='[Monta] send blocked: pack order')
            self.write({'monta_sync_state': 'blocked_pack', 'monta_needs_sync': False})
            return 0, {'note': 'Pack order blocked from sending; SKUs logged only'}
        client = MontaClient(self.env)
        return client.request(self, method, path, payload=payload, headers=headers)

    # API calls with update->create fallback
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

    # Actions / Hooks
    def action_monta_check_skus(self):
        self.ensure_one()
        try:
            self._log_pack_variant_skus_for_order()
            self._log_all_skus_now()
            if not self._order_has_pack():
                _ = self._prepare_monta_lines()
            self.message_post(body="Monta SKU check complete. SKUs logged. Non-pack lines build OK.")
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

            if order._order_has_pack():
                order.write({'monta_sync_state': 'blocked_pack', 'monta_needs_sync': False})
                order.message_post(body="Pack order detected: component SKUs logged; sending to Monta is blocked by policy.")
                continue

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
                if not order._order_has_pack() and order._should_push_now():
                    order._monta_update()
                elif order._order_has_pack():
                    order.write({'monta_sync_state': 'blocked_pack', 'monta_needs_sync': False})
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
