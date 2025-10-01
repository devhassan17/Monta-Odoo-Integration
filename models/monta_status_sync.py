# -*- coding: utf-8 -*-
"""
Sync Monta status back to sale.order + snapshot.
"""
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
        _logger.info("[Monta] Manual sync for %d orders", len(self))
        self._monta_sync_batch()
        return True

    @api.model
    def cron_monta_sync_status(self, batch_limit=200):
        domain = [("state","in",["sale","done"]),("monta_status","!=","Shipped")]
        orders = self.search(domain, limit=batch_limit, order="write_date desc")
        _logger.info("[Monta] Cron sync %d orders", len(orders))
        orders._monta_sync_batch()
        return True

    def _monta_sync_batch(self):
        from ..services.monta_status_resolver import MontaStatusResolver
        Snapshot = self.env["monta.order.status"].sudo()
        try:
            resolver = MontaStatusResolver(self.env)
        except Exception as e:
            _logger.exception("[Monta] Resolver init failed: %s", e)
            return
        for so in self:
            ref = so._monta_candidate_reference()
            try:
                status, meta = resolver.resolve(ref)
            except Exception as e:
                _logger.exception("[Monta] %s (%s) -> resolve() failed: %s", so.name, ref, e)
                continue
            if not status:
                Snapshot.upsert_for_order(so, order_status=False,
                    delivery_message=(meta or {}).get("reason"),
                    last_sync=fields.Datetime.now())
                continue
            vals_so = {
                "monta_status": status,
                "monta_status_code": (meta or {}).get("status_code"),
                "monta_status_source": (meta or {}).get("source"),
                "monta_track_trace": (meta or {}).get("track_trace"),
                "monta_last_sync": fields.Datetime.now(),
            }
            so.write(vals_so)
            Snapshot.upsert_for_order(
                so,
                monta_order_ref=(meta or {}).get("monta_order_ref") or so.name,
                order_status=status,
                delivery_message=(meta or {}).get("delivery_message"),
                track_trace_url=(meta or {}).get("track_trace"),
                delivery_date=(meta or {}).get("delivery_date"),
                last_sync=fields.Datetime.now(),
            )
