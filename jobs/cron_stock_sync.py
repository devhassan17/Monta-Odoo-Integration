# -*- coding: utf-8 -*-
from odoo import models, api
import logging
_logger = logging.getLogger(__name__)

class MontaStockCron(models.AbstractModel):
    _name = "monta.job.stock_sync"
    _description = "Monta Stock Sync Cron"

    @api.model
    def run_daily(self):
        from ..services.monta_stock_pull import MontaStockPull
        updated = MontaStockPull(self.env).pull_and_apply()
        _logger.info("[Monta Stock Cron] %s rows updated", updated)
        return True
