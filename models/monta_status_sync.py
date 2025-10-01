#models/monta_status_sync.py
# -*- coding: utf-8 -*-
"""
sale.order fields + sync methods for Monta status
(Uses services.monta_status_resolver.MontaStatusResolver)
"""
import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)

class SaleOrder(models.Model):
    _inherit = "sale.order"

    monta_status = fields.Char(string="Monta Status", copy=False, index=True)
    monta_status_code = fields.Char(string="Monta Status Code", copy=False)
    monta_status_source = fields.Selection(
        selection=[("shipments", "Shipments"), ("orderevents", "Order Events"), ("orders", "Orders Header")],
        string="Monta Status Source",
        copy=False,
    )
    monta_track_trace = fields.Char(string="Monta Track & Trace", copy=False)
    monta_last_sync = fields.Datetime(string="Monta Last Sync", copy=False)

    def _monta_candidate_reference(self):
        self.ensure_one()
        # For your numbering BCnnnnn, this is correct.
        return self.name

    def action_monta_sync_status(self):
        _logger.info("[Monta] Manual status sync for %d sale.order records", len(self))
        self._monta_sync_batch()
        return True

    @api.model
    def cron_monta_sync_status(self, batch_limit=200):
        domain = [("state", "in", ["sale", "done"]), ("monta_status", "!=", "Shipped")]
        orders = self.search(domain, limit=batch_limit, order="write_date desc")
        _logger.info("[Monta] Hourly cron: syncing %d sale.order records", len(orders))
        orders._monta_sync_batch()
        _logger.info("[Monta] Hourly cron: finished")
        return True

    def _monta_sync_batch(self):
        from ..services.monta_status_resolver import MontaStatusResolver

        try:
            resolver = MontaStatusResolver(self.env)
        except Exception as e:
            _logger.exception("[Monta] Resolver init failed (check System Parameters): %s", e)
            return

        Snapshot = self.env["monta.order.status"].sudo()

        for so in self:
            ref = so._monta_candidate_reference()
            if not ref:
                _logger.warning("[Monta] %s has no external reference; skipping", so.display_name)
                continue

            try:
                status, meta = resolver.resolve(ref)
            except Exception as e:
                _logger.exception("[Monta] %s (%s) -> resolve() raised: %s", so.name, ref, e)
                continue

            if not status:
                # record “not found” reason on snapshot so you can see it
                Snapshot.upsert_for_order(
                    so,
                    order_status=False,
                    delivery_message=(meta or {}).get("reason"),
                    last_sync=fields.Datetime.now(),
                )
                _logger.warning("[Monta] %s (%s) -> no status returned (meta=%s)", so.name, ref, meta)
                continue

            vals_so = {
                "monta_status": status,
                "monta_status_code": (meta or {}).get("status_code"),
                "monta_status_source": (meta or {}).get("source") or "orders",
                "monta_track_trace": (meta or {}).get("track_trace"),
                "monta_last_sync": fields.Datetime.now(),
            }
            try:
                so.write(vals_so)
            except Exception as e:
                _logger.exception("[Monta] %s (%s) -> write failed: %s", so.name, ref, e)

            try:
                Snapshot.upsert_for_order(
                    so,
                    monta_order_ref=(meta or {}).get("monta_order_ref") or so.name,
                    order_status=status,
                    delivery_message=(meta or {}).get("delivery_message"),
                    track_trace_url=(meta or {}).get("track_trace"),
                    delivery_date=(meta or {}).get("delivery_date"),
                    last_sync=fields.Datetime.now(),
                )
            except Exception as e:
                _logger.exception("[Monta] Snapshot upsert failed for %s: %s", so.name, e)
