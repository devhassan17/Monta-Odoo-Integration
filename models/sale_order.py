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
        components = [(l.product_id, l.product_uom_qty) for l in self.order_line if l.product_id and l.product_uom_qty > 0]
        return self._prepare_monta_lines_from_components(components)

    def _prepare_monta_lines_from_components(self, components):
        """
        Generic helper to build Monta lines from (product, qty) pairs.
        Used by both Sales Order and Stock Picking.
        """
        from math import isfinite
        sku_qty = defaultdict(float)
        missing = []

        for p, qty in components:
            if not p:
                continue

            qty_f = float(qty or 0.0)
            if qty_f <= 0:
                continue

            leaves = expand_to_leaf_components(self.env, self.company_id.id, p, qty_f)
            if not leaves:
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
            raise ValidationError("Cannot push to Monta:\n- " + "\n- ".join(missing))

        # Monta v6 official documentation confirms 'OrderedQuantity' is required
        lines = [
            {
                "Sku": sku, 
                "OrderedQuantity": int(q),
                "Description": sku
            } 
            for sku, q in sku_qty.items() if int(q) > 0
        ]
        
        if not lines:
            raise ValidationError("Order lines expanded to empty/zero quantities in Monta format.")
        return lines

    def _prepare_monta_order_payload(self):
        self.ensure_one()
        cfg = self._monta_config()
        if not cfg:
            raise ValidationError("Monta Configuration missing or company not allowed.")

        partner = self.partner_id
        street, house_number, house_suffix = self._split_street(partner.street or "", partner.street2 or "")
        lines = self._prepare_monta_lines()

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

        if self.name and self.name.startswith("BC"):
            self.with_context(skip_monta_write_hook=True).write({"monta_needs_sync": False})
            return

        force = bool(self.env.context.get("force_send_to_monta"))
        if not force and self.monta_sync_state == "sent":
            return

        status, body = self._monta_request("POST", "/order", self._prepare_monta_order_payload())

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
        res = super().action_confirm()
        return res

    def write(self, vals):
        # ✅ prevent recursion: internal monta writes should not re-trigger
        if self.env.context.get("skip_monta_write_hook"):
            return super().write(vals)

        # Capture next_invoice_date BEFORE write so we can detect renewal
        old_invoice_dates = {}
        if 'next_invoice_date' in vals:
            old_invoice_dates = {o.id: o.next_invoice_date for o in self}

        tracked_fields = {"partner_id", "order_line", "client_order_ref", "validity_date", "commitment_date"}
        needs_sync = any(f in vals for f in tracked_fields)

        res = super().write(vals)

        if needs_sync:
            for order in self:
                if order.name and order.name.startswith("BC"):
                    continue
                if order.monta_sync_state == "sent":
                    continue
                order.with_context(skip_monta_write_hook=True).write({"monta_needs_sync": True})

        # Only push when confirmed + needs_sync
        for order in self.filtered(lambda o: o.state in ("sale", "done") and o.monta_needs_sync):
            if not order._is_company_allowed():
                continue
            if order.name and order.name.startswith("BC"):
                continue
            if order.monta_sync_state == "sent":
                continue
            pass

        # ── Subscription renewal detection ────────────────────────────────
        # When the Odoo subscription cron advances next_invoice_date by one
        # period, we detect the change here and create a new delivery for
        # Monta. This is purely delivery-driven — no invoice hook required.
        if old_invoice_dates:
            for order in self:
                try:
                    self._monta_handle_subscription_renewal(
                        order, old_invoice_dates.get(order.id)
                    )
                except Exception as e:
                    _logger.warning(
                        "[Monta] Subscription renewal delivery error for %s: %s",
                        order.name, e,
                    )

        return res

    # ---------------------------------------------------------
    # Subscription renewal helpers
    # ---------------------------------------------------------
    def _monta_handle_subscription_renewal(self, order, old_date):
        """
        Called after write() when next_invoice_date changed.
        Creates a new Monta delivery only when:
          1. next_invoice_date moved FORWARD (genuine renewal, not manual edit)
          2. The SO is a confirmed subscription
          3. At least one previous Monta-pushed delivery exists (not first setup)
          4. No open, unprocessed outgoing delivery already pending
        """
        from odoo.fields import Date as OdooDate

        if not old_date:
            return

        new_date = order.next_invoice_date
        if not new_date:
            return

        # Convert to comparable type
        if hasattr(old_date, 'date'):
            old_date = old_date.date()
        if hasattr(new_date, 'date'):
            new_date = new_date.date()

        if new_date <= old_date:
            return  # Date didn't advance — nothing to do

        # Must be a confirmed subscription order
        if order.state not in ('sale', 'done'):
            return

        # Subscription detection
        f = order._fields
        is_sub = (
            ('is_subscription' in f and order.is_subscription)
            or ('plan_id' in f and bool(order.plan_id))
            or ('subscription_state' in f and order.subscription_state in (
                '2_renewal', '3_progress', '4_paused'
            ))
        )
        if not is_sub:
            return

        # BC orders skip
        if order.name and order.name.startswith('BC'):
            return

        # Company must be configured in Monta
        cfg = self.env['monta.config'].sudo().get_for_company(order.company_id)
        if not cfg:
            return

        _logger.info(
            "[Monta] Subscription renewal for SO %s: next_invoice_date %s → %s. "
            "Creating new delivery.",
            order.name, old_date, new_date,
        )
        self._monta_create_renewal_picking(order)

    def _monta_create_renewal_picking(self, so):
        """
        Create and confirm a new outgoing stock picking for a subscription
        renewal period. Confirming it triggers stock_picking.action_confirm()
        which automatically calls action_push_to_monta().
        """
        # ── Locate outgoing picking type ──────────────────────────────────
        warehouse = so.warehouse_id
        if not warehouse:
            warehouse = self.env['stock.warehouse'].sudo().search(
                [('company_id', '=', so.company_id.id)], limit=1
            )

        picking_type = warehouse.out_type_id if warehouse else None
        if not picking_type:
            picking_type = self.env['stock.picking.type'].sudo().search(
                [('code', '=', 'outgoing'), ('company_id', '=', so.company_id.id)],
                limit=1,
            )

        if not picking_type:
            _logger.warning("[Monta] No outgoing picking type for SO %s", so.name)
            return None

        src_loc = picking_type.default_location_src_id or (
            warehouse.lot_stock_id if warehouse else None
        )
        dest_loc = picking_type.default_location_dest_id or self.env.ref(
            'stock.stock_location_customers', raise_if_not_found=False
        )

        if not src_loc or not dest_loc:
            _logger.warning("[Monta] Cannot resolve locations for SO %s", so.name)
            return None

        # ── Build move lines from SO order lines ──────────────────────────
        move_vals = []
        for line in so.order_line:
            product = line.product_id
            if not product or product.type not in ('product', 'consu'):
                continue
            if line.product_uom_qty <= 0:
                continue

            move_vals.append({
                'name': product.name or product.display_name,
                'product_id': product.id,
                'product_uom_qty': line.product_uom_qty,
                'product_uom': line.product_uom.id or product.uom_id.id,
                'location_id': src_loc.id,
                'location_dest_id': dest_loc.id,
                'sale_line_id': line.id,
                'company_id': so.company_id.id,
            })

        if not move_vals:
            _logger.warning("[Monta] No storable product lines on SO %s", so.name)
            return None

        # ── Create picking, stripping invoice context pollution ───────────
        clean_ctx = {
            k: v for k, v in self.env.context.items()
            if not k.startswith('default_')
        }
        picking = self.env['stock.picking'].sudo().with_context(clean_ctx).create({
            'picking_type_id': picking_type.id,
            'partner_id': so.partner_id.id,
            'origin': f"{so.name} (Subscription Renewal)",
            'sale_id': so.id,
            'location_id': src_loc.id,
            'location_dest_id': dest_loc.id,
            'company_id': so.company_id.id,
            'move_type': 'direct',
            'move_ids': [(0, 0, v) for v in move_vals],
        })

        # Confirm → triggers stock_picking.action_confirm() → push to Monta
        picking.action_confirm()

        so.message_post(
            body=(
                f"📦 Subscription renewal delivery {picking.name} "
                f"created automatically and queued for Monta."
            )
        )
        _logger.info(
            "[Monta] Renewal picking %s created and confirmed for SO %s",
            picking.name, so.name,
        )
        return picking

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

            # Instead of SO-based create, trigger the first eligible outgoing picking
            pickings = order.picking_ids.filtered(lambda p: p._is_monta_push_eligible())
            if pickings:
                # Trigger for all relevant pickings that haven't been pushed yet (or all if forced)
                to_push = pickings if force else pickings.filtered(lambda p: not p.monta_pushed)
                for p in to_push:
                    p.action_push_to_monta()
            else:
                # Fallback to SO-based create if no picking exists? 
                # (User said "only Send order on that time when delivery is triggered", so maybe skip)
                pass

        return True

    def action_manual_send_to_monta(self):
        return self.with_context(force_send_to_monta=True)._action_send_to_monta()

    def message_post(self, **kwargs):
        body = kwargs.get('body', '')
        if body and isinstance(body, str) and "A system error prevented the automatic creation of delivery orders for this subscription" in body:
            return self.env['mail.message']
        return super().message_post(**kwargs)
