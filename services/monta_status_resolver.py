# -*- coding: utf-8 -*-
import time
import requests
from urllib.parse import urljoin


class MontaStatusResolver:
    """
    Resolver with safe-but-flexible matching:
      - Queries Monta by several keys.
      - Scores candidates across Reference, ClientReference, WebshopOrderId,
        InternalWebshopOrderId, EorderGUID (case-insensitive).
      - Accepts the candidate with the highest score if it meets a threshold.
      - Still prevents obviously wrong cross-order “bleed”.
    """

    def __init__(self, env):
        self.env = env
        ICP = env["ir.config_parameter"].sudo()
        self.base = (ICP.get_param("monta.base_url") or ICP.get_param("monta.api.base_url") or "").strip()
        self.user = (ICP.get_param("monta.username") or "").strip()
        self.pwd  = (ICP.get_param("monta.password") or "").strip()
        self.timeout = int(ICP.get_param("monta.timeout") or 20)
        # Optional knob: set to "0" to force strict exact-only matches
        self.allow_loose = (ICP.get_param("monta.match_loose") or "1").strip() != "0"

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

    # --------------- matching utils ---------------
    @staticmethod
    def _first(obj):
        if isinstance(obj, list) and obj:
            return obj[0]
        if isinstance(obj, dict):
            return obj
        return None

    @staticmethod
    def _lower(s):
        return str(s or "").strip().lower()

    def _match_score(self, target, record):
        """
        Score how well 'record' matches 'target'.
        Return (score, matched_value). Higher is better.
        """
        t = self._lower(target)
        if not t or not isinstance(record, dict):
            return (0, "")

        keys = [
            "Reference",
            "ClientReference",
            "WebshopOrderId",
            "InternalWebshopOrderId",
            "EorderGUID", "EorderGuid",
        ]
        best = (0, "")
        for k in keys:
            v = record.get(k)
            s = self._lower(v)
            if not s:
                continue
            # exact equals
            if s == t:
                sc = 100
            # startswith (very likely a decorated reference like "BC00026-1")
            elif self.allow_loose and s.startswith(t):
                sc = 80
            # contains (fallback for systems that append/prepend noise)
            elif self.allow_loose and t in s:
                sc = 60
            else:
                sc = 0

            if sc > best[0]:
                best = (sc, v or "")
                # exact hit — we can stop early
                if sc >= 100:
                    break
        return best

    def _best_candidate(self, order_ref, raw_list):
        """
        Given a list (from /orders?search=...), pick the best scoring item.
        Accept only if score >= threshold.
        """
        if not isinstance(raw_list, list) or not raw_list:
            return None
        threshold = 100 if not self.allow_loose else 60  # exact-only vs. allows contains
        winner = (0, None)
        for rec in raw_list:
            sc, matched_val = self._match_score(order_ref, rec)
            if sc > winner[0]:
                winner = (sc, rec)
                if sc >= 100:
                    break
        return winner[1] if winner[0] >= threshold else None

    # --------------- pickers ---------------
    @staticmethod
    def _pick(d, *keys):
        if not isinstance(d, dict):
            return None
        for k in keys:
            v = d.get(k)
            if v not in (None, "", []):
                return v
        return None

    # --------------- resolve ---------------
    def resolve(self, order_ref):
        """
        Returns: (status_text, meta) or (None, {'reason': ...})
        """
        # 1) search across keys; choose the best candidate
        sc, data = self._get("orders", {"search": order_ref})
        candidate = None
        if 200 <= sc < 300 and isinstance(data, list) and data:
            candidate = self._best_candidate(order_ref, data)

        # fallback queries if search didn’t give an acceptable candidate
        if not candidate:
            for params in (
                {"clientReference": order_ref},
                {"webshopOrderId": order_ref},
            ):
                sc2, data2 = self._get("orders", params)
                if 200 <= sc2 < 300 and isinstance(data2, list) and data2:
                    candidate = self._best_candidate(order_ref, data2)
                    if candidate:
                        break

        if not candidate:
            return None, {"reason": "Order not found or not matching searched reference"}

        # 2) hydrate by Id if available
        if candidate.get("Id"):
            scid, full = self._get(f"orders/{candidate['Id']}")
            if 200 <= scid < 300 and isinstance(full, dict) and full:
                candidate = full

        # 3) build normalized snapshot from the (hydrated) order header
        o = candidate
        status_txt = (
            self._pick(o, "DeliveryStatusDescription", "Status", "CurrentStatus")
            or ("Shipped" if (o.get("IsShipped") or o.get("ShippedDate")) else None)
            or "Received / Pending workflow"
        )
        status_code = self._pick(o, "StatusID", "DeliveryStatusId", "DeliveryStatusCode")
        track = self._pick(o, "TrackAndTraceLink", "TrackAndTraceUrl", "TrackAndTrace", "TrackingUrl")
        delivery = self._pick(o, "DeliveryDate", "ShippedDate", "EstimatedDeliveryTo", "LatestDeliveryDate")
        message = self._pick(o, "BlockedMessage", "DeliveryMessage", "Message", "Reason")

        meta = {
            "source": "orders",
            "status_code": status_code,
            "track_trace": track,
            "delivery_date": delivery,
            "delivery_message": message,
            # Prefer these in order; fall back to the searched ref
            "monta_order_ref": (
                o.get("WebshopOrderId")
                or o.get("InternalWebshopOrderId")
                or o.get("ClientReference")
                or o.get("Reference")
                or order_ref
            ),
        }
        return status_txt, meta
