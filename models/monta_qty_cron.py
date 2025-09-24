# -*- coding: utf-8 -*-
import logging

from odoo import api, models, SUPERUSER_ID

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
    def _cron_vals(self, model_id):
        """
        Build ir.cron values compatible with Odoo 18 (no numbercall).
        Use explicit env[...] call to avoid 'model' binding quirks in server actions.
        """
        vals = {
            'name': "Monta: Sync StockAvailable + MinStock (6h)",
            'model_id': model_id,
            'state': 'code',
            'code': "env['product.product'].sudo().cron_monta_qty_sync()",
            'interval_number': 6,
            'interval_type': 'hours',
            'active': True,
            'user_id': SUPERUSER_ID,
        }
        # If these fields exist in this DB, set them
        if 'max_number_of_calls' in self.env['ir.cron']._fields:
            vals['max_number_of_calls'] = 0  # unlimited
        if 'repeat_missed' in self.env['ir.cron']._fields:
            vals['repeat_missed'] = True
        return vals

    @api.model
    def init(self):
        """
        Create/ensure the 6-hour cron job programmatically (no XML).
        """
        ir_model = self.env['ir.model'].sudo()
        ir_cron = self.env['ir.cron'].sudo()

        model_id = ir_model._get_id('product.product')
        name = "Monta: Sync StockAvailable + MinStock (6h)"
        cron = ir_cron.search([('name', '=', name)], limit=1)

        vals = self._cron_vals(model_id)
        if cron:
            cron.write(vals)
            _logger.info("Updated cron '%s' to run every 6 hours.", name)
        else:
            ir_cron.create(vals)
            _logger.info("Created cron '%s' (every 6 hours).", name)
