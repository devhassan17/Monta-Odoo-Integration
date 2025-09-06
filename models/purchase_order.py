# -*- coding: utf-8 -*-
import logging
from odoo import models

_logger = logging.getLogger(__name__)

class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    def action_monta_push_inbound_forecast(self):
        svc = self.env['monta.inbound.forecast.service']
        for po in self:
            try:
                _logger.info("[Monta IF] Start push for PO %s", po.name)
                svc.send_for_po(po)
                _logger.info("[Monta IF] Done push for PO %s", po.name)
            except Exception as e:
                _logger.error("[Monta IF] Failed for %s: %s", po.name, e, exc_info=True)
        return True

    def button_confirm(self):
        res = super().button_confirm()
        try:
            self.action_monta_push_inbound_forecast()
        except Exception as e:
            _logger.error("[Monta IF] Auto push after confirm failed: %s", e, exc_info=True)
        return res
