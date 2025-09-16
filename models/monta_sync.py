import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)

VALID_SOURCES = ("orders", "shipments")

class MontaOrderStatus(models.Model):
    _name = "monta.order.status"
    _description = "Monta Order Status (read through from Monta)"
    _order = "id desc"

    sale_order_id = fields.Many2one("sale.order", index=True, ondelete="cascade")
    order_name    = fields.Char(index=True)
    status        = fields.Char()
    status_code   = fields.Char()
    source        = fields.Selection(selection=[(s, s) for s in VALID_SOURCES], default="orders")
    track_trace   = fields.Char()
    delivery_date = fields.Date()
    last_sync     = fields.Datetime()

    _sql_constraints = [
        ("order_unique", "unique(order_name)", "One status row per Monta order."),
    ]

    # -----------------------------
    # Helper: pull via existing SO method (no monta.http)
    # -----------------------------
    def _pull_via_sale_order(self, so):
        """
        Use the module’s existing integration:
        - call so.action_monta_sync_status()
        - read fields from sale.order
        Returns a dict with normalized keys.
        """
        # Call the method you already used manually
        try:
            so.sudo().action_monta_sync_status()
        except Exception as e:
            _logger.exception("Monta sync via sale.order failed for %s", so.name)
            return {}

        vals = so.read([
            "name",
            "monta_status",
            "monta_status_code",
            "monta_status_source",
            "monta_track_trace",
            "monta_last_sync",
            "x_monta_delivery_date",  # optional Studio field
        ])[0]

        return {
            "order_name": vals.get("name"),
            "status": vals.get("monta_status") or "",
            "status_code": (vals.get("monta_status_code") or "") and str(vals.get("monta_status_code")),
            "source": vals.get("monta_status_source") if vals.get("monta_status_source") in VALID_SOURCES else "orders",
            "track_trace": vals.get("monta_track_trace") or False,
            "delivery_date": vals.get("x_monta_delivery_date") or False,
            "last_sync": vals.get("monta_last_sync") or fields.Datetime.now(),
        }

    # -----------------------------
    # Upsert & mirror
    # -----------------------------
    @api.model
    def _upsert_from_sale_order(self, so):
        data = self._pull_via_sale_order(so)
        if not data or not data.get("order_name"):
            return False

        rec = self.search([("order_name", "=", data["order_name"])], limit=1)
        vals = dict(
            sale_order_id=so.id,
            order_name=data["order_name"],
            status=data["status"],
            status_code=data["status_code"],
            source=data["source"],
            track_trace=data["track_trace"],
            delivery_date=data["delivery_date"],
            last_sync=data["last_sync"],
        )
        if rec:
            rec.write(vals)
            _logger.info("[Monta] Updated status row for %s", data["order_name"])
        else:
            rec = self.create(vals)
            _logger.info("[Monta] Created status row for %s", data["order_name"])

        # Mirror back onto sale.order so Studio columns render in Sales dashboard
        mirror_vals = {
            "monta_order_id": rec.id,  # if you have an M2O/Integer helper field
            "monta_status": rec.status or False,
            "monta_status_code": rec.status_code or False,
            "monta_status_source": rec.source if rec.source in VALID_SOURCES else "orders",
            "monta_track_trace": rec.track_trace or False,
            "monta_last_sync": rec.last_sync,
        }
        # Optional Studio field (ignore if not present on model)
        if "x_monta_delivery_date" in so._fields:
            mirror_vals["x_monta_delivery_date"] = rec.delivery_date or False

        try:
            so.sudo().write(mirror_vals)
        except Exception as e:
            # Don’t break the sync if Studio field or source selection misconfigured
            safe = mirror_vals.copy()
            safe.pop("x_monta_delivery_date", None)
            try:
                so.sudo().write(safe)
            except Exception:
                _logger.exception("[Monta] Mirror write failed on sale.order %s", so.name)

        return rec

    # -----------------------------
    # Cron entry point (every 30 minutes)
    # -----------------------------
    @api.model
    def cron_monta_sync_status(self, batch_limit=200):
        """
        Scan recent/eligible orders, pull Monta status through sale.order’s own
        action, upsert our table, and mirror values back to sale.order.
        """
        dom = ["|", ("name", "=like", "BC%"), ("name", "=like", "S%")]
        sos = self.env["sale.order"].sudo().search(dom, limit=batch_limit, order="id desc")

        processed = 0
        for so in sos:
            rec = self._upsert_from_sale_order(so)
            if rec:
                processed += 1

        _logger.info("[Monta] Cron finished, processed %s orders", processed)
        return True
