# -*- coding: utf-8 -*-
import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)

class MontaOrderStatus(models.Model):
    _name = "monta.order.status"
    _description = "Monta Order Status snapshot"
    _order = "last_sync desc, id desc"

    order_name = fields.Char(required=True, index=True)
    sale_order_id = fields.Many2one("sale.order", ondelete="cascade", index=True)

    # IMPORTANT: keep this a Selection to avoid the upgrade crash.
    source = fields.Selection(
        [("orders", "orders"), ("shipments", "shipments"), ("events", "events")],
        default="orders",
        string="API Source",
        help="Where the status came from in Monta API.",
    )
    status = fields.Char(string="Status")
    status_code = fields.Char(string="Status Code")
    track_trace = fields.Char(string="Track & Trace URL")
    delivery_date = fields.Datetime(string="Delivery Date")
    last_sync = fields.Datetime(index=True, default=fields.Datetime.now)

    _sql_constraints = [
        ("monta_order_name_unique", "unique(order_name)", "Monta order name must be unique."),
    ]

    # --------------------------- helpers ---------------------------
    def _monta_get_order(self, name: str) -> dict:
        return self.env["monta.http"].sudo().get_json(f"/orders/{name}") or {}

    @api.model
    def _map_monta_payload(self, so, data: dict) -> dict:
        status_txt = data.get("DeliveryStatusDescription") or data.get("Status") or ""
        code = data.get("StatusID") or data.get("DeliveryStatusCode") or data.get("Code") or ""
        tnt = data.get("TrackAndTraceLink") or data.get("TrackAndTraceUrl") or ""
        # accept either date or datetime; store as datetime for UI clarity
        delivery = data.get("DeliveryDate") or data.get("EstimatedDeliveryTo") or data.get("ShippedDate") or None

        return {
            "order_name": so.name,
            "sale_order_id": so.id,
            "status": status_txt or False,
            "status_code": str(code) if code not in (None, "") else False,
            "source": "orders",
            "track_trace": tnt or False,
            "delivery_date": delivery or False,
            "last_sync": fields.Datetime.now(),
        }

    def _mirror_to_sale(self, rec):
        """Mirror into sale.order if fields exist. No Studio dependency required."""
        so = rec.sale_order_id
        if not so:
            return
        can = so._fields
        vals = {}
        if "monta_status" in can:
            vals["monta_status"] = rec.status or False
        if "monta_status_code" in can:
            vals["monta_status_code"] = rec.status_code or False
        if "monta_status_source" in can:
            vals["monta_status_source"] = rec.source or "orders"
        if "monta_track_trace" in can:
            vals["monta_track_trace"] = rec.track_trace or False
        if "monta_last_sync" in can:
            vals["monta_last_sync"] = rec.last_sync
        if "x_monta_delivery_date" in can:
            vals["x_monta_delivery_date"] = rec.delivery_date or False
        if vals:
            so.sudo().write(vals)

    # --------------------------- public API ---------------------------
    @api.model
    def _resolve_and_upsert(self, so):
        if not so or not so.name:
            return False
        data = self._monta_get_order(so.name)
        if not data:
            _logger.info("[Monta] No payload for order %s", so.name)
            return False

        vals = self._map_monta_payload(so, data)
        rec = self.search([("order_name", "=", so.name)], limit=1)
        if rec:
            rec.sudo().write(vals)
        else:
            rec = self.sudo().create(vals)

        self._mirror_to_sale(rec)
        return rec

    @api.model
    def cron_monta_sync_status(self, batch_limit=300):
        """Picked up by cron: sync and mirror."""
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
