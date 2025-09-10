# -*- coding: utf-8 -*-
import logging
from odoo import models

_logger = logging.getLogger(__name__)

class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    def _monta_if_enabled(self):
        try:
            ICP = self.env['ir.config_parameter'].sudo()
            val = (ICP.get_param('monta.inbound_enable') or '').strip().lower()
            return val in ('1', 'true', 'yes', 'on')
        except Exception:
            return False

    def action_monta_push_inbound_forecast(self):
        """Manual button/method. Now respects feature flag."""
        svc = self.env['monta.inbound.forecast.service']
        for po in self:
            try:
                if not self._monta_if_enabled():
                    _logger.info("[Monta IF] Disabled — manual push skipped for %s", po.name)
                    continue
                _logger.info("[Monta IF] Start push for PO %s", po.name)
                svc.send_for_po(po)
                _logger.info("[Monta IF] Done push for PO %s", po.name)
            except Exception as e:
                _logger.error("[Monta IF] Failed for %s: %s", po.name, e, exc_info=True)
        return True

    def button_confirm(self):
        """Auto push on confirm — now no-ops if feature flag is off."""
        res = super().button_confirm()
        try:
            if self._monta_if_enabled():
                self.action_monta_push_inbound_forecast()
            else:
                _logger.info("[Monta IF] Disabled — auto push after confirm skipped for %s", ",".join(self.mapped('name')))
        except Exception as e:
            _logger.error("[Monta IF] Auto push after confirm failed: %s", e, exc_info=True)
        return res
