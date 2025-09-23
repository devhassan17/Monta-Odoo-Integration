# -*- coding: utf-8 -*-
from odoo import api, fields, models
from ..services.monta_stock_pull import MontaStockPull
import logging

_logger = logging.getLogger(__name__)


class ProductTemplate(models.Model):
    _inherit = "product.template"

    x_monta_min_stock = fields.Float(
        string="Monta Minimum Stock",
        help="Reserve this quantity for VIPs. When Monta stock is at or below this, "
             "the product is marked Sold Out on the website."
    )
    x_monta_last_stock = fields.Float(
        string="Monta Last Stock",
        help="Latest stock value pulled from Monta WMS.",
        readonly=True,
    )
    x_is_sold_out = fields.Boolean(
        string="Sold Out",
        help="Auto-set when Last Stock <= Minimum Stock; also hides product from webshop.",
        readonly=True,
    )

    def _apply_soldout_policy(self):
        for tmpl in self:
            last_stock = tmpl.x_monta_last_stock or 0.0
            min_stock = tmpl.x_monta_min_stock or 0.0
            sold_out = last_stock <= min_stock
            tmpl.x_is_sold_out = sold_out
            # Hide from webshop for regular users (website_sale adds website_published)
            if "website_published" in tmpl._fields:
                tmpl.website_published = not sold_out
            _logger.info(
                "[Monta Stock Policy] %s | last=%s | min=%s | sold_out=%s",
                tmpl.display_name, last_stock, min_stock, sold_out
            )
        return True

    @api.model
    def cron_monta_stock_sync(self):
        """Optional: call from a manual cron you create in the UI."""
        try:
            puller = MontaStockPull(self.env)
            updated = puller.pull_and_apply()
            _logger.info("[Monta Cron] Synced %s products from Monta stock.", updated)
            return updated
        except Exception as e:
            _logger.error("[Monta Cron] Stock sync failed: %s", e, exc_info=True)
            return 0
