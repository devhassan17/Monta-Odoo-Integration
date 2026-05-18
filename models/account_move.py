# -*- coding: utf-8 -*-
"""
Monta Subscription Renewal — Invoice Post Hook
================================================

APPROACH
--------
Instead of a polling cron, we hook directly into Odoo's native invoice
posting flow.  When Odoo's own subscription renewal cron posts a renewal
invoice, `action_post()` fires here and immediately triggers the Monta
delivery creation for that renewal period.

This means:
  - ONE cron job needed: Odoo's default "Sale Subscription: Generate
    Recurring Invoices" cron.  No separate Monta cron required.
  - Zero polling — the delivery is created the instant the invoice is posted.
  - Admin actions (changing renewal date, editing products) do NOT post
    invoices → this hook never fires → no extra deliveries.

GUARDS
------
The hook only triggers when ALL of the following are true:
  1. The invoice is a posted customer invoice (out_invoice, state=posted).
  2. The invoice is linked to a Sales Order.
  3. That SO is an IN-PROGRESS subscription (subscription_state = '3_progress').
  4. This is NOT the first invoice on the SO (first = initial checkout delivery,
     already handled by Odoo's native SO-confirmation → picking flow).
  5. The customer has a valid Mollie mandate (customer ID + mandate ID +
     status = 'valid').  Skipped if Mollie module is not installed.
  6. No non-cancelled outgoing delivery already exists for this SO created
     after this invoice was posted (duplicate guard).
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
    # Override: hook into invoice posting
    # -------------------------------------------------------------------------

    def action_post(self):
        """
        Odoo's native invoice posting method.

        After calling super() (which does all standard Odoo posting logic),
        we inspect newly posted invoices and trigger a Monta renewal delivery
        for any that represent a subscription renewal.

        Odoo's default behaviour is completely preserved — we only ADD the
        Monta push after the normal posting flow completes.
        """
        # ── Let Odoo do everything it normally does first ──────────────────
        res = super().action_post()

        # ── Now check each newly-posted invoice ────────────────────────────
        for move in self:
            if move.state != "posted":
                continue
            if move.move_type != "out_invoice":
                continue

            # Resolve the linked Sale Order
            so = self._monta_get_subscription_so(move)
            if not so:
                continue

            # Run all guards — if any fails, skip this invoice
            if not self._monta_is_renewal_invoice(move, so):
                continue
            if not self._monta_has_valid_mollie_mandate(so):
                continue
            if self._monta_delivery_already_exists(move, so):
                continue

            # All guards passed — create and push a renewal delivery
            try:
                _logger.info(
                    "[Monta Invoice Hook] Invoice %s posted for SO %s — "
                    "triggering subscription renewal delivery.",
                    move.name, so.name,
                )
                so._monta_create_subscription_delivery(invoice=move)
            except Exception:
                _logger.exception(
                    "[Monta Invoice Hook] Failed to create renewal delivery "
                    "for SO %s (invoice %s).",
                    so.name, move.name,
                )

        return res

    # -------------------------------------------------------------------------
    # Guard helpers
    # -------------------------------------------------------------------------

    @api.model
    def _monta_get_subscription_so(self, move):
        """
        Return the subscription Sale Order linked to this invoice, or None.

        In Odoo 17/18 the link from invoice → SO is via:
          move.invoice_line_ids → sale_line_ids → order_id
        """
        so = move.invoice_line_ids.mapped("sale_line_ids.order_id")[:1]
        if not so:
            return None

        # Must be an in-progress subscription
        f = so._fields
        if "subscription_state" in f:
            if so.subscription_state != "3_progress":
                return None
        elif "is_subscription" in f:
            if not so.is_subscription:
                return None
        elif "plan_id" in f:
            if not so.plan_id:
                return None
        else:
            # No subscription field — not a subscription SO
            return None

        # Skip BC orders
        if so.name and so.name.startswith("BC"):
            return None

        # Verify Monta config exists for this company
        cfg = self.env["monta.config"].sudo().get_for_company(so.company_id)
        if not cfg:
            return None

        return so

    @api.model
    def _monta_is_renewal_invoice(self, move, so):
        """
        Return True only if this invoice is a RENEWAL invoice (not the first).

        The first invoice corresponds to the initial subscription purchase and
        already has a delivery created by Odoo's native SO-confirmation flow.
        We must not create a second delivery for it.

        Renewal = this invoice is NOT the oldest posted customer invoice on SO.
        """
        all_posted = so.invoice_ids.filtered(
            lambda inv: inv.move_type == "out_invoice" and inv.state == "posted"
        ).sorted(lambda inv: inv.create_date or inv.invoice_date)

        if not all_posted:
            return False

        # If this invoice IS the first/oldest one, it's the checkout invoice — skip
        if all_posted[0].id == move.id:
            _logger.debug(
                "[Monta Invoice Hook] Invoice %s is the first invoice on SO %s "
                "(initial checkout) — no renewal delivery needed.",
                move.name, so.name,
            )
            return False

        return True

    @api.model
    def _monta_has_valid_mollie_mandate(self, so):
        """
        Return True if the SO's customer has a valid Mollie mandate.
        Returns True (no block) if Mollie is not installed on this instance.
        """
        partner = so.partner_id

        # If Mollie is not installed at all, skip the guard
        mollie_installed = (
            "mollie_customer_id" in partner._fields
            or "mollie_customer_id" in so._fields
        )
        if not mollie_installed:
            return True

        mollie_cust = (
            getattr(partner, "mollie_customer_id", False)
            or getattr(so, "mollie_customer_id", False)
        )
        mollie_mandate = (
            getattr(partner, "mollie_mandate_id", False)
            or getattr(so, "mollie_mandate_id", False)
        )
        mollie_status = (
            getattr(partner, "mollie_mandate_status", "")
            or getattr(so, "mollie_mandate_status", "")
        )

        if not mollie_cust:
            _logger.info(
                "[Monta Invoice Hook] SO %s [%s]: ⛔ No Mollie Customer ID — skip.",
                so.name, partner.name,
            )
            return False
        if not mollie_mandate:
            _logger.info(
                "[Monta Invoice Hook] SO %s [%s]: ⛔ No Mollie Mandate ID — skip.",
                so.name, partner.name,
            )
            return False
        if mollie_status != "valid":
            _logger.info(
                "[Monta Invoice Hook] SO %s [%s]: ⛔ Mandate status '%s' ≠ 'valid' — skip.",
                so.name, partner.name, mollie_status,
            )
            return False

        return True

    @api.model
    def _monta_delivery_already_exists(self, move, so):
        """
        Return True if a non-cancelled outgoing delivery already exists for
        this SO that was created after this invoice was posted.

        This is the duplicate guard — prevents creating a second delivery if
        the hook fires twice (e.g. the invoice is reset-to-draft and re-posted).
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
                "invoice %s — skipping to prevent duplicate.",
                so.name, existing.name, move.name,
            )
            return True

        return False
