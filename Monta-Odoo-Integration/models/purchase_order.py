# -*- coding: utf-8 -*-
import logging
from odoo import models

_logger = logging.getLogger(__name__)

class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    # Manual trigger (from button or shell)
    def action_monta_push_inbound_forecast(self):
        svc = self.env['monta.inbound.forecast.service']
        for po in self:
            try:
                _logger.info("[Monta IF] Start push for PO %s", po.name)
                ok = svc.send_for_po(po)  # now no instance guard; still respects monta.inbound_enable
                if ok:
                    _logger.info("[Monta IF] Done push for PO %s", po.name)
                else:
                    _logger.info("[Monta IF] Skipped for PO %s (feature disabled or state not eligible)", po.name)
            except Exception as e:
                _logger.error("[Monta IF] Failed for %s: %s", po.name, e, exc_info=True)
        return True

    # Auto-push on confirm
    def button_confirm(self):
        res = super().button_confirm()
        try:
            self.action_monta_push_inbound_forecast()
        except Exception as e:
            _logger.error("[Monta IF] Auto push after confirm failed: %s", e, exc_info=True)
        return res

    # Auto-update on write (only when already confirmed)
    def write(self, vals):
        res = super().write(vals)
        try:
            to_push = self.filtered(lambda p: p.state in ('purchase', 'done'))
            if to_push:
                svc = self.env['monta.inbound.forecast.service']
                for po in to_push:
                    try:
                        svc.send_for_po(po)
                    except Exception as e:
                        _logger.error("[Monta IF] Write-trigger update failed for %s: %s", po.name, e, exc_info=True)
        except Exception as e:
            _logger.error("[Monta IF] post-write hook error: %s", e, exc_info=True)
        return res

    # Delete/cancel hooks
    def button_cancel(self):
        res = super().button_cancel()
        try:
            svc = self.env['monta.inbound.forecast.service']
            for po in self:
                svc.delete_for_po(po, note="Cancelled from Odoo")
        except Exception as e:
            _logger.error("[Monta IF] Cancel delete failed: %s", e, exc_info=True)
        return res

    def unlink(self):
        try:
            svc = self.env['monta.inbound.forecast.service']
            for po in self:
                svc.delete_for_po(po, note="Deleted from Odoo (unlink)")
        except Exception as e:
            _logger.error("[Monta IF] Unlink delete failed: %s", e, exc_info=True)
        return super().unlink()
