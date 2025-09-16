# -*- coding: utf-8 -*-
from odoo import models, api, fields
import logging
from odoo.tools import mute_logger

_logger = logging.getLogger(__name__)

class MontaOrderStatus(models.Model):
    _inherit = "monta.order.status"

    @api.model
    def _resolve_and_upsert(self, sale_order):
        """Fetch from Monta and upsert into monta.order.status (no change to sale.order)."""
        from odoo.addons.Monta_Odoo_Integration.services.monta_status_resolver import MontaStatusResolver
        resolver = MontaStatusResolver(self.env)
        ref = sale_order.name  # you can change this if you map differently
        status, meta = resolver.resolve(ref)
        vals = {
            "sale_order_id": sale_order.id,
            "order_name": ref,
            "status": status or False,
            "status_code": (str(meta.get("code")) if meta and meta.get("code") is not None else False),
            "source": (meta or {}).get("source") or "orders",
            "track_trace": (meta or {}).get("track_trace") or False,
            "delivery_date": (meta or {}).get("delivery_date") or False,
            "last_sync": fields.Datetime.now(),
        }
        rec = self.search([("order_name", "=", ref)], limit=1)
        if rec:
            rec.write(vals)
            _logger.info("[Monta] Updated status row for %s", ref)
            return rec
        rec = self.create(vals)
        _logger.info("[Monta] Created status row for %s", ref)
        return rec

    @api.model
    def cron_monta_sync_status(self, batch_limit=50):
        """Hourly cron entry point — only reads sale.order and writes *our* model."""
        dom = [("state", "in", ["sale", "done"])]
        orders = self.env["sale.order"].search(dom, limit=batch_limit, order="write_date desc")
        _logger.info("[Monta] Cron sync: %s orders", len(orders))
        for so in orders:
            try:
                self._resolve_and_upsert(so)
            except Exception as e:
                _logger.exception("Monta sync failed for %s: %s", so.name, e)
        _logger.info("[Monta] Cron finished")
        return True
