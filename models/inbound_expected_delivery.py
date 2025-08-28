# -*- coding: utf-8 -*-
from odoo import models, fields, api
import logging
_logger = logging.getLogger(__name__)

class StockPicking(models.Model):
    _inherit = 'stock.picking'

    x_monta_expected_delivery_date = fields.Datetime(
        string="Monta Expected Delivery",
        help="Expected delivery date for this inbound registration at Monta."
    )
    x_monta_edd_needs_sync = fields.Boolean(default=False, copy=False)

    def write(self, vals):
        res = super().write(vals)
        if {'x_monta_expected_delivery_date'}.intersection(vals.keys()):
            self.filtered(lambda p: p.picking_type_id.code == 'incoming').write({'x_monta_edd_needs_sync': True})
        return res

    def action_push_monta_edd_now(self):
        """Manual push button (add via UI button if you like)."""
        from ..services.monta_inbound_expected_delivery import MontaInboundEDDService
        MontaInboundEDDService(self.env).push_many(self.filtered(lambda p: p.picking_type_id.code == 'incoming'))
        return True