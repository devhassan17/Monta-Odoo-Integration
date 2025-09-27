# -*- coding: utf-8 -*-
import hashlib
import logging
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


def _hash_account(base: str, user: str) -> str:
    """Stable key for a Monta tenant. Uses base URL + username."""
    b = (base or "").strip().lower().rstrip("/")
    u = (user or "").strip().lower()
    raw = f"{b}|{u}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class MontaOrderStatus(models.Model):
    _name = "monta.order.status"
    _description = "Monta Order Status snapshot"
    _order = "last_sync desc, id desc"

    # Tenant / account scoping
    monta_account_key = fields.Char(string="Monta Account Key", index=True)
    is_current_account = fields.Boolean(
        string="Current Monta Account",
        compute="_compute_is_current_account",
        store=True,
        index=True,
    )

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

    # Status (stored)
    status = fields.Char(string="Order Status")
    status_code = fields.Integer(string="Status Code")

    source = fields.Selection(
        selection=[
            ("orders", "orders"),
            ("shipments", "shipments"),
            ("orderevents", "orderevents"),
            ("events", "events"),
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

    # Raw payload (optional)
    status_raw = fields.Text(string="Raw Status (JSON)")

    # Fast flag: available on Monta (non-empty ref)
    on_monta = fields.Boolean(
        string="Available on Monta",
        compute="_compute_on_monta",
        store=True,
        index=True,
    )

    _sql_constraints = [
        # One snapshot per (order_name, tenant)
        ("monta_order_unique_per_account",
         "unique(order_name, monta_account_key)",
         "Monta order snapshot must be unique per account and order."),
    ]

    # ---------- tenant helpers ----------
    @api.model
    def _current_account_key(self) -> str:
        ICP = self.env["ir.config_parameter"].sudo()
        base = (ICP.get_param("monta.base_url") or ICP.get_param("monta.api.base_url") or "").strip()
        user = (ICP.get_param("monta.username") or "").strip()
        return _hash_account(base, user) if (base and user) else ""

    @api.depends("monta_account_key")
    def _compute_is_current_account(self):
        cur = self._current_account_key()
        for r in self:
            r.is_current_account = bool(cur) and (r.monta_account_key == cur)

    # ---------- computes ----------
    @api.depends("monta_order_ref")
    def _compute_on_monta(self):
        for r in self:
            r.on_monta = bool((r.monta_order_ref or "").strip())

    # ---------- helpers ----------
    @api.model
    def _safe_int(self, v):
        if v in (False, None, ""):
            return 0
        try:
            return int(str(v).strip())
        except Exception:
            return 0

    @api.model
    def _normalize_vals(self, vals):
        """Map incoming keys and ignore falsy to avoid overwriting with blanks."""
        out = {}

        ref = vals.get("monta_order_ref")
        if ref is not None and str(ref).strip() != "":
            out["monta_order_ref"] = str(ref).strip()

        status = vals.get("status") or vals.get("order_status")
        if status is not None:
            out["status"] = status

        sc = vals.get("status_code")
        if sc is None:
            sc = vals.get("monta_status_code")
        if sc is not None:
            out["status_code"] = self._safe_int(sc)

        src = vals.get("source") or vals.get("monta_status_source")
        if src is not None:
            allowed = [opt[0] for opt in (self._fields["source"].selection or [])]
            if src in allowed:
                out["source"] = src

        if vals.get("delivery_message") is not None:
            out["delivery_message"] = vals.get("delivery_message")

        track = vals.get("track_trace") or vals.get("track_trace_url")
        if track is not None:
            out["track_trace"] = track

        if vals.get("delivery_date") is not None:
            out["delivery_date"] = vals.get("delivery_date")

        out["last_sync"] = vals.get("last_sync") or fields.Datetime.now()

        if vals.get("status_raw") is not None:
            out["status_raw"] = vals.get("status_raw")

        return out

    # ---------- public API (tenant-aware) ----------
    @api.model
    def upsert_for_order(self, so, **vals):
        """
        Create/update one snapshot row per (sale.order, current tenant).
        """
        if not so or not so.id:
            raise ValueError("upsert_for_order requires a valid sale.order")

        account_key = self._current_account_key()
        if not account_key:
            raise ValidationError(_("Monta credentials (base URL/username) are not configured."))

        base_vals = self._normalize_vals(vals)
        base_vals.update({
            "sale_order_id": so.id,
            "order_name": so.name,
            "monta_account_key": account_key,
        })

        # block duplicate (same ref used on another order in THIS tenant)
        incoming_ref = base_vals.get("monta_order_ref")
        if incoming_ref:
            clash = self.sudo().search([
                ("monta_account_key", "=", account_key),
                ("monta_order_ref", "=", incoming_ref),
                ("order_name", "!=", so.name),
            ], limit=1)
            if clash:
                _logger.warning(
                    "Blocked duplicate Monta ref %s for order %s in this account (already used by %s).",
                    incoming_ref, so.name, clash.order_name
                )
                base_vals.pop("monta_order_ref", None)

        rec = self.sudo().search([
            ("order_name", "=", so.name),
            ("monta_account_key", "=", account_key),
        ], limit=1)

        if rec:
            rec.write(base_vals)
            return rec
        return self.sudo().create(base_vals)

    # ---------- constraints ----------
    @api.constrains("monta_order_ref", "order_name", "monta_account_key")
    def _check_unique_nonempty_monta_ref(self):
        """Ensure a non-empty ref is unique within the same tenant."""
        for r in self:
            ref = (r.monta_order_ref or "").strip()
            if not ref:
                continue
            dup = self.search([
                ("id", "!=", r.id),
                ("monta_account_key", "=", r.monta_account_key),
                ("monta_order_ref", "=", ref),
            ], limit=1)
            if dup:
                raise ValidationError(
                    _("Monta Order Ref '%s' already exists on order %s in this account.") % (ref, dup.order_name)
                )


# ---- Optional SO helpers (unchanged) ----
class SaleOrder(models.Model):
    _inherit = "sale.order"

    monta_status = fields.Char(string="Monta Status", copy=False, index=True)
    monta_status_code = fields.Char(string="Monta Status Code", copy=False)
    monta_status_source = fields.Selection(
        selection=[("shipments", "Shipments"), ("orderevents", "Order Events"), ("orders", "Orders Header")],
        string="Monta Status Source",
        copy=False,
    )
    monta_track_trace = fields.Char(string="Monta Track & Trace", copy=False)
    monta_last_sync = fields.Datetime(string="Monta Last Sync", copy=False)

    def action_open_monta_order_status(self):
        self.ensure_one()
        action = self.env.ref("Monta-Odoo-Integration.action_monta_order_status").read()[0]
        action["domain"] = [("order_name", "=", self.name)]
        action["context"] = {"search_default_order_name": self.name}
        return action
