# -*- coding: utf-8 -*-
import time
import requests
from urllib.parse import urljoin
from odoo import fields


class MontaStatusResolver:
    """
    Read-only resolver that:
      1) finds an order via /orders?search=<ref> (with precise matching),
      2) hydrates with /orders/<Id> when available,
      3) tries shipments and orderevents for fresher status,
      4) returns a normalized (status_text, meta) tuple.

    meta = {
        "source": "shipments" | "events" | "orders",
        "status_code": str | None,
        "track_trace": str | None,
        "delivery_date": date | None,
        "delivery_message": str | None,
        "monta_order_ref": str | None,
    }
    """

    def __init__(self, env):
        self.env = env
        ICP = env["ir.config_parameter"].sudo()
        self.base = (ICP.get_param("monta.base_url") or ICP.get_param("monta.api.base_url") or "").strip()
        self.user = (ICP.get_param("monta.username") or "").strip()
        self.pwd = (ICP.get_param("monta.password") or "").strip()
        self.timeout = int(ICP.get_param("monta.timeout") or 20)
        if not (self.base and self.user and self.pwd):
            raise ValueError("Missing System Parameters: monta.base_url / monta.username / monta.password")
        if not self.base.endswith("/"):
            self.base += "/"

        self.s = requests.Session()
        self.s.auth = (self.user, self.pwd)
        self.s.headers.update({"Accept": "application/json", "Cache-Control": "no-cache", "Pragma": "no-cache"})

    # ---------------- HTTP ----------------
    def _get(self, path, params=None):
        params = dict(params or {})
        params["_ts"] = int(time.time())
        url = urljoin(self.base, path.lstrip("/"))
        r = self.s.get(url, params=params, timeout=self.timeout)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, None

    # ---------------- helpers ----------------
    @staticmethod
    def _first(obj):
        if isinstance(obj, list) and obj:
            return obj[0]
        if isinstance(obj, dict):
            return obj
        return None

    @staticmethod
    def _match_order(candidates, needle):
        """
        Prefer an EXACT match on common reference fields; otherwise fallback to first.
        This avoids grabbing the wrong order when 'search' returns multiple rows.
        """
        if not isinstance(candidates, list):
            return None
        keys = ["Reference", "ClientReference", "WebshopOrderId", "InternalWebshopOrderId"]
        for row in candidates:
            if not isinstance(row, dict):
                continue
            for k in keys:
                if (row.get(k) or "") == needle:
                    return row
        return candidates[0] if candidates else None

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
        if not isinstance(d, dict):
            return None
        for k in ("BlockedMessage", "DeliveryMessage", "Message", "Reason", "Remark"):
            v = d.get(k)
            if v:
                return v
        return None

    @staticmethod
    def _pick_delivery_date(d):
        if not isinstance(d, dict):
            return None
        for k in ("DeliveryDate", "ShippedDate", "EstimatedDeliveryTo", "LatestDeliveryDate", "ETA", "Eta"):
            v = d.get(k)
            if v:
                return v
        return None

    @staticmethod
    def _pick_track_trace(d):
        if not isinstance(d, dict):
            return None
        for k in ("TrackAndTraceLink", "TrackAndTraceUrl", "TrackAndTrace", "TrackingUrl", "trackTrace"):
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

    # ---------------- resolve ----------------
    def resolve(self, order_ref):
        """
        Resolve one order by reference and return (status_text, meta).
        """
        # 1) Find order using search variants and choose the best match
        sc, orders = self._get("orders", {"search": order_ref})
        if 200 <= sc < 300 and isinstance(orders, list) and orders:
            o = self._match_order(orders, order_ref)
        else:
            o = None

        if not o:
            for key in ("clientReference", "webshopOrderId"):
                scx, ox = self._get("orders", {key: order_ref})
                if 200 <= scx < 300 and isinstance(ox, list) and ox:
                    o = self._match_order(ox, order_ref)
                    break

        if not o:
            return None, {"reason": "Order not found"}

        # hydrate by Id if available
        if o.get("Id"):
            scid, full = self._get(f"orders/{o['Id']}")
            if 200 <= scid < 300 and isinstance(full, dict):
                o = full

        refs = {
            "orderId": o.get("Id"),
            "orderReference": o.get("Reference") or order_ref,
            "clientReference": o.get("ClientReference") or order_ref,
            "orderGuid": o.get("EorderGUID") or o.get("EorderGuid"),
            "webshopOrderId": o.get("WebshopOrderId") or o.get("InternalWebshopOrderId"),
        }

        # 2) Prefer freshest info from shipments, then events, finally order header
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
                        ship_msg = ship_msg or self._pick_delivery_message(sh)
                        ship_src = lbl
                        break
            if ship_status:
                break

        event_status = event_msg = event_src = None
        if not ship_status:
            for params, lbl in [
                ({"orderId": refs["orderId"], "limit": 1, "sort": "desc"}, "events"),
                ({"orderReference": refs["orderReference"], "limit": 1, "sort": "desc"}, "events"),
                ({"clientReference": refs["clientReference"], "limit": 1, "sort": "desc"}, "events"),
                ({"orderGuid": refs["orderGuid"], "limit": 1, "sort": "desc"}, "events"),
                ({"webshopOrderId": refs["webshopOrderId"], "limit": 1, "sort": "desc"}, "events"),
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
        dd_raw = ship_date or order_date
        dm = ship_msg or event_msg or order_msg

        # normalize date to date object when possible
        try:
            dd = fields.Date.to_date(dd_raw) if dd_raw else None
        except Exception:
            dd = None

        monta_ref = (
            refs.get("webshopOrderId")
            or refs.get("orderGuid")
            or refs.get("orderReference")
            or refs.get("clientReference")
            or order_ref
        )

        meta = {
            "source": src,                           # <- 'events' not 'orderevents'
            "status_code": (o.get("StatusID") or o.get("DeliveryStatusId") or o.get("DeliveryStatusCode")),
            "track_trace": tt,
            "delivery_date": dd,
            "delivery_message": dm,
            "monta_order_ref": monta_ref,
        }
        return status_txt, meta
