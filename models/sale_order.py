# -*- coding: utf-8 -*-
import json, re, logging
from odoo import models, fields
from odoo.exceptions import ValidationError

from ..services.monta_client import MontaClient
from ..utils.address import split_street
from ..utils.sku import resolve_sku
from ..utils.pack import get_pack_components_from_bom

_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    # -------------------------
    # PACK DEBUG LOGGING
    # -------------------------
    def _log_pack_variant_skus_for_order(self):
        """
        For each order line that looks like a pack, log:
        Pack (template) -> Variant -> Component products (with real SKUs)
        Output goes to Odoo logs via _logger.info().
        """
        def _expand_pack_components(product, qty, company_id):
            # 1) Prefer phantom BoM for the *variant*
            comps = self._get_pack_components_from_bom(product, qty)
            source = 'mrp_phantom' if comps else None

            # 2) Fallback to OCA product_pack, if any
            if not comps:
                comps = self._get_pack_components_from_oca_pack(product, qty)
                source = 'oca_pack' if comps else None
            return comps, source

        for line in self.order_line:
            product = line.product_id
            if not product:
                continue

            qty = line.product_uom_qty or 0.0
            if not self._is_pack_like(product):
                continue

            comps, source = _expand_pack_components(product, qty, self.company_id.id)

            _logger.info(
                "[Monta Pack Debug] ORDER %s | PACK %s | VARIANT %s | Source=%s | Qty=%s",
                self.name,
                product.product_tmpl_id.display_name,
                product.display_name,
                (source or "none"),
                qty,
            )

            if not comps:
                _logger.info(
                    "[Monta Pack Debug]  -> No components resolved. "
                    "Add a PHANTOM BoM for this VARIANT or OCA pack lines."
                )
                continue

            _logger.info("[Monta Pack Debug]  Components (product → qty → SKU → source):")
            for comp_prod, comp_qty in comps:
                sku, sku_src = resolve_sku(comp_prod, env=self.env, )
                _logger.info(
                    "[Monta Pack Debug]    - %s  | qty=%s  | sku=%s  | src=%s",
                    comp_prod.display_name,
                    comp_qty,
                    (sku or "EMPTY"),
                    sku_src,
                )

    # -------------------------
    # CONFIG / STATE
    # -------------------------
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
        return split_street(street, street2)

    def _should_push_now(self, min_gap_seconds=2):
        if not self.monta_last_push:
            return True
        delta = fields.Datetime.now() - self.monta_last_push
        try:
            return delta.total_seconds() >= min_gap_seconds
        except Exception:
            return True

    # -------------------------
    # PACK / BUNDLE EXPANSION
    # -------------------------

    def _get_pack_components_from_bom(self, product, qty):
        """Prefer phantom BoM expansion with robust fallback to direct bom lines."""
        return get_pack_components_from_bom(self.env, self.company_id.id, product, qty)

    def _get_pack_components_from_oca_pack(self, product, qty):
        """
        Expand components via OCA product_pack, handling common schemas:

        - product.template.pack_line_ids -> product_id / (qty|quantity|product_qty)
        - product.product.pack_line_ids  -> product_id / (qty|quantity|product_qty)
        - product.pack.line model behind either relation

        Returns list[(product.product, qty)].
        """
        comps = []

        def _extract_lines(owner):
            for field_name in ('pack_line_ids', 'pack_lines', 'pack_line_ids_variant'):
                lines = getattr(owner, field_name, False)
                if lines:
                    return lines
            return False

        lines = _extract_lines(product.product_tmpl_id) or _extract_lines(product)
        if not lines:
            return comps

        for line in lines:
            cprod = getattr(line, 'product_id', False) or getattr(line, 'item_id', False)
            q = (
                getattr(line, 'qty', False) or
                getattr(line, 'quantity', False) or
                getattr(line, 'product_qty', False) or
                getattr(line, 'uom_qty', False) or
                0.0
            )
            if cprod and q:
                comps.append((cprod, (q or 0.0) * (qty or 1.0)))
        return comps

    def _is_pack_like(self, product):
        """Heuristic: product looks like a pack if it has phantom BoM or OCA pack lines."""
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

    def _expand_line_into_components(self, line):
        """
        Expand a sale.order.line into component (product, qty) pairs.
        STRICT component mode for packs:
          - If product is pack-like but no components resolved, raise a clear ValidationError
            (we never fall back to the pack SKU).
        """
        product = line.product_id
        qty = line.product_uom_qty or 0.0
        if qty <= 0:
            return [], None

        comps = self._get_pack_components_from_bom(product, qty)
        source = 'mrp_phantom'

        if not comps:
            comps = self._get_pack_components_from_oca_pack(product, qty)
            source = 'oca_pack' if comps else None

        if comps:
            comp_list = []
            for p, q in comps:
                sku, _src = resolve_sku(p, env=self.env)
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

        if self._is_pack_like(product):
            raise ValidationError(
                f"Pack '{product.display_name}' has no resolvable components.\n"
                f"Please add a PHANTOM BoM or OCA pack lines for this VARIANT (e.g. Espresso Grind), "
                f"so Monta receives component SKUs."
            )

        return [(product, qty)], None

    # -------------------------
    # Lines → Monta format
    # -------------------------
    def _prepare_monta_lines(self):
        sku_qty, missing, pack_logs = {}, [], []

        for l in self.order_line:
            comps, pack_info = self._expand_line_into_components(l)
            if pack_info:
                pack_logs.append(pack_info)

            for prod, q in comps:
                if q <= 0:
                    continue
                sku, _source = resolve_sku(prod, env=self.env)
                if not sku:
                    missing.append({
                        'line_id': l.id,
                        'pack_product': l.product_id.display_name,
                        'component_id': prod.id,
                        'component_name': prod.display_name or prod.name or f'ID {prod.id}',
                        'resolved_sku': 'EMPTY',
                        'qty': q,
                    })
                    continue
                sku_qty[sku] = sku_qty.get(sku, 0) + q

        if pack_logs:
            self._create_monta_log(
                {'pack_expansion': pack_logs}, level='info',
                tag='Monta Pack Expansion',
                console_summary=f"[Monta Pack Expansion] {len(pack_logs)} pack line(s) expanded"
            )

        if missing:
            self._create_monta_log(
                {'missing_skus': missing}, level='error',
                tag='Monta SKU check',
                console_summary=f"[Monta SKU check] {len(missing)} missing SKU(s)"
            )
            msg_lines = ["Cannot push to Monta: some products have no mapped SKU."]
            for m in missing:
                msg_lines.append(f"- {m['component_name']} (from pack: {m['pack_product']}) → SKU: {m['resolved_sku']}")
            raise ValidationError("\n".join(msg_lines))

        lines = [{"Sku": sku, "OrderedQuantity": int(qty)}
                 for sku, qty in sku_qty.items() if int(qty) > 0]
        if not lines:
            raise ValidationError("Cannot push to Monta: order lines expanded to empty/zero quantities.")
        return lines

    # -------------------------
    # Payload / API plumbing (unchanged)
    # -------------------------
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

    # -------------------------
    # API calls (unchanged)
    # -------------------------
    def _monta_request(self, method, path, payload=None, headers=None):
        client = MontaClient(self.env)
        return client.request(self, method, path, payload=payload, headers=headers)

    def _monta_create(self):
        self.ensure_one()
        status, body = self._monta_request('POST', '/order', self._prepare_monta_order_payload())
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
        self.ensure_one()
        webshop_id = self.monta_order_id or self.name
        path = f"/order/{webshop_id}"
        status, body = self._monta_request('PUT', path, self._prepare_monta_order_payload())
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
        self.ensure_one()
        webshop_id = self.monta_order_id or self.name
        if not webshop_id:
            return 204, {'note': 'No webshoporderid stored; nothing to delete.'}
        headers = {"Content-Type": "application/json-patch+json", "Accept": "application/json"}
        path = f"/order/{webshop_id}"
        status, body = self._monta_request('DELETE', path, {"Note": note}, headers=headers)
        if status in (200, 204):
            self.write({'monta_sync_state': 'cancelled'})
        else:
            self._create_monta_log({'delete_failed': body}, 'error', console_summary="[Monta API] delete failed (logged)")
        return status, body

    # -------------------------
    # Actions / Hooks  (MERGED)
    # -------------------------
    def action_monta_check_skus(self):
        self.ensure_one()
        try:
            _ = self._prepare_monta_lines()
            self.message_post(body="Monta SKU check passed. All lines/components have SKUs.")
        except ValidationError as e:
            self.message_post(body=f"<pre>{e.name or str(e)}</pre>")
        return True

    def action_confirm(self):
        """
        Single consolidated hook:
        - Confirm order
        - Log pack→component SKUs
        - Push to Monta (create/update)
        """
        res = super(SaleOrder, self).action_confirm()
        for order in self:
            # Debug log of pack/variant SKUs (safe if no packs)
            try:
                order._log_pack_variant_skus_for_order()
            except Exception as e:
                _logger.error("[Monta Pack Debug] Failed to log pack SKUs for %s: %s", order.name, e)

            # Initial create/update
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
