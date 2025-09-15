# -*- coding: utf-8 -*-
"""
Monta deep resolver: tries multiple keys for shipments/events and falls back to order header.
Reads System Parameters: monta.base_url / monta.username / monta.password (/ monta.timeout)
No XML needed. Use hooks.py to create a cron.
"""
import time
import logging
import requests
from urllib.parse import urljoin

DEFAULT_TIMEOUT = 20

_logger = logging.getLogger(__name__)


class MontaStatusResolver:
    """
    Tiny Monta client focused on resolving a single order's delivery status.
    Usage:
        resolver = MontaStatusResolver(env)
        status_text, meta = resolver.resolve("BC00013")
    Returns:
        status_text (str|None), meta (dict with source/orderId/refs/track&trace if any)
    """

    def __init__(self, env, *, base_url=None, username=None, password=None, timeout=None):
        self.env = env
        ICP = env["ir.config_parameter"].sudo()

        # Read from params unless explicitly provided
        self.base_url = (base_url or (ICP.get_param("monta.base_url") or "")).strip()
        self.username = (username or (ICP.get_param("monta.username") or "")).strip()
        self.password = (password or (ICP.get_param("monta.password") or "")).strip()
        self.timeout = int(timeout or (ICP.get_param("monta.timeout") or DEFAULT_TIMEOUT))

        if not (self.base_url and self.username and self.password):
            raise ValueError("Missing System Parameters: monta.base_url / monta.username / monta.password")

        if not self.base_url.endswith("/"):
            self.base_url += "/"

        self.s = requests.Session()
        self.s.auth = (self.username, self.password)
        self.s.headers.update({
            "Accept": "application/json",
            # defeat intermediary caches
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        })

    # ----------------------- helpers -----------------------

    def _get(self, path, params=None, label=None):
        params = dict(params or {})
        params["_ts"] = int(time.time())  # cache buster
        url = urljoin(self.base_url, path.lstrip("/"))
        try:
            r = self.s.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as e:
            _logger.error("[Monta] HTTP ERR GET %s → %s", url, e)
            return None, None
        _logger.debug("[Monta] HTTP %s GET %s   %s", r.status_code, r.url, f"← {label}" if label else "")
        try:
            j = r.json()
        except Exception:
            j = None
        return r.status_code, j

    @staticmethod
    def _first(j):
        if isinstance(j, list) and j:
            return j[0]
        if isinstance(j, dict):
            return j
        return None

    @staticmethod
    def _pick_status(d):
        if not isinstance(d, dict):
            return None
        for k in [
            "DeliveryStatusDescription", "DeliveryStatusCode", "Status", "State",
            "OrderStatus", "ActionCode", "ShipmentStatus", "CurrentStatus",
        ]:
            v = d.get(k)
            if v not in (None, "", []):
                return v
        return None

    @staticmethod
    def _derive_order_status(o):
        if not isinstance(o, dict):
            return None
        # Priority ladder based on common Monta booleans
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
        ready = o.get("ReadyToPick")
        if ready not in (None, "", "NotReady"):
            return "Ready to pick"
        if o.get("IsBackorder"):
            return "Backorder"
        for dkey in ("EstimatedDeliveryTo", "EstimatedDeliveryFrom", "LatestDeliveryDate"):
            if o.get(dkey):
                return f"In progress — ETA {o[dkey]}"
        if o.get("StatusID") is not None:
            return f"StatusID={o['StatusID']}"
        if o.get("DeliveryStatusId") is not None:
            return f"DeliveryStatusId={o['DeliveryStatusId']}"
        return "Received / Pending workflow"

    # ----------------------- public API -----------------------

    def resolve(self, order_ref):
        """
        Return (status_text, meta) choosing best signal:
        shipments > orderevents > orders header (derived)
        """
        if not order_ref:
            return None, {"reason": "empty reference"}

        # 1) Orders search (header)
        sc, orders = self._get("orders", {"search": order_ref}, label="orders?search")
        if not (sc and 200 <= sc < 300 and isinstance(orders, list) and orders):
            sc2, orders2 = self._get("orders", {"clientReference": order_ref}, label="orders?clientReference")
            orders = orders2 if (sc2 and 200 <= sc2 < 300) else None
        o = self._first(orders) if orders else None
        if not o:
            _logger.warning("[Monta] No order found for ref=%s", order_ref)
            return None, {"source": "orders", "status_code": None, "order_id": None}

        order_id = o.get("Id")

        # Try canonical record when possible
        if order_id is not None:
            sc_id, o2 = self._get(f"orders/{order_id}", {}, label="orders/{Id}")
            if sc_id and 200 <= sc_id < 300 and isinstance(o2, dict):
                o = o2

        # Build reference set
        refs = {
            "orderId": o.get("Id"),
            "orderReference": o.get("Reference") or order_ref,
            "clientReference": o.get("ClientReference") or order_ref,
            "orderGuid": o.get("EorderGUID") or o.get("EorderGuid"),
            "webshopOrderId": o.get("WebshopOrderId") or o.get("InternalWebshopOrderId"),
        }

        # 2) Shipments (prefer shipped)
        for label, params in (
            ("shipments?orderId", {"orderId": refs["orderId"]}),
            ("shipments?orderReference", {"orderReference": refs["orderReference"]}),
            ("shipments?clientReference", {"clientReference": refs["clientReference"]}),
            ("shipments?orderGuid", {"orderGuid": refs["orderGuid"]}),
            ("shipments?webshopOrderId", {"webshopOrderId": refs["webshopOrderId"]}),
        ):
            params = {k: v for k, v in params.items() if v not in (None, "")}
            if not params:
                continue
            scS, ships = self._get("shipments", params, label=label)
            if scS and 200 <= scS < 300 and isinstance(ships, list) and ships:
                for sh in ships:
                    if not isinstance(sh, dict):
                        continue
                    st = (
                        self._pick_status(sh)
                        or ("Shipped" if (sh.get("IsShipped") or sh.get("ShippedDate")) else None)
                        or str(sh.get("ShipmentStatus") or "")
                    )
                    if isinstance(st, str) and st.strip():
                        tt = sh.get("TrackAndTraceCode")
                        dt = sh.get("ShippedDate")
                        if st.lower() == "shipped" and (tt or dt):
                            if tt:
                                st += f" (T&T: {tt})"
                            if dt:
                                st += f" on {dt}"
                        return st, {
                            "source": "shipments",
                            "order_id": refs["orderId"],
                            "status_code": sh.get("ShipmentStatus"),
                            "track_trace": tt,
                        }

        # 3) Order events (latest)
        for label, params in (
            ("orderevents?orderId", {"orderId": refs["orderId"], "limit": 1, "sort": "desc"}),
            ("orderevents?orderReference", {"orderReference": refs["orderReference"], "limit": 1, "sort": "desc"}),
            ("orderevents?clientReference", {"clientReference": refs["clientReference"], "limit": 1, "sort": "desc"}),
            ("orderevents?orderGuid", {"orderGuid": refs["orderGuid"], "limit": 1, "sort": "desc"}),
            ("orderevents?webshopOrderId", {"webshopOrderId": refs["webshopOrderId"], "limit": 1, "sort": "desc"}),
        ):
            params = {k: v for k, v in params.items() if v not in (None, "")}
            if not params:
                continue
            scE, ev = self._get("orderevents", params, label=label)
            if scE and 200 <= scE < 300 and isinstance(ev, list) and ev:
                e = ev[0]
                st = (
                    self._pick_status(e)
                    or self._pick_status((e.get("Order") or {}))
                    or self._pick_status((e.get("Shipment") or {}))
                    or (f"Event: {e.get('ActionCode')}" if e.get("ActionCode") else None)
                )
                if isinstance(st, str) and st.strip():
                    return st, {
                        "source": "orderevents",
                        "order_id": refs["orderId"],
                        "status_code": e.get("Status"),
                    }

        # 4) Fallback to the order header
        st = self._pick_status(o) or self._derive_order_status(o)
        tt = o.get("TrackAndTraceCode")
        return st, {
            "source": "orders",
            "order_id": refs["orderId"],
            "status_code": o.get("StatusID"),
            "track_trace": tt,
            "refs": {k: v for k, v in refs.items() if v},
        }
