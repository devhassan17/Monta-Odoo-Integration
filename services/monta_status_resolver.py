# -*- coding: utf-8 -*-
import json
import time
import requests
from urllib.parse import urljoin
import logging

_logger = logging.getLogger(__name__)


class MontaStatusResolver:
    """
    Freshest wins (shipments → orderevents → orders), but:
    The ORDER HEADER is authoritative for Blocked / Backorder.
    Final override priority: Blocked > Backorder > (shipments/events/header).
    """

    def __init__(self, env, company=None):
        self.env = env
        self.company = company or env.company

        cfg = env["monta.config"].sudo().get_for_company(self.company)
        if not cfg:
            raise ValueError(f"Monta config missing or company not allowed: {self.company.display_name}")

        self.base = (cfg.base_url or "").strip()
        self.user = (cfg.username or "").strip()
        self.pwd = (cfg.password or "").strip()
        self.timeout = int(cfg.timeout or 20)
        self.allow_loose = bool(cfg.match_loose)

        if not (self.base and self.user and self.pwd):
            raise ValueError("Missing Monta Configuration: base_url / username / password")

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
            data = r.json()
        except Exception:
            data = None
        _logger.debug("[Monta] GET %s params=%s -> %s", url, params, r.status_code)
        return r.status_code, data

    @staticmethod
    def _lower(s): return str(s or "").strip().lower()

    @staticmethod
    def _as_list(payload):
        if payload is None: return []
        if isinstance(payload, list): return payload
        if isinstance(payload, dict):
            for k in ("Items", "items", "Data", "data", "results", "Results", "value"):
                if isinstance(payload.get(k), list): return payload[k]
            return [payload]
        return []

    def _score(self, target, rec):
        t = self._lower(target)
        if not t or not isinstance(rec, dict): return (0, "")
        fields = ["OrderNumber", "Reference", "ClientReference", "WebshopOrderId", "InternalWebshopOrderId", "EorderGUID", "EorderGuid"]
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
        if not isinstance(d, dict): return None
        for k in keys:
            v = d.get(k)
            if v not in (None, "", []):
                return v
        return None

    @staticmethod
    def _status_from_flags(o):
        if not isinstance(o, dict): return None
        if o.get("IsBlocked"):
            msg = o.get("BlockedMessage")
            return "Blocked" + (f" — {msg}" if msg else "")
        if (o.get("IsBackorder") or o.get("IsBackOrder") or str(o.get("Backorder", "")).lower() in ("1", "true", "yes")):
            return "Backorder"
        if o.get("IsShipped") or o.get("ShippedDate"):
            st = "Shipped"
            if o.get("TrackAndTraceCode"):
                st += f" (T&T: {o['TrackAndTraceCode']})"
            if o.get("ShippedDate"):
                st += f" on {o['ShippedDate']}"
            return st
        if o.get("Picked"): return "Picked"
        if o.get("IsPicking"): return "Picking in progress"
        if o.get("ReadyToPick") and o.get("ReadyToPick") != "NotReady": return "Ready to pick"
        for k in ("EstimatedDeliveryTo", "EstimatedDeliveryFrom", "LatestDeliveryDate"):
            if o.get(k):
                return f"In progress — ETA {o[k]}"
        return None

    @staticmethod
    def _status_from_text(o):
        txt = ""
        for k in ("DeliveryStatusDescription", "Status", "CurrentStatus"):
            if o.get(k):
                txt = str(o[k])
                break
        low = txt.lower()
        if "blocked" in low: return "Blocked"
        if "backorder" in low or "back order" in low: return "Backorder"
        return txt or None

    @staticmethod
    def _is_blocked_header(o):
        if not isinstance(o, dict): return False
        if o.get("IsBlocked"): return True
        blocked_msg = MontaStatusResolver._lower(o.get("BlockedMessage") or "")
        if "blocked" in blocked_msg: return True
        status_text = MontaStatusResolver._lower(o.get("DeliveryStatusDescription") or o.get("Status") or o.get("CurrentStatus") or "")
        return "blocked" in status_text

    @staticmethod
    def _is_backorder_header(o):
        if not isinstance(o, dict): return False
        if (o.get("IsBackorder") or o.get("IsBackOrder") or str(o.get("Backorder", "")).lower() in ("1", "true", "yes")):
            return True
        status_text = MontaStatusResolver._lower(o.get("DeliveryStatusDescription") or o.get("Status") or o.get("CurrentStatus") or "")
        return ("backorder" in status_text) or ("back order" in status_text)

    def _find_order(self, order_ref, tried):
        tried.append({"direct": f"order/{order_ref}"})
        scd, direct = self._get(f"order/{order_ref}")
        if 200 <= scd < 300 and isinstance(direct, dict) and direct:
            items = self._as_list(direct)
            return items[0] if items and isinstance(items[0], dict) else direct

        params_list = [
            {"orderNumber": order_ref}, {"reference": order_ref}, {"clientReference": order_ref},
            {"webshopOrderId": order_ref}, {"internalWebshopOrderId": order_ref}, {"eorderGuid": order_ref}, {"search": order_ref}
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

    def resolve(self, order_ref):
        tried = []
        cand = self._find_order(order_ref, tried)
        if not cand:
            return None, {"reason": "Order not found or not matching searched reference", "tried": tried}

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
                st = (self._pick(sh, "DeliveryStatusDescription", "ShipmentStatus", "Status", "CurrentStatus")
                      or ("Shipped" if (sh.get("IsShipped") or sh.get("ShippedDate")) else None)
                      or str(sh.get("ShipmentStatus") or ""))
                if st:
                    ship_status = st
                    ship_tt = ship_tt or self._pick(sh, "TrackAndTraceLink", "TrackAndTraceUrl", "TrackAndTrace", "TrackingUrl")
                    ship_date = ship_date or self._pick(sh, "DeliveryDate", "ShippedDate", "EstimatedDeliveryTo", "LatestDeliveryDate")
                    ship_msg = ship_msg or self._pick(sh, "BlockedMessage", "DeliveryMessage", "Message", "Reason")
                    ship_src = lbl
                    break
            if ship_status:
                break

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
                    event_msg = self._pick(e, "BlockedMessage", "DeliveryMessage", "Message", "Reason")
                    event_tt = self._pick(e.get("Shipment") or {}, "TrackAndTraceLink", "TrackAndTraceUrl", "TrackAndTrace", "TrackingUrl")
                    event_date = self._pick(e.get("Shipment") or {}, "DeliveryDate", "ShippedDate", "EstimatedDeliveryTo", "LatestDeliveryDate")
                    event_src = lbl
                    if event_status:
                        break

        header_flag = self._status_from_flags(cand)
        header_txt = self._status_from_text(cand)
        header_status = header_flag or header_txt or "Received / Pending workflow"
        header_tt = self._pick(cand, "TrackAndTraceLink", "TrackAndTraceUrl", "TrackAndTrace", "TrackingUrl")
        header_date = self._pick(cand, "DeliveryDate", "ShippedDate", "EstimatedDeliveryTo", "LatestDeliveryDate")
        header_msg = self._pick(cand, "BlockedMessage", "DeliveryMessage", "Message", "Reason")

        src = ship_src or event_src or "orders"
        status_txt = ship_status or event_status or header_status
        tt = ship_tt or event_tt or header_tt
        dd = ship_date or event_date or header_date
        dm = ship_msg or event_msg or header_msg

        header_blocked = self._is_blocked_header(cand)
        header_backord = self._is_backorder_header(cand)

        if header_blocked:
            status_txt = "Blocked" + (f" — {header_msg}" if header_msg else "")
        elif header_backord:
            current_lower = (status_txt or "").lower()
            advanced_statuses = ["shipped", "picked", "picking", "ready to pick", "delivered", "in progress"]
            if not any(adv in current_lower for adv in advanced_statuses):
                status_txt = "Backorder"

        status_code = self._pick(cand, "StatusID", "DeliveryStatusId", "DeliveryStatusCode")
        stable_ref = (refs["orderNumber"] or refs["webshopOrderId"] or refs["orderGuid"]
                      or refs["clientReference"] or refs["orderReference"] or order_ref)

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
                "header_blocked": header_blocked,
                "header_backorder": header_backord,
                "final_status": status_txt,
            }, ensure_ascii=False),
        }
        return status_txt, meta
