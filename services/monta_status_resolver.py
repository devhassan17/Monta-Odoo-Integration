# -*- coding: utf-8 -*-
import time
import requests
from urllib.parse import urljoin

class MontaStatusResolver:
    """
    Freshest status wins:
      1) shipments
      2) orderevents (latest)
      3) orders (header)
    Also supports fast exact: GET /order/{webshoporderid}
    """

    def __init__(self, env):
        self.env = env
        ICP = env["ir.config_parameter"].sudo()
        self.base = (ICP.get_param("monta.base_url") or ICP.get_param("monta.api.base_url") or "").strip()
        self.user = (ICP.get_param("monta.username") or "").strip()
        self.pwd  = (ICP.get_param("monta.password") or "").strip()
        self.timeout = int(ICP.get_param("monta.timeout") or 20)
        self.allow_loose = (ICP.get_param("monta.match_loose") or "1").strip() != "0"

        if not (self.base and self.user and self.pwd):
            raise ValueError("Missing System Parameters: monta.base_url / monta.username / monta.password")
        if not self.base.endswith("/"):
            self.base += "/"

        self.s = requests.Session()
        self.s.auth = (self.user, self.pwd)
        self.s.headers.update({
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        })

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
    def _lower(s):
        return str(s or "").strip().lower()

    @staticmethod
    def _as_list(payload):
        """
        Normalise Monta responses:
          - raw list   -> list
          - paged dict -> Items / items / Data / data / results / Results / value
          - single dict-> wrap as [dict]
          - else       -> []
        """
        if payload is None:
            return []
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for k in ("Items", "items", "Data", "data", "results", "Results", "value"):
                if k in payload and isinstance(payload[k], list):
                    return payload[k]
            return [payload]
        return []

    def _score(self, target, rec):
        """Score record by how well it matches target on common reference fields."""
        t = self._lower(target)
        if not t or not isinstance(rec, dict):
            return (0, "")

        fields = [
            "OrderNumber", "Reference", "ClientReference",
            "WebshopOrderId", "InternalWebshopOrderId",
            "EorderGUID", "EorderGuid",
        ]
        best = (0, "")
        for f in fields:
            s = self._lower(rec.get(f))
            if not s:
                continue
            if s == t:
                sc = 100
            elif self.allow_loose and s.startswith(t):
                sc = 85
            elif self.allow_loose and t in s:
                sc = 70
            else:
                sc = 0
            if sc > best[0]:
                best = (sc, rec.get(f) or "")
                if sc >= 100:
                    break
        return best

    def _pick_best(self, target, payload):
        lst = self._as_list(payload)
        if not lst:
            return None
        threshold = 100 if not self.allow_loose else 60
        best_sc, best_rec = 0, None
        for rec in lst:
            sc, _ = self._score(target, rec)
            if sc > best_sc:
                best_sc, best_rec = sc, rec
                if sc >= 100:
                    break
        return best_rec if best_sc >= threshold else None

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
    def _derive_status_from_flags(o):
        """Map Monta boolean flags to friendly text before any generic fallback."""
        if not isinstance(o, dict):
            return None
        if o.get("IsBlocked"):
            msg = o.get("BlockedMessage")
            return "Blocked" + (f" — {msg}" if msg else "")
        if o.get("IsBackorder"):
            return "Backorder"
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
        for k in ("EstimatedDeliveryTo", "EstimatedDeliveryFrom", "LatestDeliveryDate"):
            if o.get(k):
                return f"In progress — ETA {o[k]}"
        return None

    # ----------- order lookup -----------
    def _find_order(self, order_ref, tried):
        # 0) direct exact endpoint
        tried.append({"direct": f"order/{order_ref}"})
        scd, direct = self._get(f"order/{order_ref}")
        if 200 <= scd < 300 and isinstance(direct, dict) and direct:
            items = self._as_list(direct)
            return items[0] if items and isinstance(items[0], dict) else direct

        # 1..n) search endpoints
        params_list = [
            {"orderNumber": order_ref},
            {"reference": order_ref},
            {"clientReference": order_ref},
            {"webshopOrderId": order_ref},
            {"internalWebshopOrderId": order_ref},
            {"eorderGuid": order_ref},
            {"search": order_ref},
        ]
        for p in params_list:
            tried.append(p.copy())
            sc, payload = self._get("orders", p)
            if not (200 <= sc < 300):
                continue
            cand = self._pick_best(order_ref, payload)
            if cand:
                return cand
        return None

    # ---------------- resolve ----------------
    def resolve(self, order_ref):
        """
        Returns: (status_text, meta) or (None, {'reason': ..., 'tried': [...]})
        """
        tried = []
        cand = self._find_order(order_ref, tried)
        if not cand:
            return None, {"reason": "Order not found or not matching searched reference", "tried": tried}

        # hydrate full order if possible
        if cand.get("Id"):
            scid, full = self._get(f"orders/{cand['Id']}")
            if 200 <= scid < 300 and isinstance(full, dict) and full:
                cand = full

        refs = {
            "orderId": cand.get("Id"),
            "orderNumber": cand.get("OrderNumber") or order_ref,
            "orderReference": cand.get("Reference") or order_ref,
            "clientReference": cand.get("ClientReference") or order_ref,
            "orderGuid": cand.get("EorderGUID") or cand.get("EorderGuid"),
            "webshopOrderId": cand.get("WebshopOrderId") or cand.get("InternalWebshopOrderId"),
        }

        # 1) SHIPMENTS
        ship_status = ship_tt = ship_date = ship_msg = None
        ship_src = None
        for params, lbl in [
            ({"orderId": refs["orderId"]}, "shipments"),
            ({"orderNumber": refs["orderNumber"]}, "shipments"),
            ({"orderReference": refs["orderReference"]}, "shipments"),
            ({"clientReference": refs["clientReference"]}, "shipments"),
            ({"orderGuid": refs["orderGuid"]}, "shipments"),
            ({"webshopOrderId": refs["webshopOrderId"]}, "shipments"),
        ]:
            p = {k: v for k, v in params.items() if v}
            if not p:
                continue
            scS, ships = self._get("shipments", p)
            for sh in self._as_list(ships):
                st = (
                    self._pick(sh, "DeliveryStatusDescription", "ShipmentStatus", "Status", "CurrentStatus")
                    or ("Shipped" if (sh.get("IsShipped") or sh.get("ShippedDate")) else None)
                    or str(sh.get("ShipmentStatus") or "")
                )
                if st:
                    ship_status = st
                    ship_tt = ship_tt or self._pick(sh, "TrackAndTraceLink", "TrackAndTraceUrl", "TrackAndTrace", "TrackingUrl")
                    ship_date = ship_date or self._pick(sh, "DeliveryDate", "ShippedDate", "EstimatedDeliveryTo", "LatestDeliveryDate")
                    ship_msg  = ship_msg  or self._pick(sh, "BlockedMessage", "DeliveryMessage", "Message", "Reason")
                    ship_src = lbl
                    break
            if ship_status:
                break

        # 2) ORDER EVENTS
        event_status = event_msg = event_tt = event_date = None
        event_src = None
        if not ship_status:
            for params, lbl in [
                ({"orderId": refs["orderId"], "limit": 1, "sort": "desc"}, "orderevents"),
                ({"orderNumber": refs["orderNumber"], "limit": 1, "sort": "desc"}, "orderevents"),
                ({"orderReference": refs["orderReference"], "limit": 1, "sort": "desc"}, "orderevents"),
                ({"clientReference": refs["clientReference"], "limit": 1, "sort": "desc"}, "orderevents"),
                ({"orderGuid": refs["orderGuid"], "limit": 1, "sort": "desc"}, "orderevents"),
                ({"webshopOrderId": refs["webshopOrderId"], "limit": 1, "sort": "desc"}, "orderevents"),
            ]:
                p = {k: v for k, v in params.items() if v}
                scE, ev = self._get("orderevents", p)
                lst = self._as_list(ev)
                if lst:
                    e = lst[0]
                    event_status = (
                        self._pick(e, "DeliveryStatusDescription", "Status", "CurrentStatus", "ActionCode")
                        or self._pick(e.get("Order") or {}, "Status", "CurrentStatus")
                        or self._pick(e.get("Shipment") or {}, "ShipmentStatus", "Status", "CurrentStatus")
                    )
                    event_msg  = self._pick(e, "BlockedMessage", "DeliveryMessage", "Message", "Reason")
                    event_tt   = self._pick(e.get("Shipment") or {}, "TrackAndTraceLink", "TrackAndTraceUrl", "TrackAndTrace", "TrackingUrl")
                    event_date = self._pick(e.get("Shipment") or {}, "DeliveryDate", "ShippedDate", "EstimatedDeliveryTo", "LatestDeliveryDate")
                    event_src  = lbl
                    if event_status:
                        break

        # 3) ORDER HEADER
        header_status = (
            self._pick(cand, "DeliveryStatusDescription", "Status", "CurrentStatus")
            or self._derive_status_from_flags(cand)
            or "Received / Pending workflow"
        )
        header_tt   = self._pick(cand, "TrackAndTraceLink", "TrackAndTraceUrl", "TrackAndTrace", "TrackingUrl")
        header_date = self._pick(cand, "DeliveryDate", "ShippedDate", "EstimatedDeliveryTo", "LatestDeliveryDate")
        header_msg  = self._pick(cand, "BlockedMessage", "DeliveryMessage", "Message", "Reason")

        # choose freshest
        src         = ship_src or event_src or "orders"
        status_txt  = ship_status or event_status or header_status
        tt          = ship_tt or event_tt or header_tt
        dd          = ship_date or event_date or header_date
        dm          = ship_msg or event_msg or header_msg

        status_code = self._pick(cand, "StatusID", "DeliveryStatusId", "DeliveryStatusCode")

        stable_ref = (
            refs["orderNumber"] or refs["webshopOrderId"] or refs["orderGuid"]
            or refs["clientReference"] or refs["orderReference"] or order_ref
        )

        meta = {
            "source": src,
            "status_code": status_code,
            "track_trace": tt,
            "delivery_date": dd,
            "delivery_message": dm,
            "monta_order_ref": stable_ref,
        }
        return status_txt, meta
