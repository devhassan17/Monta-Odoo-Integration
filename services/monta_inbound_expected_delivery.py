# -*- coding: utf-8 -*-
import logging
from odoo import models, fields

_logger = logging.getLogger(__name__)

class MontaInboundEDDService(models.AbstractModel):
    _name = 'monta.inbound.edd.service'
    _description = 'Push inbound expected delivery date to Monta'

    def _reference_for(self, picking):
        # Use the picking name as inbound reference (adjust if your tenant expects something else)
        return picking.name

    def _payload_for(self, picking):
        # Odoo stores UTC; Monta happily takes "YYYY-MM-DD HH:MM:SS"
        return {
            "ExpectedDelivery": fields.Datetime.to_string(picking.scheduled_date) if picking.scheduled_date else None
        }

    def _push_one(self, picking):
        if not picking or not picking.scheduled_date:
            return (0, {'note': 'no scheduled_date'})
        order_proxy = self.env['sale.order'].browse()  # just for the logging helpers
        ref = self._reference_for(picking)
        path = f"/inbound/{ref}/expected-delivery"
        status, body = order_proxy._monta_request('PUT', path, payload=self._payload_for(picking))
        (picking.message_post if hasattr(picking, 'message_post') else _logger.info)(
            f"[Monta EDD] PUT {path} -> {status} {body}"
        )
        return status, body

    def _push_many(self, pickings):
        for p in pickings:
            try:
                self._push_one(p)
            except Exception as e:
                _logger.error("EDD push failed for %s: %s", p.name, e, exc_info=True)
