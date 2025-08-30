# -*- coding: utf-8 -*-
import logging
from odoo import fields
from .monta_client import MontaClient

_logger = logging.getLogger(__name__)


class _LogProxy:
    """
    Lightweight proxy so MontaClient can call `_create_monta_log(...)`
    without requiring a real sale.order. We just log to server logs.
    """
    def _create_monta_log(self, payload, level='info', tag='Monta API', console_summary=None):
        msg = f"[{tag}] {console_summary or ''} | {payload}"
        if level == 'error':
            _logger.error(msg)
        else:
            _logger.info(msg)


class MontaInboundEDDService:
    """
    Pushes Expected Delivery Date for inbound registrations (stock.picking of type 'incoming').

    We use the native `scheduled_date` of the picking as the Expected Delivery.
    Adjust the endpoint path if your Monta tenant differs.
    """
    def __init__(self, env):
        self.env = env

    # Choose the reference used by Monta to find the inbound registration.
    # Often this is the picking name (e.g. WH/IN/0001) or a vendor ref.
    def _reference_for(self, picking):
        return picking.name

    def _payload_for(self, picking):
        return {
            "ExpectedDelivery": picking.scheduled_date and
                                fields.Datetime.to_string(picking.scheduled_date)
        }

    def push_one(self, picking):
        # If there's no date, skip pushing.
        if not picking.scheduled_date:
            _logger.info("[Monta EDD] %s has no scheduled_date; skipping push.", picking.name)
            return 0, {'note': 'No scheduled_date on picking'}

        client = MontaClient(self.env)
        ref = self._reference_for(picking)
        path = f"/inbound/{ref}/expected-delivery"

        # Use our proxy to satisfy MontaClient logging hooks.
        proxy = _LogProxy()

        status, body = client.request(proxy, "PUT", path, payload=self._payload_for(picking))
        (picking.message_post if hasattr(picking, 'message_post') else _logger.info)(
            f"[Monta EDD] Push {ref} -> {status} {body}"
        )
        return status, body

    def push_many(self, pickings):
        for p in pickings:
            try:
                self.push_one(p)
            except Exception as e:
                _logger.error("[Monta EDD] Push failed for %s: %s", p.name, e, exc_info=True)
