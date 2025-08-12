# -*- coding: utf-8 -*-
from odoo import models, fields
import logging
_logger = logging.getLogger(__name__)

class ProductProduct(models.Model):
    _inherit = 'product.product'

    monta_sku = fields.Char(
        string="Monta SKU",
        help="Explicit SKU used when sending orders to Monta. If empty, connector tries: default_code → first supplier code → barcode."
    )

    def write(self, vals):
        res = super().write(vals)
        # If identifiers changed, trigger resync for related open orders
        sku_related = {'monta_sku', 'default_code', 'barcode', 'seller_ids'}
        if sku_related.intersection(vals.keys()):
            try:
                self._trigger_monta_resync_for_open_orders()
            except Exception as e:
                _logger.error(f"[Monta Resync] Failed to trigger resync after product write: {e}")
        return res

    def _trigger_monta_resync_for_open_orders(self):
        """Mark related open orders for sync and push update immediately."""
        if not self:
            return
        SOL = self.env['sale.order.line']
        lines = SOL.search([
            ('product_id', 'in', self.ids),
            ('order_id.state', 'in', ('sale', 'done')),
        ])
        orders = lines.mapped('order_id').filtered(lambda o: o.state != 'cancel')
        if not orders:
            return
        orders.write({'monta_needs_sync': True})
        for o in orders:
            try:
                if hasattr(o, '_should_push_now'):
                    if o._should_push_now():
                        o._monta_update()
                else:
                    o._monta_update()
            except Exception as e:
                _logger.error(f"[Monta Resync] Order {o.name} update after SKU fix failed: {e}")
