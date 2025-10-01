#monta_sync.py
# -*- coding: utf-8 -*-
import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# IMPORTANT:
# This file must EXTEND the model only. Do NOT redefine the model with _name.
# The fields (including status_raw) are defined in models/monta_order_status.py.
class MontaOrderStatus(models.Model):
    _inherit = "monta.order.status"

    # ------------------ helpers (no field declarations here) ------------------

    @staticmethod
    def _first(obj):
        if isinstance(obj, list) and obj:
            return obj[0]
        if isinstance(obj, dict):
            return obj
        return None

    def _monta_get_order(self, name: str) -> dict:
        http = self.env["monta.http"].sudo()

        for params in ({"search": name}, {"clientReference": name}, {"webshopOrderId": name}):
            data = http.get_json("orders", params=params)
            if isinstance(data, list) and data:
                o = self._first(data)
                oid = (o or {}).get("Id")
                if oid:
                    full = http.get_json(f"orders/{oid}")
                    if isinstance(full, dict) and full:
                        return full
                return o or {}
            if isinstance(data, dict) and data:
                return data

        _logger.info("[Monta] No payload for order %s", name)
        return {}

    @api.model
    def _map_monta_payload(self, so, data: dict) -> dict:
        # Flexible status text/code picking
        status_txt = (
            data.get("DeliveryStatusDescription")
            or data.get("Status")
            or data.get("CurrentStatus")
            or ""
        )
        code = data.get("StatusID") or data.get("DeliveryStatusCode") or data.get("Code") or ""
        tnt = (
            data.get("TrackAndTraceLink")
            or data.get("TrackAndTraceUrl")
            or data.get("TrackAndTrace")
            or ""
        )
        # delivery message field (varies per tenant; fallbacks)
        delivery_msg = (
            data.get("DeliveryMessage")
            or data.get("Message")
            or data.get("Remark")
            or ""
        )
        # prefer shipped / latest delivery style keys if present
        delivery = (
            data.get("DeliveryDate")
            or data.get("EstimatedDeliveryTo")
            or data.get("LatestDeliveryDate")
            or None
        )
        # a human Monta reference/id (use whatever is present)
        monta_ref = (
            data.get("WebshopOrderId")
            or data.get("ClientReference")
            or data.get("Reference")
            or so.name
        )

        return {
            "order_name": so.name,
            "sale_order_id": so.id,
            "status": status_txt,
            "status_code": str(code) if code not in (None, "") else "",
            "source": "orders",
            "delivery_message": delivery_msg or False,
            "monta_order_ref": monta_ref or False,
            "track_trace": tnt or False,
            "delivery_date": delivery or False,
            "last_sync": fields.Datetime.now(),
            # if you want to stash the raw payload as text:
            # "status_raw": json.dumps(data, ensure_ascii=False),
        }

    def _mirror_to_sale(self, rec):
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
            vals["monta_status_source"] = rec.source
        if "monta_track_trace" in can:
            vals["monta_track_trace"] = rec.track_trace or False
        if "monta_last_sync" in can:
            vals["monta_last_sync"] = rec.last_sync
        # If you made a Studio field for delivery date, mirror into it:
        if "x_monta_delivery_date" in can:
            vals["x_monta_delivery_date"] = rec.delivery_date or False
        if vals:
            so.sudo().write(vals)

    @api.model
    def _resolve_and_upsert(self, so):
        if not so or not so.name:
            return False
        data = self._monta_get_order(so.name)
        if not data:
            return False

        vals = self._map_monta_payload(so, data)
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
