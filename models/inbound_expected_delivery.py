# # -*- coding: utf-8 -*-
# import logging
# from odoo import models

# _logger = logging.getLogger(__name__)

# class StockPickingInboundEDD(models.Model):
#     _inherit = 'stock.picking'

#     def write(self, vals):
#         res = super().write(vals)
#         if 'scheduled_date' in vals:
#             to_push = self.filtered(lambda p: p.picking_type_id.code == 'incoming' and p.scheduled_date)
#             if to_push:
#                 try:
#                     self.env['monta.inbound.edd.service']._push_many(to_push)
#                 except Exception as e:
#                     _logger.error("Auto EDD push failed for %s: %s", to_push.mapped('name'), e, exc_info=True)
#         return res
