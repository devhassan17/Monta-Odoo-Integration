# -*- coding: utf-8 -*-
"""
Monta Subscription Renewal Delivery Sync
=========================================

PURPOSE
-------
When Odoo renews a subscription it only creates a new invoice — it does NOT
create a new stock delivery.  This module fills that gap: for every posted
renewal invoice that has no matching Monta-pushed delivery, it creates one
delivery and pushes it to Monta automatically.

DESIGN RULES
------------
1. **Only "In Progress" subscriptions** (`subscription_state = '3_progress'`).
   Paused, churned, or draft subscriptions are ignored entirely.

2. **Valid Mollie mandate required.**
   Every subscription customer must have:
     - A Mollie Customer ID
     - A Mollie Mandate ID
     - Mandate status = 'valid'
   If ANY of these is missing, the SO is skipped and a clear log message is
   written so the issue is easy to diagnose.
   (If the Mollie module is not installed at all, this guard is skipped.)

3. **Per-invoice matching — not count-based.**
   We look at each individual renewal invoice (invoice #2 onwards) and check
   whether a Monta-pushed delivery was created after that invoice was posted.
   This means:
     - Changing a renewal date → no new invoice → no extra delivery ✅
     - Changing products → no new invoice → no extra delivery ✅
     - Actual renewal → new invoice posted → one delivery created ✅

4. **Backlog guard.**
   Renewal invoices older than RENEWAL_LOOKBACK_DAYS are skipped to avoid
   accidentally shipping historical periods in test/staging environments.

5. **Idempotent.**
   Safe to run multiple times — each invoice can only ever produce one delivery.
"""
import logging
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# Renewal invoices older than this many days are treated as historical backlog
# and will NOT trigger a new delivery.
RENEWAL_LOOKBACK_DAYS = 7


