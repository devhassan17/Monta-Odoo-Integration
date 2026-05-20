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

    def _action_launch_stock_rule(self, *args, **kwargs):
        """Bypass stock rules (prevent picking generation) for subscription orders."""
        sub_lines = self.env['sale.order.line']
        normal_lines = self.env['sale.order.line']
        
        for line in self:
            order = line.order_id
            f = order._fields
            is_sub = (
                ('is_subscription' in f and order.is_subscription)
                or ('plan_id' in f and bool(order.plan_id))
                or ('subscription_state' in f and getattr(order, 'subscription_state', '') in ('2_renewal', '3_progress', '4_paused'))
            )
            if is_sub:
                sub_lines |= line
            else:
                normal_lines |= line
                
        if sub_lines:
            _logger.info(
                "[Monta SO Hook] Bypassing stock rule generation for subscription SO lines: %s",
                sub_lines.ids
            )
            
        if normal_lines:
            return super(SaleOrderLine, normal_lines)._action_launch_stock_rule(*args, **kwargs)
            
        return True
