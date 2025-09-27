# -*- coding: utf-8 -*-
import time
import requests
from urllib.parse import urljoin


class MontaStatusResolver:
    """
    Resolver with safe-but-flexible matching and freshest status:
      1) Find the correct order (best-match across ref fields, case-insensitive).
      2) Hydrate by /orders/<Id>.
      3) Try SHIPMENTS first, then ORDER EVENTS, finally ORDER HEADER.
      4) Return normalized (status_text, meta) where meta contains:
         - source: "shipments"|"orderevents"|"orders"
         - status_code, track_trace, delivery_date, delivery_message
         - monta_order_ref (stable reference for UI)
    """

    def __init__(self, env):
        self.env = env
        ICP = env["ir.config_parameter"].sudo()
        self.base = (ICP.get_param("monta.base_url") or ICP.get_param("monta.api.base_url") or "").strip()
        self.user = (ICP.get_param("monta.username") or "").strip()
        self.pwd  = (ICP.get_param("monta.password") or "").strip()
        self.timeout = int(ICP.get_param("monta.timeout") or 20)
        # Set system param monta.match_loose=0 to require exact matches only
        self.allow_loose = (ICP.get_param("monta.match_loose") or "1").strip() != "0"

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

    # ------------- match helpers -------------
    @staticmethod
    def _lower(s):
        return str(s or "").strip().lower()

    def _match_score(self, target, rec):
        """
        Score how well 'rec' matches 'target' across key reference fields.
        """
        t = self._lower(target)
        if not t or not isinstance(rec, dict):
            return (0, "")

        fields = ["Reference", "ClientReference", "WebshopOrderId",
                  "InternalWebshopOrderId", "EorderGUID", "EorderGuid"]
        best = (0, "")
        for f in fields:
            s = self._lower(rec.get(f))
            if not s:
                continue
            if s == t:
                sc = 100
            elif self.allow_loose and s.startswith(t):
                sc = 80
            elif self.allow_loose and t in s:
                sc = 60
            else:
                sc = 0
            if sc > best[0]:
                best = (sc, rec.get(f) or "")
                if sc >= 100:
                    break
        return best

    def _best_candidate(self, order_ref, lst):
        if not isinstance(lst, list) or not lst:
            return None
        threshold = 100 if not self.allow_loose else 60
        winner = (0, None)
        for rec in lst:
            sc, _ = self._match_score(order_ref, rec)
            if sc > winner[0]:
                winner = (sc, rec)
                if sc >= 100:
                    break
        return winner[1] if winner[0] >= threshold else None

    # ------------- field pickers -------------
    @staticmethod
    def _pick(d, *keys):
        if not isinstance(d, dict):
            return None
        for k in keys:
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
        if o.get("IsShipped") or o.get("ShippedDate"):
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
        if o.get("ReadyToPick") and o.get("ReadyToPick") != "NotReady":
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
        Returns: (status_text, meta) or (None, {'reason': ...})
        """
        # 1) Search and choose the right order
        sc, found = self._get("orders", {"search": order_ref})
        cand = self._best_candidate(order_ref, found) if (200 <= sc < 300) else None

        if not cand:
            # fallbacks
            for params in ({"clientReference": order_ref}, {"webshopOrderId": order_ref}):
                sc2, lst2 = self._get("orders", params)
                if 200 <= sc2 < 300 and isinstance(lst2, list) and lst2:
                    cand = self._best_candidate(order_ref, lst2)
                    if cand:
                        break

        if not cand:
            return None, {"reason": "Order not found or not matching searched reference"}

        # hydrate by Id
        if cand.get("Id"):
            scid, full = self._get(f"orders/{cand['Id']}")
            if 200 <= scid < 300 and isinstance(full, dict) and full:
                cand = full

        # stable identifiers for downstream queries
        refs = {
            "orderId": cand.get("Id"),
            "orderReference": cand.get("Reference") or order_ref,
            "clientReference": cand.get("ClientReference") or order_ref,
            "orderGuid": cand.get("EorderGUID") or cand.get("EorderGuid"),
            "webshopOrderId": cand.get("WebshopOrderId") or cand.get("InternalWebshopOrderId") or order_ref,
        }

        # 2) Try SHIPMENTS first (freshest)
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
                    st = (self._pick(sh, "DeliveryStatusDescription", "ShipmentStatus", "Status", "CurrentStatus")
                          or ("Shipped" if (sh.get("IsShipped") or sh.get("ShippedDate")) else None)
                          or str(sh.get("ShipmentStatus") or ""))
                    if st:
                        ship_status = st
                        ship_tt = ship_tt or self._pick(sh, "TrackAndTraceLink", "TrackAndTraceUrl", "TrackAndTrace", "TrackingUrl")
                        ship_date = ship_date or self._pick(sh, "DeliveryDate", "ShippedDate", "EstimatedDeliveryTo", "LatestDeliveryDate")
                        ship_msg  = ship_msg  or self._pick(sh, "BlockedMessage", "DeliveryMessage", "Message", "Reason")
                        ship_src = lbl
                        break
            if ship_status:
                break

        # 3) If no shipment status, try ORDER EVENTS (latest one)
        event_status = event_msg = event_tt = event_date = event_src = None
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
                    event_status = (
                        self._pick(e, "DeliveryStatusDescription", "Status", "CurrentStatus", "ActionCode")
                        or self._pick(e.get("Order") or {}, "Status", "CurrentStatus")
                        or self._pick(e.get("Shipment") or {}, "ShipmentStatus", "Status", "CurrentStatus")
                    )
                    event_msg = self._pick(e, "BlockedMessage", "DeliveryMessage", "Message", "Reason")
                    event_tt = self._pick(e.get("Shipment") or {}, "TrackAndTraceLink", "TrackAndTraceUrl", "TrackAndTrace", "TrackingUrl")
                    event_date = self._pick(e.get("Shipment") or {}, "DeliveryDate", "ShippedDate", "EstimatedDeliveryTo", "LatestDeliveryDate")
                    event_src = lbl
                    if event_status:
                        break

        # 4) Order HEADER last
        order_status = (
            self._pick(cand, "DeliveryStatusDescription", "Status", "CurrentStatus") or self._derive_order_status(cand)
        )
        order_tt = self._pick(cand, "TrackAndTraceLink", "TrackAndTraceUrl", "TrackAndTrace", "TrackingUrl")
        order_date = self._pick(cand, "DeliveryDate", "ShippedDate", "EstimatedDeliveryTo", "LatestDeliveryDate")
        order_msg = self._pick(cand, "BlockedMessage", "DeliveryMessage", "Message", "Reason")

        # 5) Choose the freshest
        src = ship_src or event_src or "orders"
        status_txt = ship_status or event_status or order_status
        tt = ship_tt or event_tt or order_tt
        dd = ship_date or event_date or order_date
        dm = ship_msg or event_msg or order_msg

        status_code = (
            self._pick(cand, "StatusID", "DeliveryStatusId", "DeliveryStatusCode")  # header codes (often fill correctly)
        )

        meta = {
            "source": src,
            "status_code": status_code,
            "track_trace": tt,
            "delivery_date": dd,
            "delivery_message": dm,
            "monta_order_ref": (
                refs["webshopOrderId"] or refs["orderGuid"] or refs["clientReference"] or refs["orderReference"] or order_ref
            ),
        }
        return status_txt, meta
