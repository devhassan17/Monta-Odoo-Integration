# -*- coding: utf-8 -*-
import logging

from odoo import models

_logger = logging.getLogger(__name__)


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    def _touch_parent_for_monta(self, orders=None):
        orders = orders if orders is not None else self.mapped("order_id")
        orders = orders.filtered(lambda o: o.state in ("sale", "done"))
        if not orders:
            return

        # Mark orders for sync; actual push handled elsewhere (cron/manual/order hooks)
        orders.write({"monta_needs_sync": True})

    def create(self, vals_list):
        recs = super().create(vals_list)
        try:
            recs._touch_parent_for_monta()
        except Exception as e:
            _logger.error("[Monta Sync] touch after create failed: %s", e, exc_info=True)
        return recs

    def write(self, vals):
        res = super().write(vals)
        try:
            watched = {"product_id", "product_uom_qty", "name", "price_unit", "tax_id"}
            if watched.intersection(vals):
                self._touch_parent_for_monta()
        except Exception as e:
            _logger.error("[Monta Sync] touch after write failed: %s", e, exc_info=True)
        return res

    def unlink(self):
        orders = self.mapped("order_id")
        res = super().unlink()
        try:
            # Keep same behavior: mark orders after removing lines
            self._touch_parent_for_monta(orders=orders)
        except Exception as e:
            _logger.error("[Monta Sync] touch after unlink failed: %s", e, exc_info=True)
        return res
