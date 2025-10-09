# -*- coding: utf-8 -*-
import logging
from odoo import models, fields

_logger = logging.getLogger(__name__)

class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    def _touch_parent_for_monta_if(self):
        orders = self.mapped('order_id').filtered(lambda p: p.state in ('purchase', 'done'))
        if not orders:
            return
        orders.write({'monta_if_needs_sync': True})
        for po in orders:
            try:
                if po._if_should_push_now():
                    po.action_monta_push_inbound_forecast()
            except Exception as e:
                _logger.error("[Monta IF] Parent push after POL change failed for %s: %s", po.name, e, exc_info=True)

    def create(self, vals_list):
        recs = super().create(vals_list)
        try:
            recs._touch_parent_for_monta_if()
        except Exception as e:
            _logger.error("[Monta IF] touch after POL create failed: %s", e, exc_info=True)
        return recs

    def write(self, vals):
        res = super().write(vals)
        try:
            if {'product_id', 'product_qty', 'price_unit', 'name', 'date_planned', 'taxes_id'}.intersection(vals.keys()):
                self._touch_parent_for_monta_if()
        except Exception as e:
            _logger.error("[Monta IF] touch after POL write failed: %s", e, exc_info=True)
        return res

    def unlink(self):
        orders = self.mapped('order_id')
        res = super().unlink()
        try:
            orders.write({'monta_if_needs_sync': True})
            for po in orders.filtered(lambda p: p.state in ('purchase', 'done')):
                if po._if_should_push_now():
                    po.action_monta_push_inbound_forecast()
        except Exception as e:
            _logger.error("[Monta IF] touch after POL unlink failed: %s", e, exc_info=True)
        return res
