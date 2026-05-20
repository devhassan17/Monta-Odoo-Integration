# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models
from odoo.tools import float_utils

_logger = logging.getLogger(__name__)


class MontaOrderStatus(models.Model):
    _inherit = "monta.order.status"

    # -------------------------
    # Matching helpers
    # -------------------------
    def _lower(self, s):
        return str(s or "").strip().lower()

    def _best_match(self, target, candidates):
        t = self._lower(target)
        vals = candidates if isinstance(candidates, list) else [candidates]
        best, best_sc = None, 0

        keys = (
            "OrderNumber",
            "Reference",
            "ClientReference",
            "WebshopOrderId",
            "InternalWebshopOrderId",
            "EorderGUID",
            "EorderGuid",
        )

        for r in vals:
            sc = 0
            data = r or {}
            for k in keys:
                v = self._lower(data.get(k))
                if not v:
                    continue
                if v == t:
                    sc = 100
                    break
                if v.startswith(t):
                    sc = max(sc, 85)
                elif t in v:
                    sc = max(sc, 70)

            if sc > best_sc:
                best_sc, best = sc, r
                if best_sc >= 100:
                    break

        return best if best_sc >= 60 else None

    def _monta_get_order(self, name: str):
        http = self.env["monta.http"].sudo()

        def _as_list(payload):
            if payload is None:
                return []
            if isinstance(payload, list):
                return payload
            if isinstance(payload, dict):
                for k in ("Items", "items", "Data", "data", "results", "Results", "value"):
                    v = payload.get(k)
                    if isinstance(v, list):
                        return v
                return [payload]
            return []

        # direct exact endpoint
        direct = http.get_json(f"order/{name}")
        if isinstance(direct, dict) and direct:
            lst = _as_list(direct)
            if lst:
                match = self._best_match(name, lst)
                if match:
                    return match
            return direct

        # fallback queries
        query_params = (
            {"orderNumber": name},
            {"reference": name},
            {"clientReference": name},
            {"webshopOrderId": name},
            {"internalWebshopOrderId": name},
            {"eorderGuid": name},
            {"search": name},
        )

        for params in query_params:
            data = http.get_json("orders", params=params)
            lst = _as_list(data)
            if not lst:
                continue
            match = self._best_match(name, lst)
            if match:
                return match

        recent = http.get_json("orders", params={"limit": 250, "sort": "desc"})
        match = self._best_match(name, _as_list(recent))
        return match or {}

    @api.model
    def _resolve_and_upsert(self, so):
        if not so or not so.name:
            return False

        data = self._monta_get_order(so.name)
        if not data:
            return False

        vals = {
            "order_name": so.name,
            "sale_order_id": so.id,
            "status": data.get("Status"),
            "monta_order_ref": data.get("OrderNumber"),
        }

        rec = self.search([("order_name", "=", so.name)], limit=1)
        if rec:
            rec.sudo().write(vals)
            return rec

        return self.sudo().create(vals)


# ---------------------------------------------------------------------------
# SaleOrder: all sync logic lives in monta_status_sync.py (loaded before this).
# This stub is intentionally empty — do not add cron/sync methods here.

