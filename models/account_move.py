# -*- coding: utf-8 -*-
"""Monta integration for automatic subscription renewals.

Odoo automatic renewals typically generate an invoice (account.move) via cron,
without confirming a new sale.order. Since the existing integration triggers
on sale.order confirmation, renewals were not sent to Monta.

This file hooks into invoice posting, finds the related sale.order(s), and
pushes a *new* Monta order per renewal cycle using a unique WebshopOrderId.
"""

import json
import logging
import re
from collections import defaultdict

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from ..services.monta_client import MontaClient
from ..utils.address import split_street
from ..utils.sku import resolve_sku
from ..utils.pack import expand_to_leaf_components

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = "account.move"

    monta_renewal_pushed = fields.Boolean(default=False, copy=False)
    monta_renewal_webshop_order_id = fields.Char(copy=False, index=True)
    monta_renewal_last_push = fields.Datetime(copy=False)

    # -------------------------
    # Helpers
    # -------------------------
    def _monta_related_sale_orders(self):
        """Return sale.orders linked to this invoice via invoice lines or origin."""
        self.ensure_one()
        orders = self.env["sale.order"]

        # Primary: invoice lines -> sale lines -> order
        try:
            sol = self.invoice_line_ids.mapped("sale_line_ids")
            orders |= sol.mapped("order_id")
        except Exception:
            pass

        # Fallback: invoice_origin contains SO name(s)
        if not orders and self.invoice_origin:
            # invoice_origin can be 'SO00123' or 'SO00123, SO00124'
            names = [n.strip() for n in (self.invoice_origin or "").split(",") if n.strip()]
            if names:
                orders |= self.env["sale.order"].search([("name", "in", names)])

        return orders

    def _prepare_monta_lines_from_invoice(self, company, order_lines):
        """Build Monta lines using the invoice lines / SO lines and packs expansion."""
        product_cache = {}
        lines = []
        product_counter = 1

        for so_line in order_lines:
            product = so_line.product_id
            if not product:
                continue

            # Expand packs to leaf components for Monta
            leaf_items = expand_to_leaf_components(so_line, company, product_cache)
            if not leaf_items:
                leaf_items = [(product, so_line.product_uom_qty or 0.0)]

            # Aggregate by SKU
            sku_qty = defaultdict(float)
            for leaf_product, qty in leaf_items:
                sku = resolve_sku(leaf_product, company)
                if not sku:
                    continue
                sku_qty[sku] += float(qty or 0.0)

            for sku, qty in sku_qty.items():
                if qty <= 0:
                    continue
                line = {
                    "ProductCounter": product_counter,
                    "Number": qty,
                    "Product": {
                        "SKU": sku,
                        "Name": so_line.name or product.display_name,
                    },
                }
                lines.append(line)
                product_counter += 1

        return lines

    def _monta_build_payload_from_sale_order(self, so, webshop_order_id):
        """Reuse the same payload structure as sale.order._monta_create but per invoice."""
        self.ensure_one()
        company = so.company_id
        partner = so.partner_shipping_id or so.partner_id

        # Shipping address
        street, house_number, suffix = split_street(partner.street or "")
        postcode = (partner.zip or "").replace(" ", "")
        country_code = partner.country_id.code or ""

        # Monta expects an int for WebshopFactuurID; derive from invoice number
        inv_digits = re.sub(r"\D", "", self.name or "")
        webshop_factuur_id = int(inv_digits) if inv_digits else 9999

        payload = {
            "WebshopOrderId": webshop_order_id,
            "Reference": (self.ref or self.payment_reference or so.client_order_ref or ""),
            "ConsumerDetails": {
                "DeliveryAddress": {
                    "Company": partner.company_name or partner.name or "",
                    "FirstName": partner.name.split(" ")[0] if partner.name else "",
                    "LastName": " ".join((partner.name or "").split(" ")[1:]) if len((partner.name or "").split(" ")) > 1 else "",
                    "Street": street,
                    "HouseNumber": house_number or "1",
                    "HouseNumberSuffix": suffix or "",
                    "PostalCode": postcode or "",
                    "City": partner.city or "",
                    "Country": country_code or "",
                    "Email": partner.email or "",
                    "Phone": partner.phone or partner.mobile or "",
                }
            },
            "WebshopFactuurID": webshop_factuur_id,
            "OrderLines": [],
        }

        # Choose order lines from the subscription SO
        payload["OrderLines"] = self._prepare_monta_lines_from_invoice(company, so.order_line)

        return payload

    # -------------------------
    # Main hook
    # -------------------------
    def action_post(self):
        """When recurring invoice is posted, push a renewal order to Monta."""
        res = super().action_post()

        # Process only posted customer invoices
        moves = self.filtered(lambda m: m.move_type in ("out_invoice", "out_refund") and m.state == "posted")
        if not moves:
            return res

        for move in moves:
            # Avoid duplicates
            if move.monta_renewal_pushed:
                continue

            sale_orders = move._monta_related_sale_orders()
            if not sale_orders:
                continue

            # Only those SO that are subscription-related
            sub_orders = sale_orders.filtered(lambda so: bool(getattr(so, "subscription_id", False)) or bool(getattr(so, "is_subscription", False)))
            if not sub_orders:
                continue

            pushed_any = False

            for so in sub_orders:
                company = so.company_id
                if hasattr(so, "_is_company_allowed") and not so._is_company_allowed():
                    continue

                # Unique WebshopOrderId per renewal cycle (SO + invoice number)
                # Replace '/' to be safe for external systems
                webshop_order_id = f"{so.name}-{move.name}".replace("/", "-")

                try:
                    payload = move._monta_build_payload_from_sale_order(so, webshop_order_id)

                    # Validate lines
                    if not payload.get("OrderLines"):
                        raise ValidationError("No order lines to push to Monta for this renewal invoice.")

                    client = MontaClient(company)

                    response = client.post("/api/v10/orders", payload)
                    status = getattr(response, "status_code", None)

                    # Typical success codes: 200/201/204.
                    if status in (200, 201, 204):
                        pushed_any = True
                        _logger.info("[Monta Renewal] Pushed %s for invoice %s", webshop_order_id, move.name)
                    else:
                        # Some APIs return 409 on duplicate; treat as already pushed
                        if status in (409,):
                            pushed_any = True
                        _logger.warning(
                            "[Monta Renewal] Non-success status %s for %s (invoice %s). Response: %s",
                            status, webshop_order_id, move.name, getattr(response, "text", ""),
                        )

                except ValidationError as e:
                    _logger.warning("[Monta Renewal] %s invoice %s validation error: %s", so.name, move.name, e)
                except Exception as e:
                    _logger.exception("[Monta Renewal] Push failed for %s invoice %s: %s", so.name, move.name, e)

            if pushed_any:
                move.write({
                    "monta_renewal_pushed": True,
                    "monta_renewal_webshop_order_id": f"{sub_orders[:1].name}-{move.name}".replace("/", "-"),
                    "monta_renewal_last_push": fields.Datetime.now(),
                })

        return res
