# -*- coding: utf-8 -*-
from odoo import models

class DeliveryCarrier(models.Model):
    _inherit = 'delivery.carrier'

    def rate_shipment(self, order):
        """
        Override rate_shipment to enforce Monta's custom delivery prices.
        If the order has selected a specific Monta delivery type, we force the price
        to ignore whatever rule the carrier has (e.g. Box = €2.00).
        """
        res = super(DeliveryCarrier, self).rate_shipment(order)
        
        if hasattr(order, 'monta_delivery_type') and order.monta_delivery_type:
            if order.monta_delivery_type == 'next_day':
                res['price'] = 1.0
                res['success'] = True
                res['error_message'] = False
                res['warning_message'] = False
            elif order.monta_delivery_type in ('two_day', 'standard') and not order.monta_shipper_code:
                # Standard delivery is free, but we don't override pickup points here
                res['price'] = 0.0
                res['success'] = True
                res['error_message'] = False
                res['warning_message'] = False

        return res
