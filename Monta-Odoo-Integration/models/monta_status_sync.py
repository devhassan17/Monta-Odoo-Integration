# -*- coding: utf-8 -*-
import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)

class SaleOrder(models.Model):
    _inherit = "sale.order"

    monta_status = fields.Char(string="Monta Status", copy=False, index=True)
    monta_status_code = fields.Char(string="Monta Status Code", copy=False)
    monta_status_source = fields.Selection(
        selection=[("shipments","Shipments"),("orderevents","Order Events"),("orders","Orders Header")],
        string="Monta Status Source", copy=False)
    monta_track_trace = fields.Char(string="Monta Track & Trace", copy=False)
    monta_last_sync = fields.Datetime(string="Monta Last Sync", copy=False)

    def _monta_candidate_reference(self):
        self.ensure_one()
        return self.name

    def action_monta_sync_status(self):
        _logger.info("[Monta] Manual sync for %d sales orders", len(self))
        self._monta_sync_batch()
        return True

    @api.model
    def cron_monta_sync_status(self, batch_limit=200):
        domain = [("state","in",["sale","done"]), ("monta_status","!=", "Shipped")]
        orders = self.search(domain, limit=batch_limit, order="write_date desc")
        _logger.info("[Monta] Cron sync starting for %d orders", len(orders))
        orders._monta_sync_batch()
        _logger.info("[Monta] Cron sync finished")
        return True

    def _monta_sync_batch(self):
        from ..services.monta_status_resolver import MontaStatusResolver
        Snapshot = self.env["monta.order.status"].sudo()

        try:
            resolver = MontaStatusResolver(self.env)
        except Exception as e:
            _logger.exception("[Monta] Resolver init failed (check System Parameters): %s", e)
            return

        for so in self:
            ref = so._monta_candidate_reference()
            if not ref:
                _logger.warning("[Monta] %s has no reference; skipping", so.display_name)
                continue

            try:
                status, meta = resolver.resolve(ref)
            except Exception as e:
                _logger.exception("[Monta] %s (%s) -> resolve() failed: %s", so.name, ref, e)
                continue

            # Not found -> mark as not available on Monta, upsert snapshot with reason
            if not status:
                try:
                    # mirror the boolean if field exists
                    if "monta_on_monta" in so._fields:
                        so.write({"monta_on_monta": False})
                    Snapshot.upsert_for_order(
                        so,
                        order_status=False,
                        delivery_message=(meta or {}).get("reason"),
                        status_raw=(meta or {}).get("status_raw"),
                        last_sync=fields.Datetime.now(),
                    )
                except Exception:
                    _logger.exception("[Monta] Snapshot upsert failed for %s on not-found", so.name)
                _logger.warning("[Monta] %s (%s) -> no status returned (%s)", so.name, ref, meta)
                continue

            # Found -> write mirrors on sale.order (including Available on Monta)
            vals_so = {
                "monta_status": status,
                "monta_status_code": (meta or {}).get("status_code"),
                "monta_status_source": (meta or {}).get("source") or "orders",
                "monta_track_trace": (meta or {}).get("track_trace"),
                "monta_last_sync": fields.Datetime.now(),
            }

            # Optional mirrors if you’ve added them (safe checks)
            if "monta_order_ref" in so._fields:
                vals_so["monta_order_ref"] = (meta or {}).get("monta_order_ref")
            if "monta_delivery_message" in so._fields:
                vals_so["monta_delivery_message"] = (meta or {}).get("delivery_message")
            if "monta_delivery_date" in so._fields:
                vals_so["monta_delivery_date"] = (meta or {}).get("delivery_date")
            if "monta_status_raw" in so._fields:
                vals_so["monta_status_raw"] = (meta or {}).get("status_raw")

            # NEW: mirror Available on Monta (true if we have a stable Monta ref)
            if "monta_on_monta" in so._fields:
                vals_so["monta_on_monta"] = bool((meta or {}).get("monta_order_ref"))

            # Write to SO
            try:
                so.write(vals_so)
            except Exception as e:
                _logger.exception("[Monta] %s (%s) -> write failed: %s", so.name, ref, e)

            # Snapshot for history/audit
            try:
                Snapshot.upsert_for_order(
                    so,
                    monta_order_ref=(meta or {}).get("monta_order_ref") or so.name,
                    order_status=status,
                    delivery_message=(meta or {}).get("delivery_message"),
                    track_trace_url=(meta or {}).get("track_trace"),
                    delivery_date=(meta or {}).get("delivery_date"),
                    status_raw=(meta or {}).get("status_raw"),
                    last_sync=fields.Datetime.now(),
                )
            except Exception as e:
                _logger.exception("[Monta] Snapshot upsert failed for %s: %s", so.name, e)
