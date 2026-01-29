# -*- coding: utf-8 -*-
import json
import logging

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

    def _monta_is_initial_subscription_invoice(self, so):
        """
        ✅ Detect initial/first invoice of a subscription sale order.
        If there is NO other posted customer invoice linked to the same sale.order
        (via invoice lines -> sale_line_ids -> order_id), then this invoice is initial.
        """
        self.ensure_one()

        # If this isn't subscription order, it's not "initial subscription invoice"
        if not self._monta_is_subscription_sale_order(so):
            return False

        # Any other posted invoice linked to this SO?
        domain = [
            ("move_type", "=", "out_invoice"),
            ("state", "=", "posted"),
            ("id", "!=", self.id),
            ("invoice_line_ids.sale_line_ids.order_id", "=", so.id),
        ]
        prev = self.search(domain, limit=1)
        return not bool(prev)

    def _monta_make_webshop_order_id(self, so):
        """
        ✅ Stable unique id per invoice RECORD (prevents duplicates when invoice number changes).
        - self.name (INV/2026/xxx) can change on cancel/repost/resequence.
        - self.id NEVER changes for the same invoice record.
        - Next month renewal creates a NEW invoice record => NEW id => will send normally.
        """
        self.ensure_one()

        # If already generated once, always reuse it
        if self.monta_renewal_webshop_order_id:
            return self.monta_renewal_webshop_order_id

        so_name = (so.name or "").replace("/", "-")
        webshop_order_id = f"{so_name}-INV{self.id}"

        # store it so it stays stable forever for this invoice record
        self.write({"monta_renewal_webshop_order_id": webshop_order_id})
        return webshop_order_id

    def _monta_prepare_renewal_payload(self, so, webshop_order_id):
        """
        Reuse existing sale.order payload generator and override key fields for renewal.
        This relies on your existing sale.order integration methods.
        """
        payload = so._prepare_monta_order_payload()

        # ✅ stable unique order id for this invoice record
        payload["WebshopOrderId"] = webshop_order_id

        # ✅ Human-readable reference in Monta (OK if it changes; WebshopOrderId is the real unique key)
        payload["Reference"] = (self.name or "").strip() or (
            self.ref or self.payment_reference or getattr(so, "client_order_ref", "") or ""
        )

        # ✅ Keep FactuurID stable too (do NOT derive from invoice name digits)
        payload["WebshopFactuurID"] = int(self.id) if self.id else 9999

        return payload

    def _monta_is_duplicate_exists_error(self, status, body):
        """Detect Monta duplicate errors (same WebshopOrderId already exists)."""
        if status != 400 or not isinstance(body, dict):
            return False
        reasons = body.get("OrderInvalidReasons") or []
        for r in reasons:
            msg = (r or {}).get("Message") or ""
            if "already exists" in msg.lower():
                return True
        return False

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
            for k in (
                "OrderId",
                "orderId",
                "Id",
                "id",
                "MontaOrderId",
                "montaOrderId",
                "OrderNumber",
                "orderNumber",
            ):
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
            sale_orders = self._monta_find_related_sale_orders()
            sale_orders = sale_orders.filtered(lambda so: self._monta_is_subscription_sale_order(so))
            sale_order = sale_orders[:1] if sale_orders else False

        if not sale_order:
            return False

        webshop_order_id = self._monta_make_webshop_order_id(sale_order)

        # ✅ Hard idempotency guard (prevents repeated manual clicks / retries)
        existing = self.env["monta.order.status"].sudo().search(
            [
                ("order_name", "=", webshop_order_id),
                ("order_kind", "=", "renewal"),
                ("invoice_id", "=", self.id),
                ("status", "in", ["Sent", "sent"]),
            ],
            limit=1,
        )
        if existing and not self.env.context.get("force_send_to_monta"):
            _logger.info("[Monta Renewal] Skip manual resend; already sent: %s", webshop_order_id)
            return True

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

        # ✅ If Monta says it already exists, treat as success (idempotent)
        if self._monta_is_duplicate_exists_error(status, body):
            monta_ref = self._monta_extract_monta_ref(body, webshop_order_id)
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
            self.write(
                {
                    "monta_renewal_pushed": True,
                    "monta_renewal_webshop_order_id": webshop_order_id,
                    "monta_renewal_last_push": fields.Datetime.now(),
                }
            )
            self._monta_chatter_on_subscription(
                sale_order,
                "Monta Renewal Already Exists",
                f"<p><b>Invoice:</b> {self.name}</p><p><b>Monta Order ID:</b> {webshop_order_id}</p>",
                success=True,
            )
            return True

        if 200 <= status < 300:
            monta_ref = self._monta_extract_monta_ref(body, webshop_order_id)

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
            # ✅ If already pushed for THIS invoice record, never send again (unless forced)
            if move.monta_renewal_pushed and not move.env.context.get("force_send_to_monta"):
                continue

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

                # ✅ IMPORTANT FIX:
                # If this is the INITIAL invoice of a newly created subscription,
                # DO NOT push renewal/invoice to Monta (delivery is already handled elsewhere).
                if not move.env.context.get("force_send_to_monta"):
                    if move._monta_is_initial_subscription_invoice(so):
                        _logger.info(
                            "[Monta Renewal] Skip initial subscription invoice %s for SO %s",
                            move.name,
                            so.name,
                        )
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

                # ✅ Extra safety: if snapshot already says Sent for this renewal, don't resend.
                if not move.env.context.get("force_send_to_monta"):
                    already_sent = self.env["monta.order.status"].sudo().search(
                        [
                            ("order_name", "=", webshop_order_id),
                            ("order_kind", "=", "renewal"),
                            ("invoice_id", "=", move.id),
                            ("status", "in", ["Sent", "sent"]),
                        ],
                        limit=1,
                    )
                    if already_sent:
                        continue

                try:
                    payload = move._monta_prepare_renewal_payload(so, webshop_order_id)
                    status, body = so._monta_request("POST", "/order", payload)

                    # ✅ Idempotency: Monta says order id already exists
                    if move._monta_is_duplicate_exists_error(status, body):
                        pushed_any = True
                        monta_ref = move._monta_extract_monta_ref(body, webshop_order_id)
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
                        move.write(
                            {
                                "monta_renewal_pushed": True,
                                "monta_renewal_webshop_order_id": webshop_order_id,
                                "monta_renewal_last_push": fields.Datetime.now(),
                            }
                        )
                        continue

                    if 200 <= status < 300:
                        pushed_any = True
                        monta_ref = move._monta_extract_monta_ref(body, webshop_order_id)

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
                        _logger.warning(
                            "[Monta Renewal] Failed renewal %s invoice %s status=%s",
                            webshop_order_id,
                            move.name,
                            status,
                        )

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
                        "monta_renewal_webshop_order_id": first_webshop_order_id or move.monta_renewal_webshop_order_id,
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
