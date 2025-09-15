# -*- coding: utf-8 -*-
"""
sale.order fields + sync methods for Monta status
(Works with services/monta_status_resolver.MontaStatusResolver)

No XML required. A programmatic cron can call `cron_monta_sync_status`.
"""
import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    _inherit = "sale.order"

    # ── persisted status fields ───────────────────────────────────────────────
    monta_status = fields.Char(string="Monta Status", copy=False, index=True)
    monta_status_code = fields.Char(string="Monta Status Code", copy=False)
    monta_status_source = fields.Selection(
        selection=[
            ("shipments", "Shipments"),
            ("orderevents", "Order Events"),
            ("orders", "Orders Header"),
        ],
        string="Monta Status Source",
        copy=False,
    )
    monta_track_trace = fields.Char(string="Monta Track & Trace", copy=False)
    monta_last_sync = fields.Datetime(string="Monta Last Sync", copy=False)

    # ── reference selection ──────────────────────────────────────────────────
    def _monta_candidate_reference(self):
        """
        Override this if you store Monta's external reference elsewhere.
        Default: sale.name; use client_order_ref if you prefer:
            return self.client_order_ref or self.name
        """
        self.ensure_one()
        return self.name

    # ── public API: manual sync for any recordset ────────────────────────────
    def action_monta_sync_status(self):
        """
        Manual entrypoint (callable from shell / server action).
        """
        _logger.info("[Monta] Manual status sync for %d sale.order records", len(self))
        self._monta_sync_batch()
        return True

    # ── cron target: keep signature stable for programmatic cron ─────────────
    @api.model
    def cron_monta_sync_status(self, batch_limit=200):
        """
        Programmatic cron target. Selects a batch of orders that likely need updates.
        Tune the domain to match your flow.
        """
        domain = [
            ("state", "in", ["sale", "done"]),     # adjust if needed
            # Comment this out if you want to keep syncing after shipped:
            ("monta_status", "!=", "Shipped"),
        ]
        orders = self.search(domain, limit=batch_limit, order="write_date desc")
        _logger.info("[Monta] Hourly cron: syncing %d sale.order records", len(orders))
        orders._monta_sync_batch()
        _logger.info("[Monta] Hourly cron: finished")
        return True

    # ── core: batch sync using the resolver service ──────────────────────────
    def _monta_sync_batch(self):
        """
        Iterate current recordset, resolve status via resolver and write results.
        Robust logging for QA.
        """
        # lazy import to avoid circulars
        from ..services.monta_status_resolver import MontaStatusResolver

        env = self.env
        try:
            resolver = MontaStatusResolver(env)
        except Exception as e:
            _logger.exception("[Monta] Resolver init failed (check System Parameters): %s", e)
            return

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
                _logger.warning("[Monta] %s (%s) -> no status returned (meta=%s)", so.name, ref, meta)
                continue

            vals = {
                "monta_status": status,
                "monta_status_code": (meta or {}).get("status_code"),
                "monta_status_source": (meta or {}).get("source") or "orders",
                "monta_track_trace": (meta or {}).get("track_trace"),
                "monta_last_sync": fields.Datetime.now(),
            }

            try:
                so.write(vals)
            except Exception as e:
                _logger.exception("[Monta] %s (%s) -> write failed: %s", so.name, ref, e)
            else:
                _logger.info(
                    "[Monta] %s (%s) -> %s (src=%s, code=%s, t&t=%s)",
                    so.name,
                    ref,
                    vals["monta_status"],
                    vals["monta_status_source"],
                    vals["monta_status_code"],
                    vals["monta_track_trace"],
                )
