# -*- coding: utf-8 -*-
import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class MontaOrderStatus(models.Model):
    _name = "monta.order.status"
    _description = "Monta Order Status snapshot"
    _order = "last_sync desc, id desc"

    # Links / identifiers
    sale_order_id = fields.Many2one(
        "sale.order",
        string="Sales Order",
        index=True,
        ondelete="cascade",
        required=True,
    )
    order_name = fields.Char(string="Order Name", index=True, required=True)
    monta_order_ref = fields.Char(string="Monta Order Ref", index=True)

    # Status (match views & inbound code)
    status = fields.Char(string="Order Status")
    status_code = fields.Char(string="Status Code")
    source = fields.Char(string="Source", default="orders")

    # Extra info
    delivery_message = fields.Char(string="Delivery Message")
    track_trace = fields.Char(string="Track & Trace URL")
    delivery_date = fields.Date(string="Delivery Date")   # Monta usually sends a date
    last_sync = fields.Datetime(string="Last Sync (UTC)", default=fields.Datetime.now, index=True)

    # NEW: needed by sale_order_inbound.py (your logs show a create() with 'status_raw')
    status_raw = fields.Text(string="Raw Status (JSON)")

    _sql_constraints = [
        ("monta_order_name_unique", "unique(order_name)", "Monta order snapshot must be unique by order name."),
    ]

    # -------- helpers used by your cron / upsert --------
    @api.model
    def _normalize_vals(self, vals):
        """Accept both legacy and canonical keys so other code doesn’t break."""
        return {
            "monta_order_ref": vals.get("monta_order_ref"),
            "status": vals.get("status", vals.get("order_status")),
            "status_code": vals.get("status_code", vals.get("monta_status_code")),
            "source": vals.get("source", vals.get("monta_status_source", "orders")),
            "delivery_message": vals.get("delivery_message"),
            "track_trace": vals.get("track_trace", vals.get("track_trace_url")),
            "delivery_date": vals.get("delivery_date"),
            "last_sync": vals.get("last_sync") or fields.Datetime.now(),
            "status_raw": vals.get("status_raw"),  # safe passthrough
        }

    @api.model
    def upsert_for_order(self, so, **vals):
        """
        Create or update a single snapshot row per sale.order (keyed by order_name).
        Safe to call repeatedly.
        """
        if not so or not so.id:
            raise ValueError("upsert_for_order requires a valid sale.order record")

        base_vals = self._normalize_vals(vals)
        base_vals.update({"sale_order_id": so.id, "order_name": so.name})

        rec = self.sudo().search([("order_name", "=", so.name)], limit=1)
        if rec:
            rec.write(base_vals)
            return rec
        return self.sudo().create(base_vals)
