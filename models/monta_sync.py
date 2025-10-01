# -*- coding: utf-8 -*-
import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class MontaOrderStatus(models.Model):
    _inherit = "monta.order.status"

    # ---------------- helpers ----------------

    @staticmethod
    def _first(obj):
        if isinstance(obj, list) and obj:
            return obj[0]
        if isinstance(obj, dict) and obj:
            return obj
        return None

    @staticmethod
    def _lower(s):
        return str(s or "").strip().lower()

    def _best_match(self, target, candidates):
        t = self._lower(target)
        if isinstance(candidates, dict):
            c = candidates
            vals = [c]
        else:
            vals = candidates or []
        best = None
        best_sc = 0
        for r in vals:
            sc = 0
            for k in ("OrderNumber", "Reference", "ClientReference",
                      "WebshopOrderId", "InternalWebshopOrderId", "EorderGUID", "EorderGuid"):
                v = self._lower((r or {}).get(k))
                if not v:
                    continue
                if v == t:
                    sc = 100
                elif v.startswith(t):
                    sc = max(sc, 85)
                elif t in v:
                    sc = max(sc, 70)
            if sc > best_sc:
                best_sc, best = sc, r
                if sc >= 100:
                    break
        # minimal threshold for loose match
        return best if best_sc >= 60 else None

    def _monta_get_order(self, name: str) -> dict:
        http = self.env["monta.http"].sudo()
        # primary narrow queries
        tries = (
            {"orderNumber": name},
            {"reference": name},
            {"clientReference": name},
            {"webshopOrderId": name},
            {"internalWebshopOrderId": name},
            {"eorderGuid": name},
            {"orderNo": name},           # extra aliases some tenants expose
            {"order_no": name},
            {"customerReference": name},
            {"search": name},
        )
        for params in tries:
            data = http.get_json("orders", params=params)
            if not data:
                continue
            match = self._best_match(name, data)
            if match:
                full_id = (match or {}).get("Id")
                if full_id:
                    full = http.get_json(f"orders/{full_id}")
                    if isinstance(full, dict) and full:
                        return full
                return match

        # FINAL fallback: pull latest 250 orders and match locally
        recent = http.get_json("orders", params={"limit": 250, "sort": "desc"})
        match = self._best_match(name, recent)
        if match:
            full_id = match.get("Id")
            if full_id:
                full = http.get_json(f"orders/{full_id}")
                if isinstance(full, dict) and full:
                    return full
            return match

        _logger.info("[Monta] No payload for order %s (after extended search)", name)
        return {}

    @api.model
    def _map_monta_payload(self, so, data: dict) -> dict:
        status_txt = (
            data.get("DeliveryStatusDescription")
            or data.get("Status")
            or data.get("CurrentStatus")
            or ""
        )
        code = data.get("StatusID") or data.get("DeliveryStatusCode") or data.get("DeliveryStatusId") or ""
        tnt = (
            data.get("TrackAndTraceLink")
            or data.get("TrackAndTraceUrl")
            or data.get("TrackAndTrace")
            or data.get("TrackingUrl")
            or ""
        )
        delivery_msg = (
            data.get("DeliveryMessage")
            or data.get("Message")
            or data.get("Remark")
            or data.get("Reason")
            or ""
        )
        delivery = (
            data.get("DeliveryDate")
            or data.get("ShippedDate")
            or data.get("EstimatedDeliveryTo")
            or data.get("LatestDeliveryDate")
            or None
        )
        monta_ref = (
            data.get("OrderNumber")
            or data.get("WebshopOrderId")
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
