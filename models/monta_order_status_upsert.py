# -*- coding: utf-8 -*-
from odoo import api, fields, models

class MontaOrderStatus(models.Model):
    _inherit = "monta.order.status"   # extend existing model; DO NOT re-declare fields

    @api.model
    def _normalize_vals(self, vals):
        """Map incoming keys and guard against unknown/invalid fields."""
        v = {}
        mapping = {
            "monta_order_ref": ["monta_order_ref"],
            "status": ["status", "order_status"],
            "status_code": ["status_code", "monta_status_code"],
            "source": ["source", "monta_status_source"],
            "delivery_message": ["delivery_message"],
            "track_trace": ["track_trace", "track_trace_url"],
            "delivery_date": ["delivery_date"],
            "last_sync": ["last_sync"],
        }
        for dest, keys in mapping.items():
            for k in keys:
                if k in vals and vals[k] not in (None, False, ""):
                    v[dest] = vals[k]
                    break

        # default last_sync
        if "last_sync" not in v and "last_sync" in self._fields:
            v["last_sync"] = fields.Datetime.now()

        # keep only fields that really exist on the model
        v = {k: v[k] for k in list(v.keys()) if k in self._fields}

        # if 'source' is a selection, ensure value is allowed; otherwise drop it
        if "source" in v and self._fields.get("source").type == "selection":
            allowed = [key for key, _ in self._fields["source"].selection(self.env)]
            if v["source"] not in allowed:
                v.pop("source")
        return v

    @api.model
    def upsert_for_order(self, so, **vals):
        """Create/update a snapshot row per order. Safe to call repeatedly."""
        if not so or not getattr(so, "id", False):
            raise ValueError("upsert_for_order requires a valid sale.order record")

        payload = self._normalize_vals(vals)
        # attach identifiers if those fields exist
        if "sale_order_id" in self._fields:
            payload["sale_order_id"] = so.id
        if "order_name" in self._fields:
            payload["order_name"] = so.name

        # choose lookup key by preference
        domain = []
        if "order_name" in self._fields:
            domain = [("order_name", "=", so.name)]
        elif "sale_order_id" in self._fields:
            domain = [("sale_order_id", "=", so.id)]

        rec = self.sudo().search(domain, limit=1) if domain else self.browse()
        if rec:
            rec.write(payload)
            return rec
        return self.sudo().create(payload)
