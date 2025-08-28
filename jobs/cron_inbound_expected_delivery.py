# -*- coding: utf-8 -*-
import logging
from odoo import models, api
_logger = logging.getLogger(__name__)

class InboundEDDJob(models.AbstractModel):
    _name = "monta.job.inbound_edd"
    _description = "Monta Inbound EDD Cron"

    @api.model
    def run_daily(self, limit=200):
        pickings = self.env['stock.picking'].search([
            ('picking_type_id.code', '=', 'incoming'),
            ('x_monta_edd_needs_sync', '=', True),
        ], limit=limit, order='write_date desc')
        _logger.info("[Monta EDD Cron] syncing %s inbound pickings", len(pickings))
        from ..services.monta_inbound_expected_delivery import MontaInboundEDDService
        MontaInboundEDDService(self.env).push_many(pickings)
        return True
