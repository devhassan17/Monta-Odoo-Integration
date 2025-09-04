# -*- coding: utf-8 -*-
import logging
from datetime import datetime, timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class MontaInboundLog(models.Model):
    _name = 'monta.inbound.log'
    _description = 'Monta Inbound API logs (Purchase Orders)'
    _order = 'id desc'

    name = fields.Char('Log Name')
    purchase_order_id = fields.Many2one('purchase.order', string='Purchase Order', ondelete='cascade', index=True)
    level = fields.Selection([('info','Info'), ('error','Error')], default='info', index=True)
    log_data = fields.Text('Log JSON')
    create_date = fields.Datetime('Created On', readonly=True)


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    x_monta_inboundforecast_uid = fields.Char(string='Monta Inbound Forecast UID', copy=False, index=True)

    # ---- helpers ------------------------------------------------------------
    def _monta_po_log(self, payload, level='info', tag='Monta Inbound'):
        """Persist a structured log row and also write to server logs."""
        self.ensure_one()
        try:
            row = {
                'purchase_order_id': self.id,
                'level': level,
                'name': f'{tag} {self.name} - {level}',
                'log_data': self.env['ir.qweb']._json_dump(payload, indent=2),
            }
        except Exception:
            import json
            row = {
                'purchase_order_id': self.id,
                'level': level,
                'name': f'{tag} {self.name} - {level}',
                'log_data': json.dumps(payload, indent=2, default=str),
            }
        self.env['monta.inbound.log'].sudo().create(row)
        (_logger.info if level == 'info' else _logger.error)(f'[{tag}] {payload}')

    def _monta_supplier_code(self):
        partner = self.partner_id
        # Override first (recommended)
        code = (self.env['ir.config_parameter'].sudo().get_param('monta.supplier_code_override') or '').strip()
        if code:
            return code, 'override'

        # Custom partner field
        x = getattr(partner, 'x_monta_supplier_code', '') or ''
        if x.strip():
            return x.strip(), 'partner.x_monta_supplier_code'

        # ICP map (name/ref)
        import json
        raw = (self.env['ir.config_parameter'].sudo().get_param('monta.supplier_code_map') or '{}')
        try:
            mp = { (k or '').strip().upper(): (v or '').strip() for k, v in json.loads(raw).items() }
        except Exception:
            mp = {}
        name_u = (partner.display_name or partner.name or '').strip().upper()
        ref_u  = (partner.ref or '').strip().upper()
        if name_u in mp and mp[name_u]:
            return mp[name_u], 'ICP map (name)'
        if ref_u and ref_u in mp and mp[ref_u]:
            return mp[ref_u], 'ICP map (ref)'

        # Fallbacks
        for attr in ('ref','vat'):
            v = (getattr(partner, attr, '') or '').strip()
            if v:
                return v, f'partner.{attr}'

        d = (self.env['ir.config_parameter'].sudo().get_param('monta.default_supplier_code') or '').strip()
        return d, 'ICP default' if d else ('', 'missing')

    def _monta_warehouse_display_name(self):
        """Exact display name as seen in Monta UI."""
        ICP = self.env['ir.config_parameter'].sudo()
        override = (ICP.get_param('monta.warehouse_display_name_override') or '').strip()
        if override:
            return override, 'override'
        # else default to the Odoo warehouse name (must match Monta UI for your tenant)
        wh_name = self.picking_type_id.warehouse_id.name or ''
        return wh_name, 'odoo.warehouse.name'

    def _monta_planned_dt(self):
        planned = self.date_planned or fields.Datetime.now()
        # ensure future-ish
        try:
            now = fields.Datetime.now()
            if planned < now:
                planned = now + timedelta(days=1, hours=1)
        except Exception:
            pass
        return planned

    # ---- public actions -----------------------------------------------------
    def action_monta_send_inbound_forecast(self):
        """Manual: send/create (or upsert) an inbound forecast in Monta for this PO."""
        for po in self:
            try:
                po._monta_send_inbound_forecast_inner()
            except Exception as e:
                _logger.error("[Monta Inbound] send failed for %s: %s", po.name, e, exc_info=True)
        return True

    # ---- auto trigger on create --------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        recs = super().create(vals_list)
        ICP = self.env['ir.config_parameter'].sudo()
        auto = (ICP.get_param('monta.auto_inbound_forecast') or '1').strip() not in ('0', 'false', 'False')
        if auto:
            for po in recs:
                try:
                    po._monta_send_inbound_forecast_inner()
                except Exception as e:
                    _logger.error("[Monta Inbound] auto-create failed for %s: %s", po.name, e, exc_info=True)
                    # log but don't block PO creation
                    po._monta_po_log({'auto': True, 'error': str(e)}, level='error', tag='Monta Inbound')
        return recs

    # ---- core impl ----------------------------------------------------------
    def _monta_send_inbound_forecast_inner(self):
        self.ensure_one()
        svc = self.env['monta.inbound.forecast.service']

        supplier_code, sc_src = self._monta_supplier_code()
        wh_name, wh_src = self._monta_warehouse_display_name()
        planned_dt = self._monta_planned_dt()

        # Validate minimal inputs
        if not supplier_code:
            raise ValueError("SupplierCode missing (set partner.x_monta_supplier_code or ICP mapping or override).")
        if not wh_name:
            raise ValueError("WarehouseDisplayName missing (set monta.warehouse_display_name_override or check warehouse name).")
        if not self.order_line:
            raise ValueError("No lines on PO; nothing to forecast.")

        # Build + send
        payload, url = svc.build_payload(self, supplier_code, wh_name, planned_dt)
        self._monta_po_log({'about': 'build_payload', 'supplier': [supplier_code, sc_src],
                            'warehouse_display_name': [wh_name, wh_src],
                            'planned': fields.Datetime.to_string(planned_dt),
                            'payload_excerpt': payload}, level='info', tag='Monta Inbound')

        status, body = svc.post_group_with_lines(self, payload, url=url)
        ok = status in (200, 201)
        self._monta_po_log({'about': 'post_group_with_lines', 'status': status, 'response_excerpt': body},
                           level=('info' if ok else 'error'),
                           tag='Monta Inbound')

        if ok and isinstance(body, dict):
            uid = (body.get('UniqueId') or '') if body else ''
            if uid:
                try:
                    self.write({'x_monta_inboundforecast_uid': uid})
                except Exception:
                    pass
            # chatter note (best-effort)
            try:
                self.message_post(body=f"<b>Monta Inbound Forecast</b> created.<br/>UID: <code>{uid or '-'}</code>")
            except Exception:
                pass

        if not ok:
            raise ValueError(f"Monta returned HTTP {status}. See logs for details.")
