# -*- coding: utf-8 -*-
import time
import requests
from urllib.parse import urljoin


class MontaStatusResolver:
    """
    Read-only resolver that:
      1) finds an order via /orders?search=<ref> (fallbacks included),
      2) hydrates with /orders/<Id> when available,
      3) tries shipments and orderevents for fresher status,
      4) returns a normalized snapshot dict to store.
    No writes to sale.order here.
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

    # --------------- pickers ---------------
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
        for k in ("DeliveryStatusDescription", "DeliveryStatusCode", "Status", "State",
                  "OrderStatus", "ActionCode", "ShipmentStatus", "CurrentStatus"):
            v = d.get(k)
            if v not in (None, "", []):
                return v
        return None

    @staticmethod
    def _pick_delivery_message(d):
        """Human message when blocked / delayed etc."""
        if not isinstance(d, dict):
            return None
        for k in ("BlockedMessage", "DeliveryMessage", "Message", "Reason"):
            v = d.get(k)
            if v:
                return v
        return None

    @staticmethod
    def _pick_delivery_date(d):
        if not isinstance(d, dict):
            return None
        for k in ("DeliveryDate", "ShippedDate", "EstimatedDeliveryTo", "LatestDeliveryDate"):
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

    @staticmethod
    def _derive_order_status(o):
        if not isinstance(o, dict):
            return None
        if o.get("IsBlocked"):
            msg = o.get("BlockedMessage")
            return "Blocked" + (f" — {msg}" if msg else "")
        if o.get("IsShipped"):
            st = "Shipped"
            if o.get("TrackAndTraceCode"):
                st += f" (T&T: {o['TrackAndTraceCode']})"
            if o.get("ShippedDate"):
                st += f" on {o['ShippedDate']}"
            return st
        if o.get("Picked"):
            return "Picked"
        if o.get("IsPicking"):
            return "Picking in progress"
        if o.get("ReadyToPick") not in (None, "", "NotReady"):
            return "Ready to pick"
        if o.get("IsBackorder"):
            return "Backorder"
        for k in ("EstimatedDeliveryTo", "EstimatedDeliveryFrom", "LatestDeliveryDate"):
            if o.get(k):
                return f"In progress — ETA {o[k]}"
        if o.get("StatusID") is not None:
            return f"StatusID={o['StatusID']}"
        if o.get("DeliveryStatusId") is not None:
            return f"DeliveryStatusId={o['DeliveryStatusId']}"
        return "Received / Pending workflow"

    # --------------- resolve ---------------
    def resolve(self, order_ref):
        """
        Returns: (status_text, meta)
          meta:
            {
              "source": "shipments"|"orderevents"|"orders",
              "status_code": ...,
              "track_trace": ...,
              "delivery_date": ...,
              "delivery_message": ...,
              "monta_order_ref": ...,   # WebshopOrderId/EorderGuid when available
            }
        """
        # Find order by search/clientReference/webshopOrderId, then hydrate by Id
        sc, orders = self._get("orders", {"search": order_ref})
        if not (200 <= sc < 300 and isinstance(orders, list) and orders):
            sc2, orders2 = self._get("orders", {"clientReference": order_ref})
            orders = orders2 if (200 <= sc2 < 300 and isinstance(orders2, list) and orders2) else None
        if not orders:
            sc3, orders3 = self._get("orders", {"webshopOrderId": order_ref})
            orders = orders3 if (200 <= sc3 < 300 and isinstance(orders3, list) and orders3) else None

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
            "webshopOrderId": o.get("WebshopOrderId") or o.get("InternalWebshopOrderId") or order_ref,
        }

        # Prefer freshest info from shipments, then events, finally order header
        ship_status = ship_tt = ship_date = ship_msg = ship_src = None
        for params, lbl in [
            ({"orderId": refs["orderId"]}, "shipments"),
            ({"orderReference": refs["orderReference"]}, "shipments"),
            ({"clientReference": refs["clientReference"]}, "shipments"),
            ({"orderGuid": refs["orderGuid"]}, "shipments"),
            ({"webshopOrderId": refs["webshopOrderId"]}, "shipments"),
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
                        ship_msg  = ship_msg  or self._pick_delivery_message(sh)
                        ship_src = lbl
                        break
            if ship_status:
                break

        event_status = event_msg = event_src = None
        if not ship_status:
            for params, lbl in [
                ({"orderId": refs["orderId"], "limit": 1, "sort": "desc"}, "orderevents"),
                ({"orderReference": refs["orderReference"], "limit": 1, "sort": "desc"}, "orderevents"),
                ({"clientReference": refs["clientReference"], "limit": 1, "sort": "desc"}, "orderevents"),
                ({"orderGuid": refs["orderGuid"], "limit": 1, "sort": "desc"}, "orderevents"),
                ({"webshopOrderId": refs["webshopOrderId"], "limit": 1, "sort": "desc"}, "orderevents"),
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
                        event_msg = self._pick_delivery_message(e) or self._pick_delivery_message(e.get("Shipment") or {})
                        ship_tt = ship_tt or self._pick_track_trace(e.get("Shipment") or {})
                        ship_date = ship_date or self._pick_delivery_date(e.get("Shipment") or {})
                        event_src = lbl
                        break

        order_status = self._pick_status(o) or self._derive_order_status(o)
        order_tt = self._pick_track_trace(o)
        order_date = self._pick_delivery_date(o)
        order_msg = self._pick_delivery_message(o)

        # Build normalized snapshot
        src = ship_src or event_src or "orders"
        status_txt = ship_status or event_status or order_status
        tt = ship_tt or order_tt
        dd = ship_date or order_date
        dm = ship_msg or event_msg or order_msg

        meta = {
            "source": src,
            "status_code": o.get("StatusID") or o.get("DeliveryStatusId") or o.get("DeliveryStatusCode"),
            "track_trace": tt,
            "delivery_date": dd,
            "delivery_message": dm,
            "monta_order_ref": refs["webshopOrderId"] or refs["orderGuid"] or refs["orderReference"],
        }
        return status_txt, meta