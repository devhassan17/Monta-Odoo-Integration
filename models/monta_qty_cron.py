# -*- coding: utf-8 -*-
# Monta-Odoo-Integration/models/monta_qty_cron.py

import logging

from odoo import SUPERUSER_ID, api, models

from ..services.monta_qty_sync import MontaQtySync

_logger = logging.getLogger(__name__)

CRON_XMLID = "Monta-Odoo-Integration.ir_cron_monta_qty_sync"


class ProductProduct(models.Model):
    _inherit = "product.product"

    @api.model
    def cron_monta_qty_sync(self, limit=None):
        """
        Entry point for the 6-hour cron job or manual run.

        NOTE: Do NOT call self.env.sudo() here (safe_eval context can complain).
        The service already sudo()s only on the models that need it (ICP, etc.).
        """
        _logger.info("[Monta Qty Sync] Starting (limit=%s)", limit)
        MontaQtySync(self.env).run(limit=limit)


    @api.model
    def cron_monta_stock_pull(self, limit=None):
        """
        Entry point for the 24-hour bulk stock pull cron.
        """
        from ..services.monta_stock_pull import MontaStockPull
        _logger.info("[Monta Stock Pull] Starting (limit=%s)", limit)
        total = MontaStockPull(self.env).pull_and_apply(limit=limit)
        _logger.info("[Monta Stock Pull] Finished. Updated %s templates.", total)

