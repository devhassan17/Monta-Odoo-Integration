# -*- coding: utf-8 -*-
from odoo import api, fields, models


class MontaOrderStatus(models.Model):
    _inherit = "monta.order.status"

    # -------------------------
    # Helpers
    # -------------------------
    @api.model
    def _normalize_vals(self, vals):
        v = {}
        mapping = {
            "monta_order_ref": ("monta_order_ref",),
            "status": ("status", "order_status"),
            "status_code": ("status_code", "monta_status_code"),
            "source": ("source", "monta_status_source"),
            "delivery_message": ("delivery_message",),
            "track_trace": ("track_trace", "track_trace_url"),
            "delivery_date": ("delivery_date",),
            "last_sync": ("last_sync",),
        }

        for dest, keys in mapping.items():
            for k in keys:
                if k in vals and vals[k] not in (None, False, ""):
                    v[dest] = vals[k]
                    break

        if "last_sync" in self._fields and "last_sync" not in v:
            v["last_sync"] = fields.Datetime.now()

        # keep only real model fields
        v = {k: val for k, val in v.items() if k in self._fields}

        # validate selection value
        field = self._fields.get("source")
        if field and "source" in v and field.type == "selection":
            sel = field.selection
            options = sel(self.env) if callable(sel) else (sel or [])
            allowed = {opt[0] for opt in options}
            if v["source"] not in allowed:
                v.pop("source", None)

        return v

    # -------------------------
    # Public API
    # -------------------------
    @api.model
    def upsert_for_order(self, so, **vals):
        if not so or not getattr(so, "id", False):
            raise ValueError("upsert_for_order requires a valid sale.order record")

        payload = self._normalize_vals(vals)

        if "sale_order_id" in self._fields:
            payload["sale_order_id"] = so.id
        if "order_name" in self._fields:
            payload["order_name"] = so.name

        if "order_name" in self._fields:
            domain = [("order_name", "=", so.name)]
        elif "sale_order_id" in self._fields:
            domain = [("sale_order_id", "=", so.id)]
        else:
            domain = []

        rec = self.sudo().search(domain, limit=1) if domain else self.browse()
        if rec:
            rec.write(payload)
            return rec

        return self.sudo().create(payload)
