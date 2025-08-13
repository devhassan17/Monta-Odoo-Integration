# -*- coding: utf-8 -*-
import logging
from odoo import models

_logger = logging.getLogger(__name__)

class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    def _touch_parent_for_monta(self):
        orders = self.mapped('order_id').filtered(lambda o: o.state in ('sale', 'done') and o.state != 'cancel')
        if not orders:
            return
        orders.write({'monta_needs_sync': True})
        for o in orders:
            try:
                if hasattr(o, '_should_push_now') and o._should_push_now():
                    o._monta_update()
            except Exception as e:
                _logger.error("[Monta Sync] Order %s update after line change failed: %s", o.name, e, exc_info=True)

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
            if {'product_id', 'product_uom_qty', 'name', 'price_unit', 'tax_id'}.intersection(vals.keys()):
                self._touch_parent_for_monta()
        except Exception as e:
            _logger.error("[Monta Sync] touch after write failed: %s", e, exc_info=True)
        return res

    def unlink(self):
        orders = self.mapped('order_id')
        res = super().unlink()
        try:
            orders.write({'monta_needs_sync': True})
            for o in orders:
                if hasattr(o, '_should_push_now') and o._should_push_now():
                    o._monta_update()
        except Exception as e:
            _logger.error("[Monta Sync] touch after unlink failed: %s", e, exc_info=True)
        return res
