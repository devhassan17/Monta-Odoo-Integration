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
        orders = SaleOrder.browse()  # empty recordset

        # 1) invoice_line_ids -> sale_line_ids -> order_id
        try:
            sol = self.invoice_line_ids.mapped("sale_line_ids")
            orders |= sol.mapped("order_id")
        except Exception:
            _logger.debug("[Monta Renewal] Could not map invoice lines to sale orders", exc_info=True)

        # 2) fallback: invoice_origin may contain SO names
        if not orders and self.invoice_origin:
            names = [n.strip() for n in self.invoice_origin.split(",") if n.strip()]
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
        payload["Reference"] = self.ref or self.payment_reference or getattr(so, "client_order_ref", "") or ""

        # numeric invoice id for Monta (digits from invoice name)
        inv_digits = re.sub(r"\D", "", self.name or "")
        payload["WebshopFactuurID"] = int(inv_digits) if inv_digits else 9999

        return payload

    def _monta_chatter_on_subscription(self, so, title, message_html, success=True):
        """Post a note in the subscription (sale.order) chatter."""
        icon = "✅" if success else "❌"
        body = f"<p><b>{icon} {title}</b></p>{message_html}"
        try:
            so.message_post(
                body=body,
                message_type="comment",
                subtype_xmlid="mail.mt_note",
            )
        except Exception:
            _logger.exception("[Monta Renewal] Failed to post chatter message on %s", so.display_name)

    def _monta_extract_monta_ref(self, body, fallback):
        """Try to read Monta order ref/id from API response body."""
        if isinstance(body, dict):
            for k in ("OrderId", "orderId", "Id", "id", "MontaOrderId", "montaOrderId", "OrderNumber", "orderNumber"):
                v = body.get(k)
                if v:
                    return str(v)
        return fallback

    # -------------------------
    # Manual send hook (from Monta Order Status button)
    # -------------------------
    def _action_send_renewal_to_monta(self, sale_order=None):
        """
        ✅ Called from Monta Order Status 'Send to Monta' button.
        Sends renewal payload for this invoice + given sale_order.
        """
        self.ensure_one()
        if self.move_type != "out_invoice" or self.state != "posted":
            return False

        if not sale_order:
            # fallback discovery
            sale_orders = self._monta_find_related_sale_orders()
            sale_orders = sale_orders.filtered(lambda so: self._monta_is_subscription_sale_order(so))
            sale_order = sale_orders[:1] if sale_orders else False

        if not sale_order:
            return False

        webshop_order_id = self._monta_make_webshop_order_id(sale_order)

        # ensure status row exists (so user can see it)
        self.env["monta.order.status"].upsert_for_renewal(
            sale_order,
            self,
            webshop_order_id,
            status="Not sent",
            status_code=0,
            source="orders",
            monta_order_ref=False,
            status_raw=json.dumps({"note": "Manual send initiated"}, ensure_ascii=False),
        )

        payload = self._monta_prepare_renewal_payload(sale_order, webshop_order_id)
        status, body = sale_order._monta_request("POST", "/order", payload)

        if 200 <= status < 300:
            monta_ref = self._monta_extract_monta_ref(body, webshop_order_id)

            # update snapshot => hides button
            self.env["monta.order.status"].upsert_for_renewal(
                sale_order,
                self,
                webshop_order_id,
                status="Sent",
                status_code=status,
                source="orders",
                monta_order_ref=monta_ref,
                status_raw=json.dumps(body or {}, ensure_ascii=False),
                last_sync=fields.Datetime.now(),
            )

            self._logger_info_success(sale_order, webshop_order_id, status, body)
            self.write(
                {
                    "monta_renewal_pushed": True,
                    "monta_renewal_webshop_order_id": webshop_order_id,
                    "monta_renewal_last_push": fields.Datetime.now(),
                }
            )
            return True

        # failure => keep monta_order_ref empty so button stays visible
        self.env["monta.order.status"].upsert_for_renewal(
            sale_order,
            self,
            webshop_order_id,
            status="Error",
            status_code=status,
            source="orders",
            monta_order_ref=False,
            status_raw=json.dumps(body or {}, ensure_ascii=False),
            last_sync=fields.Datetime.now(),
        )
        self._logger_info_failure(sale_order, webshop_order_id, status, body)
        return False

    # -------------------------
    # Main Hook (covers UI + cron)
    # -------------------------
    def _post(self, soft=True):
        res = super()._post(soft=soft)

        moves = self.filtered(lambda m: m.move_type == "out_invoice" and m.state == "posted")
        for move in moves:
            # Even if previously pushed, we still want snapshot rows for visibility
            sale_orders = move._monta_find_related_sale_orders()
            if not sale_orders:
                continue

            sub_orders = sale_orders.filtered(lambda so: move._monta_is_subscription_sale_order(so))
            if not sub_orders:
                continue

            pushed_any = False
            first_webshop_order_id = False

            for so in sub_orders:
                if hasattr(so, "_is_company_allowed") and not so._is_company_allowed():
                    continue
                if hasattr(so, "_is_allowed_instance") and not so._is_allowed_instance():
                    continue

                webshop_order_id = move._monta_make_webshop_order_id(so)
                if not first_webshop_order_id:
                    first_webshop_order_id = webshop_order_id

                # ✅ Always create/update snapshot row first (so it appears in Monta Order Status page)
                self.env["monta.order.status"].upsert_for_renewal(
                    so,
                    move,
                    webshop_order_id,
                    status="Not sent" if not move.monta_renewal_pushed else "Sent",
                    status_code=0,
                    source="orders",
                    monta_order_ref=False if not move.monta_renewal_pushed else (move.monta_renewal_webshop_order_id or webshop_order_id),
                    status_raw=json.dumps({"note": "Auto-created from invoice post"}, ensure_ascii=False),
                    last_sync=fields.Datetime.now(),
                )

                # If already pushed (and not forced), do not re-send here
                if move.monta_renewal_pushed and not move.env.context.get("force_send_to_monta"):
                    continue

                try:
                    payload = move._monta_prepare_renewal_payload(so, webshop_order_id)
                    status, body = so._monta_request("POST", "/order", payload)

                    if 200 <= status < 300:
                        pushed_any = True
                        monta_ref = move._monta_extract_monta_ref(body, webshop_order_id)

                        # ✅ Update snapshot -> hides button
                        self.env["monta.order.status"].upsert_for_renewal(
                            so,
                            move,
                            webshop_order_id,
                            status="Sent",
                            status_code=status,
                            source="orders",
                            monta_order_ref=monta_ref,
                            status_raw=json.dumps(body or {}, ensure_ascii=False),
                            last_sync=fields.Datetime.now(),
                        )

                        move._logger_info_success(so, webshop_order_id, status, body)
                        _logger.info("[Monta Renewal] Sent renewal %s for invoice %s", webshop_order_id, move.name)

                    else:
                        # ✅ Keep monta_order_ref empty -> button stays visible
                        self.env["monta.order.status"].upsert_for_renewal(
                            so,
                            move,
                            webshop_order_id,
                            status="Error",
                            status_code=status,
                            source="orders",
                            monta_order_ref=False,
                            status_raw=json.dumps(body or {}, ensure_ascii=False),
                            last_sync=fields.Datetime.now(),
                        )
                        move._logger_info_failure(so, webshop_order_id, status, body)
                        _logger.warning("[Monta Renewal] Failed renewal %s invoice %s status=%s", webshop_order_id, move.name, status)

                except Exception as e:
                    self.env["monta.order.status"].upsert_for_renewal(
                        so,
                        move,
                        webshop_order_id,
                        status="Error",
                        status_code=0,
                        source="orders",
                        monta_order_ref=False,
                        status_raw=json.dumps({"exception": str(e)}, ensure_ascii=False),
                        last_sync=fields.Datetime.now(),
                    )
                    move._logger_info_exception(so, webshop_order_id, e)
                    _logger.exception("[Monta Renewal] Exception for invoice %s: %s", move.name, e)

            if pushed_any:
                move.write(
                    {
                        "monta_renewal_pushed": True,
                        "monta_renewal_webshop_order_id": first_webshop_order_id
                        or move._monta_make_webshop_order_id(sub_orders[0]),
                        "monta_renewal_last_push": fields.Datetime.now(),
                    }
                )

        return res

    # -------------------------
    # Chatter + debug helpers
    # -------------------------
    def _logger_info_success(self, so, webshop_order_id, status, body):
        self.ensure_one()
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
