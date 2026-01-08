# -*- coding: utf-8 -*-
import json
import logging
import re

from odoo import api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = "account.move"

    monta_renewal_pushed = fields.Boolean(default=False, copy=False)
    monta_renewal_webshop_order_id = fields.Char(copy=False, index=True)
    monta_renewal_last_push = fields.Datetime(copy=False)

    # -------------------------
    # Helpers
    # -------------------------
    def _monta_find_related_sale_orders(self):
        """Try to find related sale.order records from invoice lines and invoice_origin."""
        self.ensure_one()
        SaleOrder = self.env["sale.order"]
        orders = SaleOrder

        # 1) invoice_line_ids -> sale_line_ids -> order_id
        try:
            sol = self.invoice_line_ids.mapped("sale_line_ids")
            orders |= sol.mapped("order_id")
        except Exception:
            pass

        # 2) fallback: invoice_origin may contain SO names
        if not orders and self.invoice_origin:
            names = [n.strip() for n in (self.invoice_origin or "").split(",") if n.strip()]
            if names:
                orders |= SaleOrder.search([("name", "in", names)])

        return orders

    def _monta_is_subscription_sale_order(self, so):
        """Best-effort detection of subscription order across setups."""
        # Some databases have subscription_id, some have is_subscription
        return bool(getattr(so, "subscription_id", False)) or bool(getattr(so, "is_subscription", False))

    def _monta_make_webshop_order_id(self, so):
        """Unique order id per renewal cycle."""
        # Example: SO0234-INV-2026-001 (slashes replaced)
        inv_name = (self.name or "").replace("/", "-")
        so_name = (so.name or "").replace("/", "-")
        return f"{so_name}-{inv_name}"

    def _monta_prepare_renewal_payload(self, so, webshop_order_id):
        """Reuse the sale.order payload generator and override fields for renewal."""
        payload = so._prepare_monta_order_payload()

        # Override OrderId for Monta to make it unique per renewal
        payload["WebshopOrderId"] = webshop_order_id

        # Reference can be invoice ref or payment reference
        payload["Reference"] = self.ref or self.payment_reference or so.client_order_ref or ""

        # WebshopFactuurID should be numeric -> take digits from invoice name
        inv_digits = re.sub(r"\D", "", self.name or "")
        payload["WebshopFactuurID"] = int(inv_digits) if inv_digits else 9999

        return payload

    def _monta_create_status_snapshot(self, so, order_name, monta_order_ref=None, status=None, status_code=None, source="orders", raw=None):
        """Create a monta.order.status record so it appears in Monta → Order Status automatically."""
        Status = self.env["monta.order.status"].sudo()
        account_key = Status._current_account_key() if hasattr(Status, "_current_account_key") else ""

        vals = {
            "monta_account_key": account_key,
            "sale_order_id": so.id,
            "order_name": order_name,
            "monta_order_ref": monta_order_ref or "",
            "status": status or "",
            "status_code": status_code if status_code is not None else 0,
            "source": source,
            "last_sync": fields.Datetime.now(),
            "status_raw": json.dumps(raw or {}, ensure_ascii=False),
        }

        # Upsert by (order_name, account_key)
        domain = [("order_name", "=", order_name)]
        if account_key:
            domain.append(("monta_account_key", "=", account_key))

        rec = Status.search(domain, limit=1)
        if rec:
            rec.write(vals)
            return rec
        return Status.create(vals)

    # -------------------------
    # Main Hook (covers UI + cron)
    # -------------------------
    def _post(self, soft=True):
        res = super()._post(soft=soft)

        moves = self.filtered(lambda m: m.move_type == "out_invoice" and m.state == "posted")
        for move in moves:
            if move.monta_renewal_pushed:
                continue

            sale_orders = move._monta_find_related_sale_orders()
            if not sale_orders:
                continue

            # Only subscription-related SO
            sub_orders = sale_orders.filtered(lambda so: move._monta_is_subscription_sale_order(so))
            if not sub_orders:
                continue

            pushed_any = False

            for so in sub_orders:
                if hasattr(so, "_is_company_allowed") and not so._is_company_allowed():
                    continue
                if hasattr(so, "_is_allowed_instance") and not so._is_allowed_instance():
                    continue

                webshop_order_id = move._monta_make_webshop_order_id(so)

                try:
                    payload = move._monta_prepare_renewal_payload(so, webshop_order_id)

                    # Send to Monta using the same internal request method (keeps logs consistent)
                    status, body = so._monta_request("POST", "/order", payload)

                    if 200 <= status < 300:
                        pushed_any = True
                        move._monta_create_status_snapshot(
                            so=so,
                            order_name=webshop_order_id,
                            monta_order_ref=body.get("OrderRef") or body.get("orderRef") or body.get("id") or "",
                            status="sent",
                            status_code=status,
                            source="orders",
                            raw={"invoice": move.name, "response": body},
                        )
                        _logger.info("[Monta Renewal] Sent renewal %s for invoice %s", webshop_order_id, move.name)
                    else:
                        # Create snapshot anyway so you can see failure in Order Status
                        move._monta_create_status_snapshot(
                            so=so,
                            order_name=webshop_order_id,
                            monta_order_ref="",
                            status="error",
                            status_code=status,
                            source="orders",
                            raw={"invoice": move.name, "response": body},
                        )
                        _logger.warning("[Monta Renewal] Failed renewal %s invoice %s status=%s", webshop_order_id, move.name, status)

                except Exception as e:
                    move._monta_create_status_snapshot(
                        so=so,
                        order_name=webshop_order_id,
                        monta_order_ref="",
                        status="error",
                        status_code=0,
                        source="orders",
                        raw={"invoice": move.name, "exception": str(e)},
                    )
                    _logger.exception("[Monta Renewal] Exception for invoice %s: %s", move.name, e)

            if pushed_any:
                move.write({
                    "monta_renewal_pushed": True,
                    "monta_renewal_webshop_order_id": move._monta_make_webshop_order_id(sub_orders[0]),
                    "monta_renewal_last_push": fields.Datetime.now(),
                })

        return res
