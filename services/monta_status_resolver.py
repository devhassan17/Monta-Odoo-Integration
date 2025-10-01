# -*- coding: utf-8 -*-
import time
import requests
from urllib.parse import urljoin

class MontaStatusResolver:
    """
    Robust resolver selecting the freshest signal:
      shipments → orderevents → orders
    Includes a fast path: GET /order/{webshoporderid}
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
        self.s.headers.update({"Accept":"application/json","Cache-Control":"no-cache","Pragma":"no-cache"})

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
    def _as_list(payload):
        if payload is None: return []
        if isinstance(payload, list): return payload
        if isinstance(payload, dict):
            for k in ("Items","items","Data","data","results","Results","value"):
                if isinstance(payload.get(k), list): return payload[k]
            return [payload]
        return []

    @staticmethod
    def _lower(s): return str(s or "").strip().lower()

    def _score(self, target, rec):
        t = self._lower(target)
        if not t or not isinstance(rec, dict): return (0, "")
        fields = ["OrderNumber","Reference","ClientReference","WebshopOrderId","InternalWebshopOrderId","EorderGUID","EorderGuid"]
        best = (0, "")
        for f in fields:
            s = self._lower(rec.get(f))
            if not s: continue
            if s == t: sc = 100
            elif self.allow_loose and s.startswith(t): sc = 85
            elif self.allow_loose and t in s: sc = 70
            else: sc = 0
            if sc > best[0]:
                best = (sc, rec.get(f) or "")
                if sc >= 100: break
        return best

    def _pick_best(self, target, payload):
        lst = self._as_list(payload)
        if not lst: return None
        threshold = 100 if not self.allow_loose else 60
        best_sc, best_rec = 0, None
        for rec in lst:
            sc, _ = self._score(target, rec)
            if sc > best_sc:
                best_sc, best_rec = sc, rec
                if sc >= 100: break
        return best_rec if best_sc >= threshold else None

    @staticmethod
    def _pick(d, *keys):
        if not isinstance(d, dict): return None
        for k in keys:
            v = d.get(k)
            if v not in (None, "", []): return v
        return None

    @staticmethod
    def _derive_order_status(o):
        if not isinstance(o, dict): return None
        if o.get("IsBlocked"):
            msg = o.get("BlockedMessage")
            return "Blocked" + (f" — {msg}" if msg else "")
        if o.get("IsBackorder"):
            return "Backorder"
        if o.get("IsShipped") or o.get("ShippedDate"):
            st = "Shipped"
            if o.get("TrackAndTraceCode"): st += f" (T&T: {o['TrackAndTraceCode']})"
            if o.get("ShippedDate"): st += f" on {o['ShippedDate']}"
            return st
        if o.get("Picked"): return "Picked"
        if o.get("IsPicking"): return "Picking in progress"
        if o.get("ReadyToPick") and o.get("ReadyToPick") != "NotReady": return "Ready to pick"
        for k in ("EstimatedDeliveryTo","EstimatedDeliveryFrom","LatestDeliveryDate"):
            if o.get(k): return f"In progress — ETA {o[k]}"
        if o.get("StatusID") is not None: return f"StatusID={o['StatusID']}"
        if o.get("DeliveryStatusId") is not None: return f"DeliveryStatusId={o['DeliveryStatusId']}"
        return "Received / Pending workflow"

    def _find_order(self, order_ref, tried):
        # fast exact endpoint
        tried.append({"direct": f"order/{order_ref}"})
        scd, direct = self._get(f"order/{order_ref}")
        if 200 <= scd < 300 and isinstance(direct, dict) and direct:
            items = self._as_list(direct)
            return items[0] if items and isinstance(items[0], dict) else direct
        # fallback queries
        params_list = [
            {"orderNumber": order_ref},{"reference": order_ref},{"clientReference": order_ref},
            {"webshopOrderId": order_ref},{"internalWebshopOrderId": order_ref},{"eorderGuid": order_ref},{"search": order_ref}
        ]
        for p in params_list:
            tried.append(p.copy())
            sc, payload = self._get("orders", p)
            if not (200 <= sc < 300): continue
            cand = self._pick_best(order_ref, payload)
            if cand: return cand
        return None

    def resolve(self, order_ref):
        tried = []
        cand = self._find_order(order_ref, tried)
        if not cand:
            return None, {"reason": "Order not found", "tried": tried}

        # hydrate full order
        if cand.get("Id"):
            sci, full = self._get(f"orders/{cand['Id']}")
            if 200 <= sci < 300 and isinstance(full, dict) and full:
                cand = full

        # derive freshest status
        order_status = (self._pick(cand,"DeliveryStatusDescription","Status","CurrentStatus") 
                        or self._derive_order_status(cand))

        meta = {
            "source": "orders",
            "status_code": self._pick(cand,"StatusID","DeliveryStatusId","DeliveryStatusCode"),
            "track_trace": self._pick(cand,"TrackAndTraceLink","TrackAndTraceUrl","TrackAndTrace","TrackingUrl"),
            "delivery_date": self._pick(cand,"DeliveryDate","ShippedDate","EstimatedDeliveryTo","LatestDeliveryDate"),
            "delivery_message": self._pick(cand,"BlockedMessage","DeliveryMessage","Message","Reason"),
            "monta_order_ref": cand.get("OrderNumber") or order_ref,
        }
        return order_status, meta
