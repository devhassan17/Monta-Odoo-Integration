# -*- coding: utf-8 -*-
import json
import logging
import re

from odoo import fields, models

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
        return bool(getattr(so, "subscription_id", False)) or bool(getattr(so, "is_subscription", False))

    def _monta_make_webshop_order_id(self, so):
        """Unique order id per renewal cycle."""
        inv_name = (self.name or "").replace("/", "-")
        so_name = (so.name or "").replace("/", "-")
        return f"{so_name}-{inv_name}"

    def _monta_prepare_renewal_payload(self, so, webshop_order_id):
        """
        Reuse existing sale.order payload generator and override key fields for renewal.
        This relies on your existing sale.order integration methods.
        """
        payload = so._prepare_monta_order_payload()

        # unique order id for each renewal
        payload["WebshopOrderId"] = webshop_order_id

        # set reference
        payload["Reference"] = self.ref or self.payment_reference or so.client_order_ref or ""

        # numeric invoice id for Monta (digits from invoice name)
        inv_digits = re.sub(r"\D", "", self.name or "")
        payload["WebshopFactuurID"] = int(inv_digits) if inv_digits else 9999

        return payload

    def _monta_chatter_on_subscription(self, so, title, message_html, success=True):
        """Post a note in the subscription (sale.order) chatter."""
        icon = "✅" if success else "❌"
        body = f"""
            <p><b>{icon} {title}</b></p>
            {message_html}
        """
        try:
            so.message_post(
                body=body,
                message_type="comment",
                subtype_xmlid="mail.mt_note",
            )
        except Exception:
            _logger.exception("[Monta Renewal] Failed to post chatter message on %s", so.name)

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

            # only subscription-related orders
            sub_orders = sale_orders.filtered(lambda so: move._monta_is_subscription_sale_order(so))
            if not sub_orders:
                continue

            pushed_any = False

            for so in sub_orders:
                # keep your existing company/instance guards if present
                if hasattr(so, "_is_company_allowed") and not so._is_company_allowed():
                    continue
                if hasattr(so, "_is_allowed_instance") and not so._is_allowed_instance():
                    continue

                webshop_order_id = move._monta_make_webshop_order_id(so)

                try:
                    payload = move._monta_prepare_renewal_payload(so, webshop_order_id)

                    # Use your existing request function so it logs in Monta request logs
                    status, body = so._monta_request("POST", "/order", payload)

                    if 200 <= status < 300:
                        pushed_any = True
                        move._logger_info_success(so, webshop_order_id, status, body)
                        _logger.info(
                            "[Monta Renewal] Sent renewal %s for invoice %s",
                            webshop_order_id,
                            move.name,
                        )
                    else:
                        move._logger_info_failure(so, webshop_order_id, status, body)
                        _logger.warning(
                            "[Monta Renewal] Failed renewal %s invoice %s status=%s",
                            webshop_order_id,
                            move.name,
                            status,
                        )

                except Exception as e:
                    move._logger_info_exception(so, webshop_order_id, e)
                    _logger.exception("[Monta Renewal] Exception for invoice %s: %s", move.name, e)

            if pushed_any:
                move.write({
                    "monta_renewal_pushed": True,
                    "monta_renewal_webshop_order_id": move._monta_make_webshop_order_id(sub_orders[0]),
                    "monta_renewal_last_push": fields.Datetime.now(),
                })

        return res

    # -------------------------
    # Chatter + debug helpers
    # -------------------------
    def _logger_info_success(self, so, webshop_order_id, status, body):
        self.ensure_one()
        # chatter
        self._monta_chatter_on_subscription(
            so,
            "Monta Renewal Sent",
            f"""
            <p><b>Invoice:</b> {self.name}</p>
            <p><b>Monta Order ID:</b> {webshop_order_id}</p>
            <p><b>API Status:</b> {status}</p>
            """,
            success=True,
        )

    def _logger_info_failure(self, so, webshop_order_id, status, body):
        self.ensure_one()
        # chatter
        self._monta_chatter_on_subscription(
            so,
            "Monta Renewal Failed",
            f"""
            <p><b>Invoice:</b> {self.name}</p>
            <p><b>Monta Order ID:</b> {webshop_order_id}</p>
            <p><b>API Status:</b> {status}</p>
            <p><b>Response:</b> <pre>{json.dumps(body or {}, ensure_ascii=False, indent=2)}</pre></p>
            """,
            success=False,
        )

    def _logger_info_exception(self, so, webshop_order_id, exc):
        self.ensure_one()
        self._monta_chatter_on_subscription(
            so,
            "Monta Renewal Error",
            f"""
            <p><b>Invoice:</b> {self.name}</p>
            <p><b>Monta Order ID:</b> {webshop_order_id}</p>
            <p><b>Error:</b> {str(exc)}</p>
            """,
            success=False,
        )
