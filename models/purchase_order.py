# -*- coding: utf-8 -*-
import logging
from odoo import models, fields, api

_logger = logging.getLogger(__name__)

class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    monta_if_last_push = fields.Datetime(copy=False)
    monta_if_needs_sync = fields.Boolean(default=False, copy=False)

    # throttle helper
    def _if_should_push_now(self, min_gap_seconds=2):
        if not self.monta_if_last_push:
            return True
        delta = fields.Datetime.now() - self.monta_if_last_push
        try:
            return delta.total_seconds() >= min_gap_seconds
        except Exception:
            return True

    def action_monta_push_inbound_forecast(self):
        svc = self.env['monta.inbound.forecast.service']
        for po in self:
            try:
                _logger.info("[Monta IF] Start push for PO %s", po.name)
                ok = svc.send_for_po(po)
                if ok:
                    po.write({'monta_if_needs_sync': False, 'monta_if_last_push': fields.Datetime.now()})
                _logger.info("[Monta IF] Done push for PO %s", po.name)
            except Exception as e:
                _logger.error("[Monta IF] Failed for %s: %s", po.name, e, exc_info=True)
        return True

    # Auto on confirm
    def button_confirm(self):
        res = super().button_confirm()
        try:
            self.action_monta_push_inbound_forecast()
        except Exception as e:
            _logger.error("[Monta IF] Auto push after confirm failed: %s", e, exc_info=True)
        return res

    # Auto on cancel -> delete at Monta
    def button_cancel(self):
        res = super().button_cancel()
        svc = self.env['monta.inbound.forecast.service']
        for po in self:
            try:
                svc.delete_for_po(po, note="Cancelled from Odoo")
            except Exception as e:
                _logger.error("[Monta IF] Delete on cancel failed for %s: %s", po.name, e, exc_info=True)
        return res

    # Auto on unlink -> delete at Monta
    def unlink(self):
        svc = self.env['monta.inbound.forecast.service']
        for po in self:
            try:
                # delete even if not confirmed, just in case you pre-pushed
                svc.delete_for_po(po, note="Deleted from Odoo (unlink)")
            except Exception as e:
                _logger.error("[Monta IF] Delete on unlink failed for %s: %s", po.name, e, exc_info=True)
        return super().unlink()

    # Mark as needs sync on meaningful changes; push if confirmed/done
    def write(self, vals):
        tracked = {'partner_id', 'date_planned', 'order_line', 'picking_type_id', 'origin'}
        if any(k in vals for k in tracked):
            vals.setdefault('monta_if_needs_sync', True)
        res = super().write(vals)

        # For confirmed/done POs, push immediately (throttled)
        to_push = self.filtered(lambda p: p.state in ('purchase', 'done') and p.monta_if_needs_sync)
        for po in to_push:
            try:
                if po._if_should_push_now():
                    po.action_monta_push_inbound_forecast()
            except Exception as e:
                _logger.error("[Monta IF] Auto push after write failed for %s: %s", po.name, e, exc_info=True)
        return res
