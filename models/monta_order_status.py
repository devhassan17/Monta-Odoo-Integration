# -*- coding: utf-8 -*-
import hashlib
import json
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

    # ✅ NEW: distinguish normal SO vs subscription renewal invoice
    order_kind = fields.Selection(
        selection=[
            ("sale", "Sale Order"),
            ("renewal", "Subscription Renewal"),
        ],
        string="Kind",
        default="sale",
        required=True,
        index=True,
    )

    sale_order_id = fields.Many2one(
        "sale.order",
        string="Sales Order",
        index=True,
        ondelete="cascade",
        required=True,  # keep required, renewal row will still point to related SO
    )

    # ✅ NEW: link renewal invoice
    invoice_id = fields.Many2one(
        "account.move",
        string="Invoice",
        index=True,
        ondelete="set null",
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

    _sql_constraints = [
        (
            "monta_order_unique_per_account",
            "unique(order_name, monta_account_key)",
            "Monta order snapshot must be unique per account and order.",
        ),
    ]

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

    @api.model
    def _base_upsert_domain_vals(self, order_name: str):
        account_key = self._current_account_key()
        if not account_key:
            raise ValidationError(_("Monta credentials are not configured."))

        domain = [("order_name", "=", order_name)]
        base_vals = {"order_name": order_name, "last_sync": fields.Datetime.now()}

        if self._has_monta_account_key_column():
            base_vals["monta_account_key"] = account_key
            domain.append(("monta_account_key", "=", account_key))

        return domain, base_vals

    @api.model
    def upsert_for_order(self, so, **vals):
        """Existing behavior: snapshot row for normal sale order."""
        if not so or not so.id:
            raise ValueError("upsert_for_order requires a valid sale.order")

        domain, base_vals = self._base_upsert_domain_vals(so.name)

        base_vals.update(
            {
                "sale_order_id": so.id,
                "order_kind": "sale",
                "invoice_id": False,
                "last_sync": vals.get("last_sync") or fields.Datetime.now(),
            }
        )

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

        rec = self.sudo().search(domain, limit=1)
        if rec:
            rec.write(base_vals)
            return rec

        return self.sudo().create(base_vals)

    @api.model
    def upsert_for_renewal(self, so, invoice, webshop_order_id: str, **vals):
        """✅ NEW: snapshot row for subscription renewal invoice."""
        if not so or not so.id:
            raise ValueError("upsert_for_renewal requires a valid sale.order")
        if not invoice or not invoice.id:
            raise ValueError("upsert_for_renewal requires a valid account.move")
        if not webshop_order_id:
            raise ValueError("upsert_for_renewal requires webshop_order_id")

        domain, base_vals = self._base_upsert_domain_vals(webshop_order_id)

        base_vals.update(
            {
                "sale_order_id": so.id,
                "invoice_id": invoice.id,
                "order_kind": "renewal",
                "last_sync": vals.get("last_sync") or fields.Datetime.now(),
            }
        )

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

        rec = self.sudo().search(domain, limit=1)
        if rec:
            rec.write(base_vals)
            return rec

        return self.sudo().create(base_vals)

    def action_manual_send_to_monta(self):
        """
        ✅ Updated:
        - If row is renewal (invoice_id exists): send renewal invoice to Monta.
        - Else: send sale order as before.
        """
        for record in self:
            sale_order = record.sale_order_id
            if not sale_order:
                continue

            try:
                if record.order_kind == "renewal" and record.invoice_id:
                    # send renewal invoice payload
                    record.invoice_id.with_context(force_send_to_monta=True)._action_send_renewal_to_monta(
                        sale_order=sale_order
                    )
                    sale_order.message_post(body="✅ Renewal invoice sent to Monta manually from Monta Order Status.")
                else:
                    # normal sale order send
                    sale_order.with_context(force_send_to_monta=True)._action_send_to_monta()
                    sale_order.message_post(body="✅ Order sent to Monta manually from Monta Order Status.")

            except Exception as e:
                sale_order.message_post(body=f"❌ Failed to send to Monta manually: {e}")

        return True
