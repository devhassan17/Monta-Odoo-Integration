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


def post_init_hook(cr, registry):
    """
    Create the cron if it doesn't exist (idempotent).
    You may already have the cron from earlier – that's fine.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    cron = env.ref(CRON_XMLID, raise_if_not_found=False)
    if cron:
        _logger.info("[Monta Qty Sync] Cron already exists (id=%s)", cron.id)
        return

    cron = env["ir.cron"].create(
        {
            "name": "Monta: Sync StockAvailable + MinStock (6h)",
            "model_id": env.ref("product.model_product_product").id,
            "state": "code",
            "code": "model.cron_monta_qty_sync()",
            "interval_number": 6,
            "interval_type": "hours",
            "active": True,
        }
    )
    _logger.info("[Monta Qty Sync] Created cron (id=%s)", cron.id)
