# -*- coding: utf-8 -*-
import hashlib

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


def _hash_account(base: str, user: str) -> str:
    b = (base or "").strip().lower().rstrip("/")
    u = (user or "").strip().lower()
    return hashlib.sha1(f"{b}|{u}".encode("utf-8")).hexdigest()


class MontaOrderStatus(models.Model):
    _name = "monta.order.status"
    _description = "Monta Order Status snapshot"
    _order = "last_sync desc, id desc"

    monta_account_key = fields.Char(string="Monta Account Key", index=True)
    is_current_account = fields.Boolean(
        string="Current Monta Account",
        compute="_compute_is_current_account",
        store=True,
        index=True,
    )

    sale_order_id = fields.Many2one(
        "sale.order",
        string="Sales Order",
        index=True,
        ondelete="cascade",
        required=True,
    )
    order_name = fields.Char(string="Order Name", index=True, required=True)
    monta_order_ref = fields.Char(string="Monta Order Ref", index=True)

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

    delivery_message = fields.Char(string="Delivery Message")
    track_trace = fields.Char(string="Track & Trace URL")
    delivery_date = fields.Date(string="Delivery Date")
    last_sync = fields.Datetime(string="Last Sync (UTC)", default=fields.Datetime.now, index=True)

    status_raw = fields.Text(string="Raw Status (JSON)")
    on_monta = fields.Boolean(string="Available on Monta", compute="_compute_on_monta", store=True, index=True)

    manual_send_available = fields.Boolean(
        string="Can Send to Monta",
        compute="_compute_manual_send_available",
        store=False
    )

    _sql_constraints = [
        (
            "monta_order_unique_per_account",
            "unique(order_name, monta_account_key)",
            "Monta order snapshot must be unique per account and order.",
        ),
    ]

    @api.depends("monta_order_ref")
    def _compute_manual_send_available(self):
        for rec in self:
            rec.manual_send_available = not bool((rec.monta_order_ref or "").strip())

    def action_manual_send_to_monta(self):
        for rec in self:
            if rec.sale_order_id and not rec.monta_order_ref:
                rec.sale_order_id._monta_create()
                rec.sale_order_id.message_post(
                    body="Sent manually to Monta.",
                    message_type="comment"
                )

    @api.model
    def _has_monta_account_key_column(self) -> bool:
        cache_name = "_monta_has_account_key_col"
        cached = getattr(self.env.registry, cache_name, None)
        if cached is not None:
            return cached

        self.env.cr.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
            (self._table, "monta_account_key"),
        )
        has_col = bool(self.env.cr.fetchone())
        setattr(self.env.registry, cache_name, has_col)
        return has_col

    @api.model
    def _current_account_key(self) -> str:
        cfg = self.env["monta.config"].sudo().get_singleton()
        base = (cfg.base_url or "").strip()
        user = (cfg.username or "").strip()
        return _hash_account(base, user) if (base and user) else ""

    @api.depends("monta_account_key")
    def _compute_is_current_account(self):
        cur = self._current_account_key()
        for r in self:
            r.is_current_account = bool(cur) and (r.monta_account_key == cur)

    @api.depends("monta_order_ref")
    def _compute_on_monta(self):
        for r in self:
            r.on_monta = bool((r.monta_order_ref or "").strip())

    @api.model
    def upsert_for_order(self, so, **vals):
        if not so or not so.id:
            raise ValueError("upsert_for_order requires a valid sale.order")

        account_key = self._current_account_key()
        if not account_key:
            raise ValidationError(_("Monta credentials are not configured."))

        base_vals = {
            "sale_order_id": so.id,
            "order_name": so.name,
            "last_sync": vals.get("last_sync") or fields.Datetime.now(),
        }

        for k in (
            "status",
            "status_code",
            "source",
            "delivery_message",
            "track_trace",
            "delivery_date",
            "status_raw",
            "monta_order_ref",
        ):
            if k in vals and vals[k] is not None:
                base_vals[k] = vals[k]

        domain = [("order_name", "=", so.name)]
        if self._has_monta_account_key_column():
            base_vals["monta_account_key"] = account_key
            domain.append(("monta_account_key", "=", account_key))

        rec = self.sudo().search(domain, limit=1)
        if rec:
            rec.write(base_vals)
            return rec

        return self.sudo().create(base_vals)
