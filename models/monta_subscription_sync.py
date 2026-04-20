# -*- coding: utf-8 -*-
"""
Monta Subscription Delivery Sync
==================================
When a subscription renews in Odoo, this cron detects the gap between
the number of posted invoices and the number of Monta-pushed deliveries
for that subscription SO, then creates and pushes the missing delivery.

This approach:
  - Does NOT hook into invoice posting (no invoice dependency)
  - Does NOT auto-confirm or touch dates (no date-doubling)
  - Works purely with Odoo's native stock.picking delivery objects
  - Is safe to run multiple times (idempotent)
"""
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class MontaSubscriptionSync(models.Model):
    _inherit = "sale.order"

    # ------------------------------------------------------------------
    # Cron entry point
    # ------------------------------------------------------------------
    @api.model
    def _cron_monta_subscription_delivery_sync(self):
        """
        Scheduled action: find subscription SOs where the number of posted
        invoices exceeds the number of Monta-pushed outgoing deliveries, then
        create and push a new delivery for each missing period.

        Logic:
          posted_invoices  = invoices on this SO with state='posted'
          monta_deliveries = outgoing pickings with monta_pushed=True
          if posted_invoices > monta_deliveries → renewal delivery needed
        """
        _logger.info("[Monta Sub Sync] Starting subscription delivery sync cron")

        subscription_orders = self._monta_find_subscription_orders()
        _logger.info(
            "[Monta Sub Sync] Found %d active subscription orders to check",
            len(subscription_orders),
        )

        created = 0
        for so in subscription_orders:
            try:
                if self._monta_subscription_needs_delivery(so):
                    picking = self._monta_create_subscription_delivery(so)
                    if picking:
                        created += 1
                        _logger.info(
                            "[Monta Sub Sync] Created renewal delivery %s for SO %s",
                            picking.name, so.name,
                        )
            except Exception as e:
                _logger.warning(
                    "[Monta Sub Sync] Error processing SO %s: %s",
                    so.name, e,
                )

        _logger.info(
            "[Monta Sub Sync] Done — created %d renewal delivery(ies)", created
        )

    # ------------------------------------------------------------------
    # Detection helpers
    # ------------------------------------------------------------------
    @api.model
    def _monta_find_subscription_orders(self):
        """Return confirmed subscription SOs for companies with Monta configured."""
        # Find all companies that have Monta configured
        configs = self.env["monta.config"].sudo().search([])
        company_ids = configs.mapped("company_id").ids
        if not company_ids:
            return self.browse()

        # Find SOs that look like subscriptions (any known field variant)
        domain = [
            ("state", "in", ["sale", "done"]),
            ("company_id", "in", company_ids),
        ]

        f = self._fields

        # Build subscription filter — use whichever field exists
        sub_domain = False
        if "is_subscription" in f:
            sub_domain = ("is_subscription", "=", True)
        elif "plan_id" in f:
            sub_domain = ("plan_id", "!=", False)
        elif "subscription_state" in f:
            sub_domain = ("subscription_state", "in", [
                "3_progress", "4_paused", "2_renewal"
            ])

        if sub_domain:
            domain.append(sub_domain)
        else:
            # No subscription field found on this Odoo version — skip
            _logger.warning(
                "[Monta Sub Sync] Cannot detect subscriptions: no known "
                "subscription field found on sale.order"
            )
            return self.browse()

        orders = self.sudo().search(domain)

        # Filter out BC orders
        orders = orders.filtered(
            lambda o: not (o.name and o.name.startswith("BC"))
        )
        return orders

    @api.model
    def _monta_subscription_needs_delivery(self, so):
        """
        Return True if this subscription SO needs a new Monta delivery.

        Rule:
          posted invoice count > monta-pushed outgoing delivery count
        """
        posted_invoices = so.invoice_ids.filtered(
            lambda inv: inv.move_type == "out_invoice" and inv.state == "posted"
        )
        invoice_count = len(posted_invoices)
        if invoice_count == 0:
            return False  # No invoice yet — initial delivery handled by stock_picking hook

        monta_deliveries = so.picking_ids.filtered(
            lambda p: p.picking_type_code == "outgoing" and p.monta_pushed
        )
        delivery_count = len(monta_deliveries)

        needs = invoice_count > delivery_count
        if needs:
            _logger.info(
                "[Monta Sub Sync] SO %s: %d invoices, %d Monta deliveries → needs new delivery",
                so.name, invoice_count, delivery_count,
            )
        return needs

    # ------------------------------------------------------------------
    # Delivery creation
    # ------------------------------------------------------------------
    @api.model
    def _monta_create_subscription_delivery(self, so):
        """
        Create a new outgoing stock picking for a subscription renewal and
        push it to Monta.  Lines are taken from the SO lines (same products
        and quantities as the original order).  Confirming the picking
        triggers stock_picking.action_confirm() which calls
        action_push_to_monta() automatically.
        """
        # ---- Locate outgoing picking type ----
        warehouse = so.warehouse_id
        if not warehouse:
            warehouse = self.env["stock.warehouse"].sudo().search(
                [("company_id", "=", so.company_id.id)], limit=1
            )

        picking_type = warehouse.out_type_id if warehouse else None
        if not picking_type:
            picking_type = self.env["stock.picking.type"].sudo().search(
                [("code", "=", "outgoing"), ("company_id", "=", so.company_id.id)],
                limit=1,
            )

        if not picking_type:
            _logger.warning(
                "[Monta Sub Sync] No outgoing picking type for SO %s", so.name
            )
            return None

        src_loc = picking_type.default_location_src_id or (
            warehouse.lot_stock_id if warehouse else None
        )
        dest_loc = picking_type.default_location_dest_id or self.env.ref(
            "stock.stock_location_customers", raise_if_not_found=False
        )

        if not src_loc or not dest_loc:
            _logger.warning(
                "[Monta Sub Sync] Cannot resolve stock locations for SO %s", so.name
            )
            return None

        # ---- Build move lines from SO order lines ----
        move_vals = []
        for line in so.order_line:
            product = line.product_id
            if not product:
                continue
            if product.type not in ("product", "consu"):
                continue  # Skip services
            if line.product_uom_qty <= 0:
                continue

            move_vals.append({
                "name": product.name or product.display_name,
                "product_id": product.id,
                "product_uom_qty": line.product_uom_qty,
                "product_uom": line.product_uom.id or product.uom_id.id,
                "location_id": src_loc.id,
                "location_dest_id": dest_loc.id,
                "sale_line_id": line.id,
                "company_id": so.company_id.id,
            })

        if not move_vals:
            _logger.warning(
                "[Monta Sub Sync] No storable product lines on SO %s", so.name
            )
            return None

        # ---- Create the picking (strip invoice context to avoid move_type clash) ----
        clean_ctx = {
            k: v for k, v in self.env.context.items()
            if not k.startswith("default_")
        }
        picking = self.env["stock.picking"].sudo().with_context(clean_ctx).create({
            "picking_type_id": picking_type.id,
            "partner_id": so.partner_id.id,
            "origin": f"{so.name} (Subscription Renewal)",
            "sale_id": so.id,
            "location_id": src_loc.id,
            "location_dest_id": dest_loc.id,
            "company_id": so.company_id.id,
            "move_type": "direct",
            "move_ids": [(0, 0, v) for v in move_vals],
        })

        # Confirm → triggers stock_picking.action_confirm() → action_push_to_monta()
        picking.action_confirm()

        # Chatter note
        so.message_post(
            body=(
                f"📦 Subscription renewal delivery {picking.name} "
                f"created automatically and queued for Monta."
            )
        )

        return picking
