# -*- coding: utf-8 -*-
"""
MontaStatusResolver
-------------------
Resolves a human-friendly delivery status for a Monta order, using multiple
API paths in a robust order. This version **prefers the singular endpoints**
(`/order/{ref}` and `/order?search=`) because they are the freshest on your
tenant, then falls back to shipments/events and finally the plural header
(`/orders`).

System Parameters used (Settings → Technical → System Parameters):
    - monta.base_url     e.g. https://api-v6.monta.nl/
    - monta.username
    - monta.password
    - monta.timeout      (optional, default 20s)
"""

import logging
import time
from urllib.parse import urljoin

import requests

_logger = logging.getLogger("monta_order_status_sync")


class MontaStatusResolver:
    # ------------------------------ lifecycle ------------------------------

    def __init__(self, env):
        """Create a requests session from Odoo System Parameters."""
        self.env = env
        ICP = env["ir.config_parameter"].sudo()

        base = (ICP.get_param("monta.base_url") or "").strip()
        user = (ICP.get_param("monta.username") or "").strip()
        pwd = (ICP.get_param("monta.password") or "").strip()
        try:
            timeout = int(ICP.get_param("monta.timeout") or 20)
        except Exception:
            timeout = 20

        if not (base and user and pwd):
            raise RuntimeError(
                "Missing System Parameters: monta.base_url / monta.username / monta.password"
            )

        if not base.endswith("/"):
            base += "/"

        self.base = base
        self.timeout = timeout

        s = requests.Session()
        s.auth = (user, pwd)
        s.headers.update(
            {
                "Accept": "application/json",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }
        )
        self.s = s

    # ------------------------------ http utils ------------------------------

    def _get(self, path, params=None, label=None):
        """GET JSON with timestamp param and return (status_code, json_or_None)."""
        params = dict(params or {})
        params["_ts"] = int(time.time())
        url = urljoin(self.base, path.lstrip("/"))
        r = self.s.get(url, params=params, timeout=self.timeout)
        _logger.debug("HTTP %s GET %s%s", r.status_code, r.url, f"   ← {label}" if label else "")
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
        """Scan common status keys on a dict."""
        if not isinstance(d, dict):
            return None
        for k in (
            "DeliveryStatusDescription",
            "DeliveryStatusCode",
            "Status",
            "State",
            "OrderStatus",
            "ActionCode",
            "ShipmentStatus",
            "CurrentStatus",
        ):
            v = d.get(k)
            if v not in (None, "", []):
                return v
        return None

    @staticmethod
    def _normalize_status_text(text):
        """Normalize common lowercase/raw values into nice labels."""
        if not text:
            return None
        t = str(text).strip()
        mapping = {
            "delivered": "Delivered",
            "shipped": "Shipped",
            "blocked": "Blocked",
            "picked": "Picked",
            "picking": "Picking in progress",
        }
        return mapping.get(t.lower(), t)

    @staticmethod
    def _derive_order_status(o):
        """If we only have an order header, derive a readable status."""
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
        for dkey in ("EstimatedDeliveryTo", "EstimatedDeliveryFrom", "LatestDeliveryDate"):
            if o.get(dkey):
                return f"In progress — ETA {o[dkey]}"
        if o.get("StatusID") is not None:
            return f"StatusID={o['StatusID']}"
        if o.get("DeliveryStatusId") is not None:
            return f"DeliveryStatusId={o['DeliveryStatusId']}"
        return "Received / Pending workflow"

    # ------------------------------ resolvers ------------------------------

    def _resolve_via_order_single(self, order_ref):
        """
        Prefer Monta's singular endpoints which are freshest on your tenant:
            - GET /order/{reference}
            - GET /order?search={reference}
        Returns (status_text, meta) or (None, None).
        """
        # /order/{ref}
        sc, body = self._get(f"order/{order_ref}", {}, label="order/{ref}")
        if sc and 200 <= sc < 300 and isinstance(body, dict):
            st = (
                self._pick_status(body)
                or body.get("Status")
                or body.get("DeliveryStatusDescription")
                or body.get("OrderStatus")
            )
            st = self._normalize_status_text(st)
            if st:
                tt = body.get("TrackAndTraceCode") or body.get("TrackAndTraceLink")
                return st, {
                    "source": "order_single",
                    "order_id": body.get("Id"),
                    "status_code": body.get("StatusID") or body.get("DeliveryStatusId"),
                    "track_trace": tt,
                }

        # /order?search=
        sc2, body2 = self._get("order", {"search": order_ref}, label="order?search")
        if sc2 and 200 <= sc2 < 300:
            obj = body2[0] if isinstance(body2, list) and body2 else (
                body2 if isinstance(body2, dict) else None
            )
            if isinstance(obj, dict):
                st = (
                    self._pick_status(obj)
                    or obj.get("Status")
                    or obj.get("DeliveryStatusDescription")
                    or obj.get("OrderStatus")
                )
                st = self._normalize_status_text(st)
                if st:
                    tt = obj.get("TrackAndTraceCode") or obj.get("TrackAndTraceLink")
                    return st, {
                        "source": "order_single",
                        "order_id": obj.get("Id"),
                        "status_code": obj.get("StatusID") or obj.get("DeliveryStatusId"),
                        "track_trace": tt,
                    }

        return None, None

    # ------------------------------ public API ------------------------------

    def resolve(self, order_ref):
        """
        Resolve a Monta status for the given external reference (usually sale.name
        or client_order_ref). Returns:
            (status_text, meta_dict)
        where meta_dict may include: source, order_id, status_code, track_trace, refs.
        """
        if not order_ref:
            return None, {"reason": "empty reference"}

        # 0) Prefer singular endpoints first (freshest on this tenant)
        st0, meta0 = self._resolve_via_order_single(order_ref)
        if st0:
            return st0, meta0

        # 1) Orders list/header (plural) to get canonical record & Id
        sc, orders = self._get("orders", {"search": order_ref}, label="orders?search")
        if not (200 <= sc < 300 and isinstance(orders, list) and orders):
            sc2, orders2 = self._get("orders", {"clientReference": order_ref}, label="orders?clientReference")
            orders = orders2 if (200 <= sc2 < 300) else None
        o = self._first(orders) if orders else None
        if not o:
            return None, {"reason": "Order not found"}

        # Try canonical orders/{Id} if exposed
        if o.get("Id"):
            sc_id, o_by_id = self._get(f"orders/{o['Id']}", {}, label="orders/{Id}")
            if 200 <= sc_id < 300 and isinstance(o_by_id, dict):
                o = o_by_id

        # Candidate refs for shipments/events
        refs = {
            "orderId": o.get("Id"),
            "orderReference": o.get("Reference") or order_ref,
            "clientReference": o.get("ClientReference") or order_ref,
            "orderGuid": o.get("EorderGUID") or o.get("EorderGuid") or o.get("EorderGUID".lower()),
            "webshopOrderId": o.get("WebshopOrderId") or o.get("InternalWebshopOrderId"),
        }

        # 2) Shipments (prefer a true shipment signal)
        ship_status = None
        ship_where = None
        ship_tt = None
        ship_param_sets = [
            ("shipments", {"orderId": refs["orderId"]}, "shipments?orderId"),
            ("shipments", {"orderReference": refs["orderReference"]}, "shipments?orderReference"),
            ("shipments", {"clientReference": refs["clientReference"]}, "shipments?clientReference"),
            ("shipments", {"orderGuid": refs["orderGuid"]}, "shipments?orderGuid"),
            ("shipments", {"webshopOrderId": refs["webshopOrderId"]}, "shipments?webshopOrderId"),
        ]
        for path, params, label in ship_param_sets:
            params = {k: v for k, v in params.items() if v not in (None, "")}
            if not params:
                continue
            scS, ships = self._get(path, params, label=label)
            if 200 <= scS < 300 and isinstance(ships, list) and ships:
                for sh in ships:
                    if not isinstance(sh, dict):
                        continue
                    st = (
                        self._pick_status(sh)
                        or ("Shipped" if (sh.get("IsShipped") or sh.get("ShippedDate")) else None)
                        or str(sh.get("ShipmentStatus") or "")
                    )
                    st = self._normalize_status_text(st)
                    if st:
                        tt = sh.get("TrackAndTraceCode") or sh.get("TrackAndTraceLink")
                        dt = sh.get("ShippedDate")
                        if st.lower() == "shipped" and (tt or dt):
                            if tt:
                                st += f" (T&T: {tt})"
                            if dt:
                                st += f" on {dt}"
                        ship_status = st
                        ship_where = label
                        ship_tt = tt
                        break
            if ship_status:
                break

        if ship_status:
            return ship_status, {
                "source": ship_where,
                "order_id": refs["orderId"],
                "track_trace": ship_tt,
            }

        # 3) Order events (latest)
        event_status = None
        event_where = None
        event_param_sets = [
            ("orderevents", {"orderId": refs["orderId"], "limit": 1, "sort": "desc"}, "orderevents?orderId"),
            ("orderevents", {"orderReference": refs["orderReference"], "limit": 1, "sort": "desc"}, "orderevents?orderReference"),
            ("orderevents", {"clientReference": refs["clientReference"], "limit": 1, "sort": "desc"}, "orderevents?clientReference"),
            ("orderevents", {"orderGuid": refs["orderGuid"], "limit": 1, "sort": "desc"}, "orderevents?orderGuid"),
            ("orderevents", {"webshopOrderId": refs["webshopOrderId"], "limit": 1, "sort": "desc"}, "orderevents?webshopOrderId"),
        ]
        for path, params, label in event_param_sets:
            params = {k: v for k, v in params.items() if v not in (None, "")}
            if not params:
                continue
            scE, ev = self._get(path, params, label=label)
            if 200 <= scE < 300 and isinstance(ev, list) and ev:
                e = ev[0]
                st = (
                    self._pick_status(e)
                    or self._pick_status(e.get("Order") or {})
                    or self._pick_status(e.get("Shipment") or {})
                    or (f"Event: {e.get('ActionCode')}" if e.get("ActionCode") else None)
                )
                st = self._normalize_status_text(st)
                if st:
                    event_status = st
                    event_where = label
                    break

        if event_status:
            return event_status, {
                "source": event_where,
                "order_id": refs["orderId"],
            }

        # 4) Final fallback: plural orders header
        order_status = self._pick_status(o) or self._derive_order_status(o)
        order_status = self._normalize_status_text(order_status)

        return order_status, {
            "source": "orders",
            "order_id": refs["orderId"],
            "status_code": o.get("StatusID") or o.get("DeliveryStatusId"),
            "track_trace": o.get("TrackAndTraceCode") or o.get("TrackAndTraceLink"),
            "refs": {k: v for k, v in refs.items() if v},
        }
