# -*- coding: utf-8 -*-
"""
Extensions for monta.order.status (no field re-declarations).
Keep lightweight helpers in case you call them from elsewhere.
"""
import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class MontaOrderStatus(models.Model):
    _inherit = "monta.order.status"   # <-- do NOT redeclare the model

    @staticmethod
    def _first(obj):
        if isinstance(obj, list) and obj:
            return obj[0]
        if isinstance(obj, dict):
            return obj
        return None

    @api.model
    def _map_monta_payload(self, so, data: dict) -> dict:
        """(Kept for compatibility if you use MontaHttp elsewhere)"""
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
        delivery_msg = (
            data.get("DeliveryMessage")
            or data.get("Message")
            or data.get("Remark")
            or ""
        )
        delivery = (
            data.get("DeliveryDate")
            or data.get("EstimatedDeliveryTo")
            or data.get("LatestDeliveryDate")
            or None
        )
        monta_ref = (
            data.get("WebshopOrderId")
            or data.get("ClientReference")
            or data.get("Reference")
            or so.name
        )
        try:
            delivery = fields.Date.to_date(delivery) if delivery else None
        except Exception:
            delivery = None

        return {
            "order_name": so.name,
            "sale_order_id": so.id,
            "status": status_txt or False,
            "status_code": str(code) if code not in (None, "") else False,
            "source": "orders",
            "delivery_message": delivery_msg or False,
            "monta_order_ref": monta_ref or False,
            "track_trace": tnt or False,
            "delivery_date": delivery or False,
            "last_sync": fields.Datetime.now(),
        }
