# -*- coding: utf-8 -*-
import time
import requests
from urllib.parse import urljoin

class MontaStatusResolver:
    def __init__(self, env):
        self.env = env
        ICP = env["ir.config_parameter"].sudo()
        self.base = (ICP.get_param("monta.base_url") or "").strip()
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

    def _get(self, path, params=None):
        params = dict(params or {})
        params["_ts"] = int(time.time())
        url = urljoin(self.base, path.lstrip("/"))
        r = self.s.get(url, params=params, timeout=self.timeout)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, None

    @staticmethod
    def _first(obj):
        if isinstance(obj, list) and obj:
            return obj[0]
        if isinstance(obj, dict):
            return obj
        return None

    @staticmethod
    def _pick_status(d):
        if not isinstance(d, dict):
            return None
        for k in ("DeliveryStatusDescription","DeliveryStatusCode","Status","State",
                  "OrderStatus","ActionCode","ShipmentStatus","CurrentStatus"):
            v = d.get(k)
            if v not in (None, "", []):
                return v
        return None

    @staticmethod
    def _derive_order_status(o):
        if not isinstance(o, dict):
            return None
        if o.get("IsBlocked"):
            msg = o.get("BlockedMessage")
            return "Blocked" + (f" — {msg}" if msg else "")
        if o.get("IsShipped"):
            st = "Shipped"
            if o.get("TrackAndTraceCode"): st += f" (T&T: {o['TrackAndTraceCode']})"
            if o.get("ShippedDate"):       st += f" on {o['ShippedDate']}"
            return st
        if o.get("Picked"): return "Picked"
        if o.get("IsPicking"): return "Picking in progress"
        if o.get("ReadyToPick") not in (None, "", "NotReady"): return "Ready to pick"
        if o.get("IsBackorder"): return "Backorder"
        for k in ("EstimatedDeliveryTo","EstimatedDeliveryFrom","LatestDeliveryDate"):
            if o.get(k): return f"In progress — ETA {o[k]}"
        if o.get("StatusID") is not None:         return f"StatusID={o['StatusID']}"
        if o.get("DeliveryStatusId") is not None: return f"DeliveryStatusId={o['DeliveryStatusId']}"
        return "Received / Pending workflow"

    @staticmethod
    def _pick_delivery_date(d):
        if not isinstance(d, dict):
            return None
        for k in ("ShippedDate", "EstimatedDeliveryTo", "LatestDeliveryDate"):
            v = d.get(k)
            if v:
                return v
        return None

    @staticmethod
    def _pick_track_trace(d):
        if not isinstance(d, dict):
            return None
        for k in ("TrackAndTraceLink", "TrackAndTraceUrl", "TrackAndTrace", "TrackingUrl"):
            v = d.get(k)
            if v:
                return v
        return None

    def resolve(self, order_ref):
        sc, orders = self._get("orders", {"search": order_ref})
        if not (200 <= sc < 300 and isinstance(orders, list) and orders):
            sc2, orders2 = self._get("orders", {"clientReference": order_ref})
            orders = orders2 if (200 <= sc2 < 300) else None
        o = self._first(orders) if orders else None
        if not o:
            return None, {"reason": "Order not found"}

        if o.get("Id"):
            scid, o2 = self._get(f"orders/{o['Id']}")
            if 200 <= scid < 300 and isinstance(o2, dict):
                o = o2

        refs = {
            "orderId": o.get("Id"),
            "orderReference": o.get("Reference") or order_ref,
            "clientReference": o.get("ClientReference") or order_ref,
            "orderGuid": o.get("EorderGUID") or o.get("EorderGuid"),
            "webshopOrderId": o.get("WebshopOrderId") or o.get("InternalWebshopOrderId"),
        }

        ship_status = ship_tt = ship_date = ship_src = None
        for params, lbl in [
            ({"orderId": refs["orderId"]}, "shipments?orderId"),
            ({"orderReference": refs["orderReference"]}, "shipments?orderReference"),
            ({"clientReference": refs["clientReference"]}, "shipments?clientReference"),
            ({"orderGuid": refs["orderGuid"]}, "shipments?orderGuid"),
            ({"webshopOrderId": refs["webshopOrderId"]}, "shipments?webshopOrderId"),
        ]:
            params = {k: v for k, v in params.items() if v}
            if not params:
                continue
            scS, ships = self._get("shipments", params)
            if 200 <= scS < 300 and isinstance(ships, list) and ships:
                for sh in ships:
                    st = (self._pick_status(sh) or
                          ("Shipped" if (sh.get("IsShipped") or sh.get("ShippedDate")) else None) or
                          str(sh.get("ShipmentStatus") or ""))
                    st = "Shipped" if st is True else (st or None)
                    if st:
                        ship_status = st
                        ship_tt = ship_tt or self._pick_track_trace(sh)
                        ship_date = ship_date or self._pick_delivery_date(sh)
                        ship_src = lbl
                        break
            if ship_status:
                break

        event_status = event_src = None
        if not ship_status:
            for params, lbl in [
                ({"orderId": refs["orderId"], "limit": 1, "sort": "desc"}, "orderevents?orderId"),
                ({"orderReference": refs["orderReference"], "limit": 1, "sort": "desc"}, "orderevents?orderReference"),
                ({"clientReference": refs["clientReference"], "limit": 1, "sort": "desc"}, "orderevents?clientReference"),
                ({"orderGuid": refs["orderGuid"], "limit": 1, "sort": "desc"}, "orderevents?orderGuid"),
                ({"webshopOrderId": refs["webshopOrderId"], "limit": 1, "sort": "desc"}, "orderevents?webshopOrderId"),
            ]:
                params = {k: v for k, v in params.items() if v}
                scE, ev = self._get("orderevents", params)
                if 200 <= scE < 300 and isinstance(ev, list) and ev:
                    e = ev[0]
                    st = (self._pick_status(e) or
                          self._pick_status(e.get("Order") or {}) or
                          self._pick_status(e.get("Shipment") or {}) or
                          (f"Event: {e.get('ActionCode')}" if e.get("ActionCode") else None))
                    if st:
                        event_status = st
                        ship_tt = ship_tt or self._pick_track_trace(e.get("Shipment") or {})
                        ship_date = ship_date or self._pick_delivery_date(e.get("Shipment") or {})
                        event_src = lbl
                        break

        order_status = self._pick_status(o) or self._derive_order_status(o)
        order_tt = self._pick_track_trace(o)
        order_date = self._pick_delivery_date(o)

        meta = {
            "source": ship_src or event_src or "orders",
            "code": o.get("StatusID") or o.get("DeliveryStatusId"),
            "track_trace": ship_tt or order_tt,
            "delivery_date": ship_date or order_date,
        }
        if ship_status:
            return ship_status, meta
        if event_status:
            return event_status, meta
        return order_status, meta
