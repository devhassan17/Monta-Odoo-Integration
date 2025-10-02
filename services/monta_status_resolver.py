# -*- coding: utf-8 -*-
import json
import time
import requests
from urllib.parse import urljoin

import logging
_logger = logging.getLogger(__name__)

class MontaStatusResolver:
    """
    Freshest status wins (shipments → orderevents → orders).
    Prioritises flags: Blocked > Backorder > Shipped > Picked > ReadyToPick.
    Falls back to Monta text. Supports fast GET /order/{webshoporderid}.
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
            data = r.json()
        except Exception:
            data = None
        _logger.debug("[Monta] GET %s params=%s -> %s", url, params, r.status_code)
        return r.status_code, data

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

    # ---------- status builders ----------
    @staticmethod
    def _status_from_flags(o):
        """
        Strict flag mapping with priority:
        Blocked > Backorder > Shipped > Picked > Picking > ReadyToPick > ETA.
        """
        if not isinstance(o, dict):
            return None

        # 1) Blocked — highest priority
        if o.get("IsBlocked"):
            msg = o.get("BlockedMessage")
            return "Blocked" + (f" — {msg}" if msg else "")

        # 2) Backorder — only if NOT blocked
        if (
            o.get("IsBackorder") or o.get("IsBackOrder")
            or str(o.get("Backorder", "")).lower() in ("1", "true", "yes")
        ):
            return "Backorder"

        # 3) Shipped
        if o.get("IsShipped") or o.get("ShippedDate"):
            st = "Shipped"
            if o.get("TrackAndTraceCode"):
                st += f" (T&T: {o['TrackAndTraceCode']})"
            if o.get("ShippedDate"):
                st += f" on {o['ShippedDate']}"
            return st

        # 4) Warehouse flow
        if o.get("Picked"):
            return "Picked"
        if o.get("IsPicking"):
            return "Picking in progress"
        if o.get("ReadyToPick") and o.get("ReadyToPick") != "NotReady":
            return "Ready to pick"

        # 5) ETA-ish flags
        for k in ("EstimatedDeliveryTo", "EstimatedDeliveryFrom", "LatestDeliveryDate"):
            if o.get(k):
                return f"In progress — ETA {o[k]}"
        return None

    @staticmethod
    def _status_from_text(o):
        """
        Fallback textual parsing if flags are missing.
        If text contains 'blocked' or 'backorder', reflect that explicitly.
        """
        txt = ""
        for k in ("DeliveryStatusDescription", "Status", "CurrentStatus"):
            if o.get(k):
                txt = str(o[k])
                break

        low = txt.lower()
        if "blocked" in low:
            return "Blocked"
        if "backorder" in low or "back order" in low:
            return "Backorder"
        return txt or None

    # ----------- order lookup -----------
    def _find_order(self, order_ref, tried):
        # 0) direct exact endpoint
        tried.append({"direct": f"order/{order_ref}"})
        scd, direct = self._get(f"order/{order_ref}")
        if 200 <= scd < 300 and isinstance(direct, dict) and direct:
            items = self._as_list(direct)
            _logger.debug("[Monta] direct order hit for %s", order_ref)
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
                _logger.debug("[Monta] matched %s via %s", order_ref, p)
                return cand
        _logger.info("[Monta] No order found for %s (tried=%s)", order_ref, tried)
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

        # 1) SHIPMENTS (freshest)
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
                _logger.debug("[Monta] %s using shipment status '%s'", order_ref, ship_status)
                break

        # 2) ORDER EVENTS (if no shipment status)
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
                        _logger.debug("[Monta] %s using event status '%s'", order_ref, event_status)
                        break

        # 3) ORDER HEADER (flags FIRST, then text)
        header_flag = self._status_from_flags(cand)
        header_txt  = self._status_from_text(cand)
        header_status = header_flag or header_txt or "Received / Pending workflow"
        header_tt   = self._pick(cand, "TrackAndTraceLink", "TrackAndTraceUrl", "TrackAndTrace", "TrackingUrl")
        header_date = self._pick(cand, "DeliveryDate", "ShippedDate", "EstimatedDeliveryTo", "LatestDeliveryDate")
        header_msg  = self._pick(cand, "BlockedMessage", "DeliveryMessage", "Message", "Reason")

        # choose freshest
        src         = ship_src or event_src or "orders"
        status_txt  = ship_status or event_status or header_status
        tt          = ship_tt or event_tt or header_tt
        dd          = ship_date or event_date or header_date
        dm          = ship_msg or event_msg or header_msg

        # code & stable ref
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
            "status_raw": json.dumps({
                "order": cand,
                "used_source": src,
                "ship_status": ship_status,
                "event_status": event_status,
            }, ensure_ascii=False),
        }

        _logger.info(
            "[Monta] %s -> %s (src=%s, code=%s, msg=%s)",
            order_ref, status_txt, src, status_code, (dm or "")[:160]
        )
        return status_txt, meta
