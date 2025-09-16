# odoo/addons/Monta-Odoo-Integration/models/monta_sync.py
from odoo import api, fields, models, _
import logging
_logger = logging.getLogger(__name__)

class MontaOrderStatus(models.Model):
    _name = "monta.order.status"
    _description = "Monta Order Status"
    _rec_name = "order_name"

    sale_order_id = fields.Many2one("sale.order", index=True)
    order_name     = fields.Char(index=True)
    status         = fields.Char()
    status_code    = fields.Char()
    source         = fields.Selection([("orders","orders"),("shipments","shipments")], default="orders")
    track_trace    = fields.Char()
    delivery_date  = fields.Date()
    last_sync      = fields.Datetime(default=lambda self: fields.Datetime.now())

    # ---- Monta client helpers (replace request impl with yours) ----
    def _monta_get_order(self, name):
        """Return dict from Monta 'orders' endpoint; {} if none."""
        return self.env["monta.http"].sudo().get_json(f"/orders/{name}") or {}

    def _monta_get_shipment(self, name):
        """Fallback: shipments view of the same reference."""
        return self.env["monta.http"].sudo().get_json(f"/shipments/{name}") or {}

    # ---- Single upsert from Monta to our table ---------------------
    def _resolve_and_upsert(self, so):
        name = so.name
        data = self._monta_get_order(name) or {}
        src = "orders"
        if not data:
            data = self._monta_get_shipment(name) or {}
            src = "shipments"

        if not data:
            return False

        vals = {
            "sale_order_id": so.id,
            "order_name": name,
            "status": data.get("DeliveryStatusDescription") or data.get("Status") or "",
            "status_code": str(data.get("DeliveryStatusCode") or data.get("StatusID") or "" ),
            "source": src,
            "track_trace": data.get("TrackAndTraceUrl") or data.get("TrackAndTraceLink") or False,
            # Monta dates are usually ISO strings -> keep date part
            "delivery_date": (data.get("DeliveryDate") or data.get("PlannedDeliveryDate") or "")[:10] or False,
            "last_sync": fields.Datetime.now(),
        }

        rec = self.sudo().search([("sale_order_id","=",so.id)], limit=1)
        if rec:
            rec.write(vals)
            _logger.info("[Monta] Updated status row for %s", name)
        else:
            rec = self.sudo().create(vals)
            _logger.info("[Monta] Created status row for %s", name)
        return rec

    # ---- Mirror back to Studio fields on sale.order ----------------
    def _mirror_to_sale(self, rec):
        so = rec.sale_order_id
        if not so:
            return
        updates = {}
        # Only write into fields that exist on sale.order (Studio or native).
        fields_to_check = self.env["ir.model.fields"].sudo().search_read([
            ("model", "=", "sale.order"),
            ("name", "in", [
                "monta_status","monta_status_code","monta_status_source",
                "monta_track_trace","monta_last_sync","x_monta_delivery_date",
            ]),
        ], ["name"])
        existing = {f["name"] for f in fields_to_check}

        if "monta_status" in existing:         updates["monta_status"] = rec.status or False
        if "monta_status_code" in existing:     updates["monta_status_code"] = rec.status_code or False
        if "monta_status_source" in existing:   updates["monta_status_source"] = rec.source  # 'orders'/'shipments'
        if "monta_track_trace" in existing:     updates["monta_track_trace"] = rec.track_trace or False
        if "monta_last_sync" in existing:       updates["monta_last_sync"] = rec.last_sync
        if "x_monta_delivery_date" in existing: updates["x_monta_delivery_date"] = rec.delivery_date or False

        if updates:
            # prevent chatter spam
            so.with_context(mail_create_nolog=True, tracking_disable=True).sudo().write(updates)

    # ---- Cron entry point -----------------------------------------
    @api.model
    def cron_monta_sync_status(self, batch_limit=200):
        """Hourly job: pull Monta *orders* first; mirror to Studio fields."""
        SO = self.env["sale.order"].sudo()
        # choose candidates: orders that look like Monta refs (BC/S*) or already linked
        dom = ["|", ("name", "=like", "BC%"), ("name", "=like", "S%")]
        sos = SO.search(dom, limit=batch_limit)
        count = 0
        for so in sos:
            rec = self._resolve_and_upsert(so)
            if rec:
                self._mirror_to_sale(rec)
                count += 1
        _logger.info("[Monta] Cron finished, processed %s orders", count)
        return True
