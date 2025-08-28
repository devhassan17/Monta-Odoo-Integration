# -*- coding: utf-8 -*-
import logging
from .monta_client import MontaClient
_logger = logging.getLogger(__name__)

class MontaInboundEDDService:
    """
    Pushes Expected Delivery Date for inbound registrations (stock.picking of type 'incoming').
    Expects a Monta endpoint like /inbound/{reference}/expected-delivery or a PATCH on /inbound.
    If your tenant uses another route, only tweak PATH building below.
    """
    def __init__(self, env):
        self.env = env

    def _reference_for(self, picking):
        # Choose your reference (name/origin/vendor ref). Default to picking.name
        return picking.name

    def _payload_for(self, picking):
        return {
            "ExpectedDelivery": picking.x_monta_expected_delivery_date and
                                fields.Datetime.to_string(picking.x_monta_expected_delivery_date)
        }

    def push_one(self, picking):
        if not picking.x_monta_expected_delivery_date:
            return
        client = MontaClient(self.env)
        ref = self._reference_for(picking)
        path = f"/inbound/{ref}/expected-delivery"
        # Reuse sale.order logging via a fake order-like wrapper:
        order_log_proxy = self.env['sale.order'].browse()  # empty; we’ll just log at INFO level here
        status, body = client.request(order_log_proxy, "PUT", path, payload=self._payload_for(picking))
        (picking.message_post if hasattr(picking, 'message_post') else _logger.info)(
            f"[Monta EDD] {ref} -> {status} {body}"
        )
        if 200 <= (status or 0) < 300:
            picking.write({'x_monta_edd_needs_sync': False})
        return status, body

    def push_many(self, pickings):
        for p in pickings:
            try:
                self.push_one(p)
            except Exception as e:
                _logger.error("EDD push failed for %s: %s", p.name, e, exc_info=True)
