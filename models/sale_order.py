# -*- coding: utf-8 -*-
import json
import logging
import re
from collections import defaultdict

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

from ..services.monta_client import MontaClient
from ..utils.address import split_street
from ..utils.pack import expand_to_leaf_components
from ..utils.sku import resolve_sku

_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    _inherit = "sale.order"

    monta_order_id = fields.Char(copy=False, index=True)
    monta_sync_state = fields.Selection(
        [
            ("draft", "Draft"),
            ("sent", "Sent"),
            ("updated", "Updated"),
            ("cancelled", "Cancelled"),
            ("error", "Error"),
        ],
        default="draft",
        copy=False,
    )
    monta_last_push = fields.Datetime(copy=False)
    monta_needs_sync = fields.Boolean(default=False, copy=False)
    monta_retry_count = fields.Integer(default=0, copy=False)

    # Tracking & Status related
    monta_status_ids = fields.One2many(
        "monta.order.status", "sale_order_id", string="Monta Status History"
    )
    monta_tracking_url = fields.Char(
        string="Monta Tracking URL", compute="_compute_monta_tracking_url", store=False
    )

    @api.depends("monta_status_ids.track_trace")
    def _compute_monta_tracking_url(self):
        for order in self:
            # Get latest tracking URL from all related status snapshots
            latest = order.monta_status_ids.filtered(lambda s: s.track_trace).sorted("last_sync", reverse=True)[:1]
            order.monta_tracking_url = latest.track_trace if latest else False

    # ---------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------
    def _split_street(self, street, street2=""):
        return split_street(street, street2)

    def _should_push_now(self, min_gap_seconds=2):
        if not self.monta_last_push:
            return True
        delta = fields.Datetime.now() - self.monta_last_push
        try:
            return delta.total_seconds() >= min_gap_seconds
        except Exception:
            return True

    def _monta_config(self):
        return self.env["monta.config"].sudo().get_for_company(self.company_id)

    def _is_company_allowed(self):
        cfg = self._monta_config()
        if not cfg:
            _logger.warning("[Monta Guard] Company not allowed or config missing for %s", self.company_id.display_name)
            return False
        return True

    def _is_allowed_instance(self):
        cfg = self._monta_config()
        if not cfg:
            return False

        ICP = self.env["ir.config_parameter"].sudo()
        web_url = (ICP.get_param("web.base.url") or "").strip().rstrip("/") + "/"
        allowed_conf = (cfg.allowed_base_urls or "").strip()
        if not allowed_conf:
            return True

        allowed_list = [u.strip().rstrip("/") + "/" for u in allowed_conf.split(",") if u.strip()]
        allowed_lower = {a.lower() for a in allowed_list}
        ok = web_url.lower() in allowed_lower

        if not ok:
            _logger.warning("[Monta Guard] Blocked. web.base.url=%s allowed_list=%s", web_url, allowed_list)
            self._create_monta_log(
                {"guard": {"web_base_url": web_url, "allowed_list": allowed_list, "blocked": True}},
                level="info",
                tag="Monta Guard",
                console_summary="[Monta Guard] blocked by instance URL",
            )
        return ok

    def _is_monta_enabled_for_order(self):
        self.ensure_one()
        cfg = self._monta_config()
        if not cfg:
            return False

        # Filter by warehouse
        if cfg.x_monta_warehouse_ids and self.warehouse_id not in cfg.x_monta_warehouse_ids:
            _logger.info("[Monta Filter] Skipping %s: warehouse %s not in allowed list", self.name, self.warehouse_id.name)
            return False

        # Filter by shipping method (carrier / route)
        # Check if the carrier has a 'Monta' route assigned (e.g. via Odoo Studio field x_route_ids)
        is_allowed_route = False
        if self.carrier_id:
            # Safely check for 'Monta' in routes (common Studio field name: x_route_ids)
            if hasattr(self.carrier_id, "x_route_ids"):
                if any(r.name == "Monta" for r in self.carrier_id.x_route_ids):
                    is_allowed_route = True

        if not is_allowed_route:
            # Fallback for manual global whitelist in Monta Config
            if cfg.x_monta_route_ids and self.carrier_id not in cfg.x_monta_route_ids:
                carrier_name = self.carrier_id.name or "None/Empty"
                _logger.info("[Monta Filter] Skipping %s: shipping method '%s' not allowed by route or global config", self.name, carrier_name)
                return False

        return True

    def _create_monta_log(self, payload, level="info", tag="Monta API", console_summary=None):
        self.ensure_one()
        valid_level = "info" if level == "warning" else level

        self.env["monta.sale.log"].sudo().create(
            {
                "sale_order_id": self.id,
                "log_data": json.dumps(payload, indent=2, default=str, ensure_ascii=False),
                "level": valid_level,
                "name": f"{tag} {self.name} - {valid_level}",
            }
        )
        (_logger.info if valid_level == "info" else _logger.error)(f"[{tag}] {console_summary or self.name}")

    # ---------------------------------------------------------
    # Payload prep
    # ---------------------------------------------------------
    def _prepare_monta_lines(self):
        from math import isfinite

        sku_qty = defaultdict(float)
        missing = []

        for l in self.order_line:
            p = l.product_id
            if not p:
                continue

            # Skip subscription products per user request
            name = (p.name or "").upper()
            display_name = (p.display_name or "").upper()
            if "(SUBSCRIPTION)" in name or "(SUBSCRIPTION)" in display_name:
                continue
            if hasattr(p, "recurring_invoice") and p.recurring_invoice:
                continue

            qty = float(l.product_uom_qty or 0.0)
            if qty <= 0:
                continue

            leaves = expand_to_leaf_components(self.env, self.company_id.id, p, qty)
            if not leaves:
                # If it's not a subscription, but we can't expand it, it might be a missing BoM
                # But we only log error if it's not a subscription
                missing.append(f"'{p.display_name}' has no resolvable components.")
                continue

            for comp, q in leaves:
                sku, _src = resolve_sku(comp, env=self.env, allow_synthetic=False)
                if not sku:
                    missing.append(f"Component '{comp.display_name}' is missing a real SKU.")
                    continue

                try:
                    qv = float(q or 0.0)
                    if not isfinite(qv):
                        qv = 0.0
                except Exception:
                    qv = 0.0

                sku_qty[sku] += qv

        if missing:
            self._create_monta_log(
                {"missing_skus": missing},
                level="error",
                tag="Monta SKU check",
                console_summary=f"[Monta SKU check] {len(missing)} missing",
            )
            # We no longer raise ValidationError here to avoid crashing background writes.
            # Callers like action_confirm will handle raising it if needed.
        
        lines = [{"Sku": sku, "OrderedQuantity": int(q)} for sku, q in sku_qty.items() if int(q) > 0]
        return lines, missing

    def _prepare_monta_order_payload(self):
        self.ensure_one()
        cfg = self._monta_config()
        if not cfg:
            raise ValidationError("Monta Configuration missing or company not allowed.")

        partner = self.partner_id
        street, house_number, house_suffix = self._split_street(partner.street or "", partner.street2 or "")
        lines, _missing = self._prepare_monta_lines()
        
        if not lines:
            # If no lines (e.g. only subscriptions), we shouldn't even call this or we should skip the POST
            return None

        invoice_id_digits = re.sub(r"\D", "", self.name or "")
        webshop_factuur_id = int(invoice_id_digits) if invoice_id_digits else 9999

        full_name = partner.name or ""
        first_name = full_name.split(" ")[0] if full_name else ""
        last_name = " ".join(full_name.split(" ")[1:]) if len(full_name.split(" ")) > 1 else ""

        addr_common = {
            "Company": partner.company_name or partner.name or "",
            "FirstName": first_name,
            "LastName": last_name,
            "Street": street,
            "HouseNumber": house_number or "1",
            "HouseNumberAddition": house_suffix or "",
            "PostalCode": partner.zip or "0000AA",
            "City": partner.city or "TestCity",
            "CountryCode": partner.country_id.code if partner.country_id else "NL",
            "PhoneNumber": partner.phone or "0000000000",
            "EmailAddress": partner.email or "test@example.com",
        }

        payload = {
            "WebshopOrderId": self.name,
            "Reference": self.client_order_ref or "",
            "ConsumerDetails": {
                "DeliveryAddress": dict(addr_common),
                "InvoiceAddress": dict(addr_common),
            },
            "Lines": lines,
            "Invoice": {
                "PaymentMethodDescription": "Odoo Order",
                "AmountInclTax": float(self.amount_total or 0.0),
                "TotalTax": float(sum((line.price_tax or 0.0) for line in self.order_line)),
                "WebshopFactuurID": webshop_factuur_id,
                "Currency": self.currency_id.name or "EUR",
            },
        }

        if (cfg.origin or "").strip():
            payload["Origin"] = cfg.origin.strip()

        return payload

    # ---------------------------------------------------------
    # API
    # ---------------------------------------------------------
    def _monta_request(self, method, path, payload=None, headers=None):
        if not self._is_company_allowed():
            return 0, {"note": "Blocked: company not allowed in Monta Configuration"}
        if not self._is_allowed_instance():
            return 0, {"note": "Blocked: instance URL guard"}
        
        if method == "POST" and path == "/order" and not payload:
            return 0, {"note": "Skipped: no lines to push (possibly all subscriptions)"}
            
        client = MontaClient(self.env, company=self.company_id)
        return client.request(self, method, path, payload=payload, headers=headers)

    def _is_duplicate_exists_error(self, status, body):
        """
        Monta duplicate example:
        {
          "OrderInvalidReasons": [{"Code": 1, "Message": "An order with that Webshop Order ID already exists"}]
        }
        """
        if status != 400 or not isinstance(body, dict):
            return False
        reasons = body.get("OrderInvalidReasons") or []
        for r in reasons:
            msg = (r or {}).get("Message") or ""
            if "already exists" in msg.lower():
                return True
        return False

    def _monta_create(self):
        self.ensure_one()
        
        payload = self._prepare_monta_order_payload()
        if not payload:
            _logger.info("[Monta] Skipping push for %s: no shippable lines found.", self.name)
            return

        status, body = self._monta_request("POST", "/order", payload)

        Status = self.env["monta.order.status"].sudo()
        account_key = Status._current_account_key() if hasattr(Status, "_current_account_key") else ""

        def upsert_snapshot(order_name, state, http_code, raw):
            now = fields.Datetime.now()
            vals = {
                "monta_account_key": account_key or False,
                "sale_order_id": self.id,
                "order_name": order_name,
                "monta_order_ref": (raw or {}).get("OrderRef")
                or (raw or {}).get("orderRef")
                or (raw or {}).get("id")
                or "",
                "status": state,
                "status_code": http_code if http_code is not None else 0,
                "source": "orders",
                "last_sync": now,
                "status_raw": json.dumps(raw or {}, ensure_ascii=False),
            }

            domain = [("order_name", "=", order_name)]
            try:
                if (
                    account_key
                    and hasattr(Status, "_has_monta_account_key_column")
                    and Status._has_monta_account_key_column()
                ):
                    domain = [
                        "&",
                        ("order_name", "=", order_name),
                        "|",
                        ("monta_account_key", "=", account_key),
                        ("monta_account_key", "=", False),
                    ]
            except Exception:
                domain = [("order_name", "=", order_name)]

            rec = Status.search(domain, limit=1)
            if rec:
                rec.write(vals)
            else:
                Status.create(vals)

        now = fields.Datetime.now()

        # ✅ Treat “already exists” as success (idempotent)
        if self._is_duplicate_exists_error(status, body):
            # This means Monta already has it; mark as sent so system stops retrying.
            self.with_context(skip_monta_write_hook=True).write(
                {
                    "monta_order_id": self.name,
                    "monta_sync_state": "sent",
                    "monta_last_push": now,
                    "monta_needs_sync": False,
                    "monta_retry_count": 0,
                }
            )
            upsert_snapshot(self.name, "sent", status, body)
            self.message_post(body="Monta: Order already existed. Marked as sent in Odoo to prevent duplicate retries.")
            return

        if 200 <= status < 300:
            self.with_context(skip_monta_write_hook=True).write(
                {
                    "monta_order_id": self.name,
                    "monta_sync_state": "sent",
                    "monta_last_push": now,
                    "monta_needs_sync": False,
                    "monta_retry_count": 0,
                }
            )
            upsert_snapshot(self.name, "sent", status, body)
            self.message_post(body="Order sent to Monta successfully.")
        else:
            # normal error
            if self.monta_retry_count < 1:
                self.with_context(skip_monta_write_hook=True).write(
                    {
                        "monta_sync_state": "error",
                        "monta_needs_sync": True,
                        "monta_retry_count": self.monta_retry_count + 1,
                    }
                )
            else:
                self.with_context(skip_monta_write_hook=True).write(
                    {
                        "monta_sync_state": "error",
                        "monta_needs_sync": False,
                    }
                )
            upsert_snapshot(self.name, "error", status, body)
            self.message_post(body="Failed to send order to Monta.")

    def _monta_delete(self, note="Cancelled from Odoo"):
        self.ensure_one()
        webshop_id = self.monta_order_id or self.name
        headers = {"Content-Type": "application/json-patch+json", "Accept": "application/json"}
        return self._monta_request("DELETE", f"/order/{webshop_id}", {"Note": note}, headers=headers)

    # ---------------------------------------------------------
    # Hooks
    # ---------------------------------------------------------
    def action_confirm(self):
        # ✅ Mitigation for Odoo 18 Subscription Validation Error
        # If order has subscription products but no plan_id, try to find a default plan to avoid crash.
        Plan = self.env["sale.subscription.plan"].sudo()
        for order in self:
            if not getattr(order, "is_subscription", False):
                # check if any line has a subscription product
                if any(getattr(line.product_id, "is_subscription", False) or getattr(line.product_id, "recurring_invoice", False) for line in order.order_line):
                    if not getattr(order, "plan_id", False):
                        default_plan = Plan.search([], limit=1)
                        if default_plan:
                            order.write({"plan_id": default_plan.id})
                            _logger.info("[Monta Mitigation] Auto-assigned subscription plan %s to %s", default_plan.name, order.name)

        res = super().action_confirm()
        for order in self:
            if order.monta_sync_state != "sent" and order._is_company_allowed() and order._is_monta_enabled_for_order():
                # check if it has any non-subscription lines
                lines, missing = order._prepare_monta_lines()
                if missing:
                    raise ValidationError("Cannot push to Monta:\n- " + "\n- ".join(missing))
                if lines:
                    order.with_context(skip_monta_write_hook=True).write({"monta_needs_sync": True})
                    order._monta_create()
        return res

    def write(self, vals):
        # ✅ prevent recursion: internal monta writes should not re-trigger
        if self.env.context.get("skip_monta_write_hook"):
            return super().write(vals)

        tracked_fields = {"partner_id", "order_line", "client_order_ref", "validity_date", "commitment_date"}
        if any(f in vals for f in tracked_fields):
            # Only mark for sync if confirmed AND not already sent
            for order in self:
                if (
                    order.state in ("sale", "done")
                    and order.monta_sync_state != "sent"
                    and order._is_company_allowed()
                    and order._is_monta_enabled_for_order()
                ):
                    lines, _missing = order._prepare_monta_lines()
                    if lines:
                        vals["monta_needs_sync"] = True
                        break

        res = super().write(vals)

        # Only push when confirmed + needs_sync + not already sent
        for order in self.filtered(
            lambda o: o.state in ("sale", "done") and o.monta_needs_sync and o.monta_sync_state != "sent"
        ):
            if order._is_company_allowed() and order._is_monta_enabled_for_order():
                if order._should_push_now():
                    order._monta_create()

        return res

    def action_cancel(self):
        res = super().action_cancel()
        for order in self:
            if order._is_company_allowed():
                order._monta_delete(note="Cancelled")
        return res

    # ---------------------------------------------------------------------
    # Wrapper method expected by monta.order.status button
    # ---------------------------------------------------------------------
    def _action_send_to_monta(self):
        """
        Called from monta.order.status button.
        Supports force send using context key: force_send_to_monta=True
        """
        for order in self:
            if not order._is_company_allowed():
                continue

            force = bool(order.env.context.get("force_send_to_monta"))
            if not force and order.monta_order_id:
                continue

            if force:
                order.with_context(skip_monta_write_hook=True).write({"monta_needs_sync": False, "monta_retry_count": 0})

            # Explicit check for missing SKUs when sending
            _lines, missing = order._prepare_monta_lines()
            if missing:
                raise ValidationError("Cannot push to Monta:\n- " + "\n- ".join(missing))

            order._monta_create()

        return True

    def action_manual_send_to_monta(self):
        return self.with_context(force_send_to_monta=True)._action_send_to_monta()
