# -*- coding: utf-8 -*-
import logging
from odoo import api, models

_logger = logging.getLogger(__name__)

class ProductProductQtySync(models.Model):
    _inherit = 'product.product'

    @api.model
    def cron_monta_qty_sync(self, limit=None):
        """Entry point for the 6-hour cron job."""
        from ..services.monta_qty_sync import MontaQtySync
        _logger.info("Running Monta Qty Sync (limit=%s)", limit)
        MontaQtySync(self.env).run(limit=limit)
        return True

    @api.model
    def init(self):
        """
        Register a 6-hour cron programmatically if it doesn't exist.
        No XML needed.
        """
        ir_model = self.env['ir.model'].sudo()
        ir_cron = self.env['ir.cron'].sudo()
        model_id = ir_model._get_id('product.product')

        # Use a stable code signature to find-or-create
        name = "Monta: Sync StockAvailable + MinStock (6h)"
        existing = ir_cron.search([('name', '=', name)], limit=1)
        if existing:
            # Ensure it runs every 6 hours and is active
            existing.write({
                'interval_number': 6,
                'interval_type': 'hours',
                'active': True,
                'model_id': model_id,
                'state': 'code',
                'code': "model.cron_monta_qty_sync()",
            })
            return

        # Create brand-new cron
        ir_cron.create({
            'name': name,
            'model_id': model_id,
            'state': 'code',
            'code': "model.cron_monta_qty_sync()",
            'interval_number': 6,
            'interval_type': 'hours',
            'numbercall': -1,
            'active': True,
        })
        _logger.info("Created cron '%s' (every 6 hours)", name)
