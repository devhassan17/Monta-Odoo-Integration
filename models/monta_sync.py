import logging
from datetime import datetime

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

class MontaOrderStatus(models.Model):
    _name = "monta.order.status"
    _description = "Monta Order Status snapshot"
    _order = "last_sync desc"

    order_name = fields.Char(required=True, index=True)
    sale_order_id = fields.Many2one("sale.order", ondelete="cascade", index=True)
    status = fields.Char()
    status_code = fields.Char()
    source = fields.Selection([("orders", "orders"), ("shipments", "shipments")], default="orders")
    track_trace = fields.Char()
    delivery_date = fields.Date()
    last_sync = fields.Datetime(index=True, default=fields.Datetime.now)

    _sql_constraints = [
        ("monta_order_name_unique", "unique(order_name)", "Monta order name must be unique."),
    ]

    # --------------------------- helpers ---------------------------

    def _monta_get_order(self, name: str) -> dict:
        """Pull a single order by name from Monta 'orders' endpoint."""
        return self.env["monta.http"].sudo().get_json(f"/orders/{name}") or {}

    @api.model
    def _map_monta_payload(self, so, data: dict) -> dict:
        """Map Monta JSON into our fields. Be generous with keys."""
        # Common keys observed across conversation:
        # - "Reference", "DeliveryStatusDescription", "StatusID" or "Code"
        # - "TrackAndTraceLink" or "TrackAndTraceUrl"
        # - "DeliveryDate" (ISO date)
        status_txt = data.get("DeliveryStatusDescription") or data.get("Status") or ""
        code = data.get("StatusID") or data.get("DeliveryStatusCode") or data.get("Code") or ""
        tnt = data.get("TrackAndTraceLink") or data.get("TrackAndTraceUrl") or ""
        delivery = data.get("DeliveryDate") or None

        vals = {
            "order_name": so.name,
            "sale_order_id": so.id,
            "status": status_txt,
            "status_code": str(code) if code not in (None, "") else "",
            "source": "orders",
            "track_trace": tnt or False,
            "delivery_date": delivery or False,
            "last_sync": fields.Datetime.now(),
        }
        return vals

    def _mirror_to_sale(self, rec):
        """Mirror key values onto sale.order, **only** if fields exist."""
        so = rec.sale_order_id
        if not so:
            return
        # Only write fields that actually exist on sale.order to keep it safe.
        can = so._fields
        vals = {}

        if "monta_order_id" in can:
            vals["monta_order_id"] = rec.id
        if "monta_status" in can:
            vals["monta_status"] = rec.status or False
        if "monta_status_code" in can:
            vals["monta_status_code"] = rec.status_code or False
        if "monta_status_source" in can:
            vals["monta_status_source"] = rec.source if rec.source in ("orders", "shipments") else "orders"
        if "monta_track_trace" in can:
            vals["monta_track_trace"] = rec.track_trace or False
        if "monta_last_sync" in can:
            vals["monta_last_sync"] = rec.last_sync

        # If you created this *Studio* field, we mirror delivery date.
        if "x_monta_delivery_date" in can:
            vals["x_monta_delivery_date"] = rec.delivery_date or False

        if vals:
            so.sudo().write(vals)

    # --------------------------- public API ---------------------------

    @api.model
    def _resolve_and_upsert(self, so):
        """Pull Monta data for SO; upsert snapshot; mirror to sale.order.
           Returns the record or False if Monta returned nothing."""
        if not so or not so.name:
            return False

        data = self._monta_get_order(so.name)
        if not data:
            _logger.info("[Monta] No payload for order %s", so.name)
            return False

        vals = self._map_monta_payload(so, data)

        # upsert by order_name (unique)
        rec = self.search([("order_name", "=", so.name)], limit=1)
        if rec:
            rec.sudo().write(vals)
            _logger.info("[Monta] Updated status row for %s", so.name)
        else:
            rec = self.sudo().create(vals)
            _logger.info("[Monta] Created status row for %s", so.name)

        self._mirror_to_sale(rec)
        return rec

    @api.model
    def cron_monta_sync_status(self, batch_limit=300):
        """Cron target: Every 30 min.
           - Picks SOs that look like Monta orders (BC% or S%)
           - Upserts snapshots
           - Mirrors fields for Studio/dashboard
        """
        SO = self.env["sale.order"].sudo()
        dom = ["|", ("name", "=like", "BC%"), ("name", "=like", "S%")]
        orders = SO.search(dom, limit=batch_limit, order="id")
        processed = 0
        for so in orders:
            try:
                if self._resolve_and_upsert(so):
                    processed += 1
            except Exception as e:
                _logger.exception("[Monta] Failed syncing %s: %s", so.name, e)
        _logger.info("[Monta] Cron finished, processed %s orders", processed)
        return True
