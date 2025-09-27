# models/monta_order_status.py
# -*- coding: utf-8 -*-
import logging
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

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

    # Status (stored, not computed)
    status = fields.Char(string="Order Status")
    # accept int, but tolerate legacy strings by casting on write
    status_code = fields.Integer(string="Status Code")

    # Keep Selection to avoid registry cleanups changing choices dynamically
    source = fields.Selection(
        selection=[
            ("orders", "orders"),
            ("shipments", "shipments"),
            ("orderevents", "orderevents"),
            ("events", "events"),  # backward compat
        ],
        string="Source",
        default="orders",
        index=True,
    )

    # Extra info
    delivery_message = fields.Char(string="Delivery Message")
    track_trace = fields.Char(string="Track & Trace URL")
    delivery_date = fields.Date(string="Delivery Date")
    last_sync = fields.Datetime(string="Last Sync (UTC)", default=fields.Datetime.now, index=True)

    # Raw payload (needed by inbound writer)
    status_raw = fields.Text(string="Raw Status (JSON)")

    # Fast flag to show "available on Monta"
    on_monta = fields.Boolean(
        string="Available on Monta",
        compute="_compute_on_monta",
        store=True,
        index=True,
    )

    _sql_constraints = [
        ("monta_order_name_unique", "unique(order_name)", "Monta order snapshot must be unique by order name."),
    ]

    # ---------- COMPUTES ----------
    @api.depends("monta_order_ref")
    def _compute_on_monta(self):
        for r in self:
            r.on_monta = bool((r.monta_order_ref or "").strip())

    # ---------- HELPERS ----------
    @api.model
    def _safe_int(self, v):
        if v in (False, None, ""):
            return 0
        try:
            return int(v)
        except Exception:
            return 0

    @api.model
    def _normalize_vals(self, vals):
        """Accept both legacy and canonical keys so other code doesn’t break.
        IMPORTANT: we DO NOT include keys that are falsy to avoid overwriting with blanks.
        """
        out = {}

        # Monta ref: only set if provided and non-empty
        ref = vals.get("monta_order_ref")
        if ref is not None and str(ref).strip() != "":
            out["monta_order_ref"] = str(ref).strip()

        # Status (char)
        status = vals.get("status", vals.get("order_status"))
        if status is not None:
            out["status"] = status

        # Status code (int) – cast safely
        sc = vals.get("status_code", vals.get("monta_status_code"))
        if sc is not None:
            out["status_code"] = self._safe_int(sc)

        # Source (selection)
        src = vals.get("source", vals.get("monta_status_source"))
        if src is not None:
            out["source"] = src

        # Delivery message/url/date
        if vals.get("delivery_message") is not None:
            out["delivery_message"] = vals.get("delivery_message")

        if vals.get("track_trace") is not None:
            out["track_trace"] = vals.get("track_trace")
        elif vals.get("track_trace_url") is not None:
            out["track_trace"] = vals.get("track_trace_url")

        if vals.get("delivery_date") is not None:
            out["delivery_date"] = vals.get("delivery_date")

        # last_sync always refreshed unless explicitly passed
        out["last_sync"] = vals.get("last_sync") or fields.Datetime.now()

        if vals.get("status_raw") is not None:
            out["status_raw"] = vals.get("status_raw")

        return out

    # ---------- API used by your cron/upserts ----------
    @api.model
    def upsert_for_order(self, so, **vals):
        """
        Create or update a single snapshot row per sale.order (keyed by order_name).
        SAFE semantics:
        - Never overwrite monta_order_ref with blank/False.
        - Cast status_code to int safely.
        - Prevent the same non-empty monta_order_ref from appearing on multiple orders.
        """
        if not so or not so.id:
            raise ValueError("upsert_for_order requires a valid sale.order record")

        base_vals = self._normalize_vals(vals)
        base_vals.update({"sale_order_id": so.id, "order_name": so.name})

        rec = self.sudo().search([("order_name", "=", so.name)], limit=1)

        # Duplicate MontaRef protection (only for non-empty refs)
        incoming_ref = base_vals.get("monta_order_ref")
        if incoming_ref:
            clash = self.sudo().search(
                [("monta_order_ref", "=", incoming_ref), ("order_name", "!=", so.name)],
                limit=1,
            )
            if clash:
                # Ref already belongs to a different order — do not propagate it.
                _logger.warning(
                    "Blocked duplicate Monta ref %s for order %s (already used by %s).",
                    incoming_ref, so.name, clash.order_name
                )
                # Remove the ref from vals so we don't write it
                base_vals.pop("monta_order_ref", None)

        if rec:
            # merge without blanking existing ref
            return rec.sudo().write(base_vals) or rec

        return self.sudo().create(base_vals)

    # ---------- CONSTRAINTS ----------
    @api.constrains("monta_order_ref", "order_name")
    def _check_unique_nonempty_monta_ref(self):
        """Ensure a non-empty monta_order_ref is unique across orders."""
        for r in self:
            ref = (r.monta_order_ref or "").strip()
            if not ref:
                continue
            dup = self.search([("id", "!=", r.id), ("monta_order_ref", "=", ref)], limit=1)
            if dup:
                raise ValidationError(
                    _("Monta Order Ref '%s' already exists on order %s; refs must be unique.") % (ref, dup.order_name)
                )
