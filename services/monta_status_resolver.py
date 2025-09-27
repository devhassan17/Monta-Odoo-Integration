# -*- coding: utf-8 -*-
import time
import requests
from urllib.parse import urljoin


class MontaStatusResolver:
    """
    Strict resolver:
      - Searches order by multiple keys.
      - Accepts a result ONLY if it matches the searched ref on at least one of:
        Reference, ClientReference, WebshopOrderId, InternalWebshopOrderId, EorderGUID.
      - If not matched, returns (None, {'reason': 'Order not found'}).
    """

    def __init__(self, env):
        self.env = env
        ICP = env["ir.config_parameter"].sudo()
        self.base = (ICP.get_param("monta.base_url") or ICP.get_param("monta.api.base_url") or "").strip()
        self.user = (ICP.get_param("monta.username") or "").strip()
        self.pwd  = (ICP.get_param("monta.password") or "").strip()
        self.timeout = int(ICP.get_param("monta.timeout") or 20)

        if not (self.base and self.user and self.pwd):
            raise ValueError("Missing System Parameters: monta.base_url / monta.username / monta.password")
        if not self.base.endswith("/"):
            self.base += "/"

        self.s = requests.Session()
        self.s.auth = (self.user, self.pwd)
        self.s.headers.update({"Accept": "application/json", "Cache-Control": "no-cache", "Pragma": "no-cache"})

    # --------------- HTTP ---------------
    def _get(self, path, params=None):
        params = dict(params or {})
        params["_ts"] = int(time.time())
        url = urljoin(self.base, path.lstrip("/"))
        r = self.s.get(url, params=params, timeout=self.timeout)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, None

    # --------------- helpers ---------------
    @staticmethod
    def _first(obj):
        if isinstance(obj, list) and obj:
            return obj[0]
        if isinstance(obj, dict):
            return obj
        return None

    @staticmethod
    def _matches_ref(order_ref, d):
        """Ensure the remote order actually belongs to this reference."""
        if not isinstance(d, dict):
            return False
        cand = {
            str(d.get("Reference") or ""),
            str(d.get("ClientReference") or ""),
            str(d.get("WebshopOrderId") or ""),
            str(d.get("InternalWebshopOrderId") or ""),
            str(d.get("EorderGUID") or d.get("EorderGuid") or ""),
        }
        order_ref = str(order_ref or "").strip()
        return order_ref and (order_ref in cand)

    @staticmethod
    def _pick(d, *keys):
        if not isinstance(d, dict):
            return None
        for k in keys:
            v = d.get(k)
            if v not in (None, "", []):
                return v
        return None

    # --------------- resolve ---------------
    def resolve(self, order_ref):
        """
        Returns: (status_text, meta) or (None, {'reason': ...})
        """
        def find_order():
            # query in priority
            for params in (
                {"search": order_ref},
                {"clientReference": order_ref},
                {"webshopOrderId": order_ref},
            ):
                sc, data = self._get("orders", params)
                if 200 <= sc < 300 and isinstance(data, list) and data:
                    o = self._first(data)
                    # hydrate
                    if isinstance(o, dict) and o.get("Id"):
                        sc2, full = self._get(f"orders/{o['Id']}")
                        if 200 <= sc2 < 300 and isinstance(full, dict) and full:
                            o = full
                    return o
            return None

        o = find_order()
        if not o or not self._matches_ref(order_ref, o):
            return None, {"reason": "Order not found or not matching searched reference"}

        # status, track&trace, delivery, message
        status_txt = (
            self._pick(o, "DeliveryStatusDescription", "Status", "CurrentStatus")
            or ("Shipped" if (o.get("IsShipped") or o.get("ShippedDate")) else None)
            or "Received / Pending workflow"
        )
        status_code = self._pick(o, "StatusID", "DeliveryStatusId", "DeliveryStatusCode")
        track = self._pick(o, "TrackAndTraceLink", "TrackAndTraceUrl", "TrackAndTrace", "TrackingUrl")
        delivery = self._pick(o, "DeliveryDate", "ShippedDate", "EstimatedDeliveryTo", "LatestDeliveryDate")
        message = self._pick(o, "BlockedMessage", "DeliveryMessage", "Message", "Reason")

        # normalized meta
        meta = {
            "source": "orders",
            "status_code": status_code,
            "track_trace": track,
            "delivery_date": delivery,
            "delivery_message": message,
            "monta_order_ref": (
                o.get("WebshopOrderId")
                or o.get("InternalWebshopOrderId")
                or o.get("ClientReference")
                or o.get("Reference")
                or order_ref
            ),
        }
        return status_txt, meta
