# -*- coding: utf-8 -*-
"""
Monta Subscription Renewal — Invoice Post Hook
================================================

IDEAL APPROACH (2-Phase)
-------------------------
Phase 1 — Delivery Creation (this file, action_post hook):
  When Odoo's subscription cron posts a renewal invoice, we ALWAYS create
  an Odoo outgoing delivery for that renewal period.  No Monta-specific
  checks happen here.  The delivery is visible to warehouse staff regardless
  of whether it will be sent to Monta.

Phase 2 — Monta Push (stock_picking.py, _is_monta_push_eligible):
  When the delivery is confirmed (action_confirm), our stock_picking hook
  fires and checks ALL Monta conditions:
    ✅ Monta config enabled for this company
    ✅ Valid Mollie customer ID, mandate ID, status = 'valid'
    ✅ Route filter (if enabled)
    ✅ Not already pushed (idempotency)
  If all pass → delivery is pushed to Monta.
  If any fail → delivery stays in Odoo only (warehouse can still process it).

WHY THIS IS THE IDEAL WAY
--------------------------
  - One cron needed: Odoo's "Sale Subscription: Generate Recurring Invoices"
  - Delivery always exists in Odoo → warehouse visibility regardless of Monta
  - Monta push is a conditional side-effect, not a blocker
  - Admin changes (renewal date, products) don't post invoices → hook never fires
  - Duplicate-safe: DB-level guard prevents double delivery creation

DELIVERY CREATION GUARDS (Phase 1)
------------------------------------
  1. Posted customer invoice (out_invoice)
  2. Linked to a confirmed subscription SO (subscription_state = '3_progress')
  3. NOT the first/checkout invoice (first delivery created by Odoo natively)
  4. No non-cancelled outgoing delivery already exists for this period
"""
import logging
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    """
    Deprecated fields kept so existing database columns don't break.
    Monta integration now works exclusively through stock.picking (deliveries).
    """
    _inherit = "account.move"

    monta_renewal_pushed = fields.Boolean(
        string="Pushed to Monta (Deprecated)",
        copy=False,
    )
    monta_renewal_webshop_order_id = fields.Char(
        string="Monta Webshop Order ID (Deprecated)",
        copy=False,
    )
    monta_renewal_last_push = fields.Datetime(
        string="Monta Last Push (Deprecated)",
        copy=False,
    )

    # -------------------------------------------------------------------------
    # Phase 1: Hook into invoice posting → always create Odoo delivery
    # -------------------------------------------------------------------------

    def action_post(self):
        """
        Odoo's native invoice posting method — extended for Monta renewals.

        After super() completes (all standard Odoo posting logic runs first),
        we check each newly-posted invoice.  If it is a subscription renewal
        invoice and no delivery exists yet for this period, we ALWAYS create
        one — regardless of Mollie mandate or Monta route conditions.

        The delivery's action_confirm() then handles Phase 2 automatically:
        _is_monta_push_eligible() in stock_picking.py evaluates all Monta
        conditions and either pushes the delivery to Monta or skips silently.

        Odoo's default behaviour is 100% preserved — we only ADD delivery
        creation after the standard posting flow.
        """
        # ── Phase 1a: Let Odoo complete its full posting flow first ──────────
        res = super().action_post()

        # ── Phase 1b: Create renewal delivery for qualifying invoices ────────
        for move in self:
            if move.state != "posted":
                continue
            if move.move_type != "out_invoice":
                continue

            # Resolve linked in-progress subscription SO
            so = self._monta_get_subscription_so(move)
            if not so:
                continue

            # Guard: skip the initial checkout invoice (Odoo native flow handles it)
            if not self._monta_is_renewal_invoice(move, so):
                continue

            # Guard: skip if a delivery already exists for this renewal period
            if self._monta_delivery_already_exists(move, so):
                continue

            # ── CREATE THE DELIVERY ────────────────────────────────────────────
            # No Monta checks here. The delivery is created unconditionally for
            # every valid renewal. Phase 2 (stock_picking._is_monta_push_eligible)
            # handles the Monta-specific checks during action_confirm().
            try:
                _logger.info(
                    "[Monta Invoice Hook] 📄 Invoice %s posted for SO %s "
                    "— creating renewal delivery (Phase 1).",
                    move.name, so.name,
                )
                picking = so._monta_create_subscription_delivery(invoice=move)
                if picking:
                    _logger.info(
                        "[Monta Invoice Hook] ✅ Delivery %s created for SO %s. "
                        "Phase 2 Monta push will depend on eligibility check.",
                        picking.name, so.name,
                    )
                else:
                    _logger.warning(
                        "[Monta Invoice Hook] ⚠️  Delivery creation returned None "
                        "for SO %s (invoice %s). Check logs above.",
                        so.name, move.name,
                    )
            except Exception:
                _logger.exception(
                    "[Monta Invoice Hook] ❌ Error creating renewal delivery "
                    "for SO %s (invoice %s).",
                    so.name, move.name,
                )

        return res

    # -------------------------------------------------------------------------
    # Phase 1 guard helpers
    # -------------------------------------------------------------------------

    @api.model
    def _monta_get_subscription_so(self, move):
        """
        Return the in-progress subscription Sale Order linked to this invoice.
        Returns None if the invoice is not from a qualifying subscription.

        Link path (Odoo 17/18):
          account.move → invoice_line_ids → sale_line_ids → order_id
        """
        so = move.invoice_line_ids.mapped("sale_line_ids.order_id")[:1]
        if not so:
            return None

        # Must be an in-progress subscription
        f = so._fields
        if "subscription_state" in f:
            if so.subscription_state != "3_progress":
                _logger.debug(
                    "[Monta Invoice Hook] SO %s: subscription_state='%s' ≠ '3_progress' — skip.",
                    so.name, so.subscription_state,
                )
                return None
        elif "is_subscription" in f:
            if not so.is_subscription:
                return None
        elif "plan_id" in f:
            if not so.plan_id:
                return None
        else:
            return None  # No subscription fields found

        # Skip BC (manual/bulk) orders
        if so.name and so.name.startswith("BC"):
            return None

        # Monta must be configured for this company
        cfg = self.env["monta.config"].sudo().get_for_company(so.company_id)
        if not cfg:
            _logger.debug(
                "[Monta Invoice Hook] SO %s: no Monta config for company %s — skip.",
                so.name, so.company_id.name,
            )
            return None

        return so

    @api.model
    def _monta_is_renewal_invoice(self, move, so):
        """
        Return True only if this is a RENEWAL invoice (not the initial checkout).

        The first/oldest posted invoice on a subscription SO corresponds to the
        original purchase.  Odoo already creates a delivery for it during SO
        confirmation — we must not create a duplicate.

        Any subsequent invoice = renewal → we create a new delivery.
        """
        all_posted = so.invoice_ids.filtered(
            lambda inv: inv.move_type == "out_invoice" and inv.state == "posted"
        ).sorted(lambda inv: inv.create_date or inv.invoice_date)

        if not all_posted:
            return False

        if all_posted[0].id == move.id:
            _logger.debug(
                "[Monta Invoice Hook] Invoice %s is the FIRST invoice on SO %s "
                "(initial checkout) — Odoo native delivery handles this, skip.",
                move.name, so.name,
            )
            return False

        return True

    @api.model
    def _monta_delivery_already_exists(self, move, so):
        """
        Duplicate guard — return True if a non-cancelled outgoing delivery
        already exists for this SO created at or after this invoice was posted.

        Prevents double-delivery if:
          - Invoice is reset-to-draft and re-posted by an admin
          - Two processes run simultaneously (race condition)
        """
        if not move.create_date:
            return False

        threshold = move.create_date - timedelta(hours=1)
        existing = self.env["stock.picking"].sudo().search([
            ("sale_id", "=", so.id),
            ("picking_type_code", "=", "outgoing"),
            ("state", "!=", "cancel"),
            ("create_date", ">=", threshold),
        ], limit=1)

        if existing:
            _logger.info(
                "[Monta Invoice Hook] SO %s: delivery %s already exists for "
                "invoice %s (created %s) — skipping duplicate creation.",
                so.name, existing.name, move.name, existing.create_date,
            )
            return True

        return False
