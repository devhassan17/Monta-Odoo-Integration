# -*- coding: utf-8 -*-
import logging
from odoo import models

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    # NOTE:
    #   No new fields are added.
    #   We use Odoo's native `scheduled_date` on stock.picking as the Expected Delivery.

    def write(self, vals):
        """
        If `scheduled_date` changed on inbound receipts, push the new value to Monta.
        """
        res = super().write(vals)
        if 'scheduled_date' in vals:
            incoming = self.filtered(lambda p: p.picking_type_id.code == 'incoming')
            if incoming:
                try:
                    from ..services.monta_inbound_expected_delivery import MontaInboundEDDService
                    MontaInboundEDDService(self.env).push_many(incoming)
                except Exception as e:
                    _logger.error("[Monta EDD] Auto-push after scheduled_date change failed: %s",
                                  e, exc_info=True)
        return res

    def action_push_monta_edd_now(self):
        """
        Manual push for the current records (use Odoo Studio / dev mode to add a button if desired).
        """
        incoming = self.filtered(lambda p: p.picking_type_id.code == 'incoming')
        if not incoming:
            return True
        from ..services.monta_inbound_expected_delivery import MontaInboundEDDService
        MontaInboundEDDService(self.env).push_many(incoming)
        return True
