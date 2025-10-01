# -*- coding: utf-8 -*-
import time
import requests
from urllib.parse import urljoin

class MontaStatusResolver:
    """
    Robust, tenant-safe resolver.

    Order lookup tries, in order (each may return list OR paged dict):
      0) /order/{webshoporderid}     <-- fast exact path (NEW)
      1) /orders?orderNumber=<ref>
      2) /orders?reference=<ref>
      3) /orders?clientReference=<ref>
      4) /orders?webshopOrderId=<ref>
      5) /orders?internalWebshopOrderId=<ref>
      6) /orders?eorderGuid=<ref>
      7) /orders?search=<ref>
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

    @staticmethod
    def _lower(s): return str(s or "").strip().lower()

    def _score(self, target, rec):
        t = self._lower(target)
        if not t or not isinstance(rec, dict): return (0, "")
        fields = ["OrderNumber", "Reference", "ClientReference", "WebshopOrderId",
                  "InternalWebshopOrderId", "EorderGUID", "EorderGuid"]
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

    def _find_order(self, order_ref, tried):
        # NEW: try direct endpoint first
        tried.append({"direct": f"order/{order_ref}"})
        sc_direct, direct_payload = self._get(f"order/{order_ref}")
        if 200 <= sc_direct < 300 and isinstance(direct_payload, dict) and direct_payload:
            items = self._as_list(direct_payload)
            if items and isinstance(items[0], dict):
                return items[0]
            return direct_payload

        # fallback queries
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
            if not (200 <= sc < 300): continue
            cand = self._pick_best(order_ref, payload)
            if cand: return cand
        return None

    def resolve(self, order_ref):
        tried = []
        cand = self._find_order(order_ref, tried)
        if not cand:
            return None, {"reason": "Order not found or not matching searched reference", "tried": tried}
        return "Received / Pending workflow", {"monta_order_ref": order_ref}