class MontaSubscriptionSync(models.Model):
    _inherit = "sale.order"

    # ------------------------------------------------------------------
    # Cron entry point
    # ------------------------------------------------------------------

    @api.model
    def _cron_monta_subscription_delivery_sync(self):
        """
        Scheduled action — push one Monta delivery per unprocessed renewal invoice.

        Flow:
          1. Find all in-progress subscription SOs in Monta-configured companies.
          2. For each SO, validate Mollie mandate (hard requirement).
          3. Find renewal invoices (invoice #2+) that have no matching delivery.
          4. For each unmatched invoice, create and push a new delivery.
        """
        _logger.info("[Monta Sub Sync] ─── Starting subscription renewal delivery sync ───")

        orders = self._monta_find_active_subscriptions()
        _logger.info("[Monta Sub Sync] Found %d in-progress subscription(s) to evaluate.", len(orders))

        created = 0
        skipped_mollie = 0

        for so in orders:
            try:
                # ── Guard: Mollie mandate must be valid ──────────────────────
                if not self._monta_has_valid_mollie_mandate(so):
                    skipped_mollie += 1
                    continue

                # ── Find renewal invoices with no delivery yet ───────────────
                pending_invoices = self._monta_get_unprocessed_renewal_invoices(so)

                for invoice in pending_invoices:
                    picking = so._monta_create_subscription_delivery(invoice=invoice)
                    if picking:
                        created += 1
                        _logger.info(
                            "[Monta Sub Sync] ✅ Created delivery %s for SO %s (renewal invoice: %s)",
                            picking.name, so.name, invoice.name,
                        )

            except Exception:
                _logger.exception(
                    "[Monta Sub Sync] ❌ Unexpected error processing SO %s", so.name
                )

        _logger.info(
            "[Monta Sub Sync] ─── Done: %d delivery(ies) created | %d skipped (Mollie) ───",
            created, skipped_mollie,
        )

    # ------------------------------------------------------------------
    # Step 1 — Find active in-progress subscriptions
    # ------------------------------------------------------------------

    @api.model
    def _monta_find_active_subscriptions(self):
        """
        Return confirmed, IN-PROGRESS subscription SOs from Monta-configured companies.

        'In Progress' strictly means subscription_state = '3_progress'.
        Paused ('4_paused'), churned ('6_churn'), and draft states are excluded.
        """
        # ── Resolve companies from Monta config ────────────────────────────
        configs = self.env["monta.config"].sudo().search([])
        if not configs:
            _logger.warning("[Monta Sub Sync] No Monta configuration record found — aborting.")
            return self.browse()

        company_ids = set()
        for cfg in configs:
            if cfg.allowed_company_ids:
                # Config restricts to specific companies
                company_ids.update(cfg.allowed_company_ids.ids)
            else:
                # No restriction = all companies allowed
                company_ids.update(self.env["res.company"].sudo().search([]).ids)

        if not company_ids:
            _logger.warning("[Monta Sub Sync] Could not resolve any company IDs — aborting.")
            return self.browse()

        # ── Build domain ────────────────────────────────────────────────────
        domain = [
            ("state", "in", ["sale", "done"]),
            ("company_id", "in", list(company_ids)),
        ]

        f = self._fields

        # Priority: use subscription_state for the strictest filter.
        # Fall back to older field names if subscription_state doesn't exist.
        if "subscription_state" in f:
            # STRICT: only in-progress subscriptions
            domain.append(("subscription_state", "=", "3_progress"))
        elif "is_subscription" in f:
            domain.append(("is_subscription", "=", True))
        elif "plan_id" in f:
            domain.append(("plan_id", "!=", False))
        else:
            _logger.warning(
                "[Monta Sub Sync] No subscription field found on sale.order — aborting."
            )
            return self.browse()

        orders = self.sudo().search(domain).filtered(
            lambda o: not (o.name and o.name.startswith("BC"))
        )

        _logger.info(
            "[Monta Sub Sync] _monta_find_active_subscriptions: %d candidate(s) in companies %s",
            len(orders), sorted(company_ids),
        )
        return orders

    # ------------------------------------------------------------------
    # Step 2 — Validate Mollie mandate
    # ------------------------------------------------------------------

    @api.model
    def _monta_has_valid_mollie_mandate(self, so):
        """
        Return True if the SO's customer has a valid Mollie mandate.

        Checks (all three must be present and valid):
          - mollie_customer_id   : customer must exist in Mollie
          - mollie_mandate_id    : a mandate must be on file
          - mollie_mandate_status: must equal 'valid'

        If the Mollie module is not installed (fields don't exist on the model),
        this guard is skipped and True is returned (no blocking).
        """
        partner = so.partner_id

        # If Mollie is not installed, skip this guard entirely
        mollie_installed = (
            'mollie_customer_id' in partner._fields
            or 'mollie_customer_id' in so._fields
        )
        if not mollie_installed:
            _logger.debug(
                "[Monta Sub Sync] SO %s: Mollie not installed — mandate guard skipped.", so.name
            )
            return True

        # Read all three Mollie fields — prefer partner-level, fall back to SO-level
        mollie_cust = (
            getattr(partner, 'mollie_customer_id', False)
            or getattr(so, 'mollie_customer_id', False)
        )
        mollie_mandate = (
            getattr(partner, 'mollie_mandate_id', False)
            or getattr(so, 'mollie_mandate_id', False)
        )
        mollie_status = (
            getattr(partner, 'mollie_mandate_status', '')
            or getattr(so, 'mollie_mandate_status', '')
        )

        if not mollie_cust:
            _logger.info(
                "[Monta Sub Sync] SO %s [%s]: ⛔ No Mollie Customer ID — skip.",
                so.name, partner.name,
            )
            return False

        if not mollie_mandate:
            _logger.info(
                "[Monta Sub Sync] SO %s [%s]: ⛔ No Mollie Mandate ID — skip.",
                so.name, partner.name,
            )
            return False

        if mollie_status != 'valid':
            _logger.info(
                "[Monta Sub Sync] SO %s [%s]: ⛔ Mollie mandate status is '%s' (expected 'valid') — skip.",
                so.name, partner.name, mollie_status,
            )
            return False

        _logger.debug(
            "[Monta Sub Sync] SO %s: ✅ Mollie mandate valid (cust=%s, mandate=%s).",
            so.name, mollie_cust, mollie_mandate,
        )
        return True

    # ------------------------------------------------------------------
    # Step 3 — Find renewal invoices with no matching Monta delivery
    # ------------------------------------------------------------------

    @api.model
    def _monta_get_unprocessed_renewal_invoices(self, so):
        """
        Return a list of posted renewal invoices that do not yet have a
        corresponding Monta-pushed delivery.

        Definition:
          - Renewal invoice = any posted customer invoice after the FIRST one.
            (The first invoice is the initial checkout delivery — already handled
             by Odoo's native SO confirmation flow.)
          - "Processed" = a Monta-pushed outgoing delivery exists whose create_date
            is AFTER the invoice's create_date (with a 1-hour buffer).

        Why this is immune to admin changes:
          - Admin edits renewal date → only next_invoice_date changes → no new invoice
          - Admin edits products → SO lines change → no new invoice immediately
          - Both cases leave the invoice list unchanged → no unprocessed invoices found
          - Only an actual subscription renewal (which posts a new invoice) can trigger.

        Backlog guard:
          Invoices older than RENEWAL_LOOKBACK_DAYS are skipped to prevent
          accidentally shipping historical periods.
        """
        cutoff_date = fields.Datetime.now() - timedelta(days=RENEWAL_LOOKBACK_DAYS)

        # All posted customer invoices, sorted oldest → newest
        all_invoices = so.invoice_ids.filtered(
            lambda inv: inv.move_type == "out_invoice" and inv.state == "posted"
        ).sorted(lambda inv: inv.create_date or inv.invoice_date)

        if len(all_invoices) <= 1:
            # Only the initial checkout invoice exists — nothing to renew
            _logger.debug(
                "[Monta Sub Sync] SO %s: only %d posted invoice(s) — initial checkout only, skip.",
                so.name, len(all_invoices),
            )
            return []

        # Renewal invoices = everything after the first (index 1+)
        renewal_invoices = list(all_invoices[1:])

        # ALL non-cancelled outgoing deliveries for this SO (pushed OR not pushed).
        # IMPORTANT: do NOT filter by monta_pushed=True here.
        # If a delivery was created by a previous cron run but the Monta push
        # failed (monta_pushed=False), we must NOT create another delivery for
        # the same period — that would produce a duplicate.
        all_outgoing_deliveries = so.picking_ids.filtered(
            lambda p: p.picking_type_code == "outgoing" and p.state != "cancel"
        )

        unprocessed = []
        for invoice in renewal_invoices:
            inv_created = invoice.create_date
            if not inv_created:
                continue

            # ── Backlog guard: skip old invoices ─────────────────────────
            if inv_created < cutoff_date:
                _logger.info(
                    "[Monta Sub Sync] SO %s: invoice %s is %d day(s) old — "
                    "older than %d-day backlog window, skipping.",
                    so.name, invoice.name,
                    (fields.Datetime.now() - inv_created).days,
                    RENEWAL_LOOKBACK_DAYS,
                )
                continue

            # ── Check: is there a Monta delivery created after this invoice? ─
            # We use a 1-hour buffer so deliveries created *slightly before*
            # the invoice (due to clock/transaction timing) are still counted.
            invoice_threshold = inv_created - timedelta(hours=1)
            has_matching_delivery = any(
                d.create_date and d.create_date >= invoice_threshold
                for d in all_outgoing_deliveries
            )

            if has_matching_delivery:
                _logger.debug(
                    "[Monta Sub Sync] SO %s: invoice %s already has a matching Monta delivery — skip.",
                    so.name, invoice.name,
                )
            else:
                _logger.info(
                    "[Monta Sub Sync] SO %s: invoice %s (posted %s) has no Monta delivery yet — queuing.",
                    so.name, invoice.name, inv_created,
                )
                unprocessed.append(invoice)

        return unprocessed

    # ------------------------------------------------------------------
    # Step 4 — Create and push the renewal delivery
    # ------------------------------------------------------------------

    def _monta_create_subscription_delivery(self, invoice=None):
        self.ensure_one()
        so = self
        """
        Create a new outgoing stock picking for one subscription renewal period
        and push it to Monta.

        Products and quantities are taken from the current SO order lines.
        Confirming the picking triggers our stock_picking.action_confirm() hook,
        which calls action_push_to_monta() automatically.

        Args:
            so:      The subscription sale.order record.
            invoice: The renewal account.move that triggered this delivery
                     (used only for logging/chatter context).
        """


        # ── Locate outgoing picking type ──────────────────────────────────────
        warehouse = so.warehouse_id or self.env["stock.warehouse"].sudo().search(
            [("company_id", "=", so.company_id.id)], limit=1
        )

        picking_type = (
            (warehouse.out_type_id if warehouse else None)
            or self.env["stock.picking.type"].sudo().search(
                [("code", "=", "outgoing"), ("company_id", "=", so.company_id.id)],
                limit=1,
            )
        )

        if not picking_type:
            _logger.warning(
                "[Monta Sub Sync] SO %s: no outgoing picking type found — cannot create delivery.",
                so.name,
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
                "[Monta Sub Sync] SO %s: cannot resolve stock locations — aborting delivery.",
                so.name,
            )
            return None

        # ── Build stock move lines from Invoice lines ───────────────────────
        move_vals = []
        lines_source = invoice.invoice_line_ids if invoice else so.order_line
        for line in lines_source:
            product = line.product_id
            if not product:
                continue
            if product.type not in ("product", "consu"):
                continue  # Services have no stock moves
            
            qty = line.quantity if hasattr(line, 'quantity') else getattr(line, 'product_uom_qty', 0.0)
            if qty <= 0:
                continue

            uom = getattr(line, 'product_uom_id', False) or getattr(line, 'product_uom', False)
            sale_line = line.sale_line_ids[:1] if hasattr(line, 'sale_line_ids') else line

            move_vals.append({
                "name": product.name or product.display_name,
                "product_id": product.id,
                "product_uom_qty": qty,
                "product_uom": uom.id if uom else product.uom_id.id,
                "location_id": src_loc.id,
                "location_dest_id": dest_loc.id,
                "sale_line_id": sale_line.id if sale_line else False,
                "company_id": so.company_id.id,
            })

        if not move_vals:
            _logger.warning(
                "[Monta Sub Sync] SO %s: no storable product lines found — cannot create delivery.",
                so.name,
            )
            return None

        # ── Create the picking ───────────────────────────────────────────────
        # Strip any default_ context keys to avoid move_type conflicts
        clean_ctx = {
            k: v for k, v in self.env.context.items()
            if not k.startswith("default_")
        }

        invoice_ref = invoice.name if invoice else "Renewal"
        picking = self.env["stock.picking"].sudo().with_context(clean_ctx).create({
            "picking_type_id": picking_type.id,
            "partner_id": so.partner_id.id,
            "origin": f"{so.name} (Subscription Renewal - {invoice_ref})",
            "sale_id": so.id,
            "location_id": src_loc.id,
            "location_dest_id": dest_loc.id,
            "company_id": so.company_id.id,
            "move_type": "direct",
            "move_ids": [(0, 0, v) for v in move_vals],
        })

        # Confirm triggers our stock_picking.action_confirm() hook → action_push_to_monta()
        picking.with_context(monta_create_delivery=True).action_confirm()

        # If Route Filter is enabled but Skip Subscriptions is disabled, 
        # cancel the renewal delivery immediately so it can never be pushed in the future.
        cfg = self.env["monta.config"].sudo().get_for_company(so.company_id)
        if cfg and cfg.enable_route_filter and not cfg.route_filter_skip_subscriptions:
            _logger.info(
                "[Monta Sub Sync] SO %s: Route Filter is enabled and Skip Subscriptions is disabled. "
                "Cancelling renewal delivery %s immediately.",
                so.name, picking.name,
            )
            picking.action_cancel()
            
            # Post a chatter note on the SO for cancellation visibility
            so.message_post(
                body=(
                    f"🚫 Monta Integration: Subscription renewal delivery {picking.name} "
                    f"was cancelled automatically because Route Filter: Skip Subscriptions? "
                    f"is disabled/blocking subscription renewals from being sent."
                )
            )
        else:
            _logger.info(
                "[Monta Sub Sync] SO %s: Renewal delivery %s created for invoice %s.",
                so.name, picking.name, invoice_ref,
            )

        return picking
