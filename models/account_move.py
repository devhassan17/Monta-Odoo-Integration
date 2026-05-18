# -*- coding: utf-8 -*-
"""
Monta Subscription Renewal - Invoice Post Hook

When Odoo's subscription cron posts a renewal invoice, this hook creates
a new outgoing delivery for that renewal period and pushes it to Monta.

Flow:
  1. Odoo subscription cron runs (default, untouched)
  2. Renewal invoice posted -> action_post() fires
  3. Our hook runs AFTER super() -- Odoo is never affected
  4. If this is a renewal invoice (not first) -> create delivery -> push to Monta

No subscription-specific checks (Mollie, mandate, etc.).
Deliveries go through the same eligibility path as regular orders.
"""
import logging
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    """
    Extends account.move to trigger Monta delivery creation on subscription
    renewal invoice posting.

    Deprecated legacy fields kept so existing DB columns don't break.
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
    # Override action_post - hook AFTER Odoo's default posting
    # -------------------------------------------------------------------------

    def action_post(self):
        """
        Odoo's native invoice posting method - extended for Monta renewals.

        Order of execution:
          1. super().action_post() runs first (100% of Odoo's default logic)
          2. Our code runs after, isolated in try/except
          3. Odoo's result is always returned regardless

        For each posted renewal invoice on a subscription SO, we create a
        new outgoing delivery and push it to Monta. No extra subscription
        checks -- deliveries go through the same eligibility logic as regular
        orders (route filter, BC check, etc.).
        """
        # Step 1: Odoo's full default posting runs first
        res = super().action_post()

        # Step 2: Create renewal delivery for qualifying invoices.
        # Wrapped in try/except -- any bug here is logged silently and
        # NEVER propagates to affect Odoo's cron or invoice posting.
        try:
            for move in self:
                if move.state != "posted":
                    continue
                if move.move_type != "out_invoice":
                    continue

                # Resolve the linked in-progress subscription SO
                so = self._monta_get_subscription_so(move)
                if not so:
                    continue

                _logger.info(
                    "[Monta Invoice Hook] Invoice %s posted for SO %s -- evaluating renewal delivery.",
                    move.name, so.name,
                )

                # Skip the first invoice -- the native Odoo delivery (created at
                # SO confirmation) already handles the first period.
                # Only renewal invoices (2nd+) need a new delivery.
                if not self._monta_is_renewal_invoice(move, so):
                    continue

                # Mollie mandate check -- renewal requires valid mandate
                if not self._monta_has_valid_mollie_mandate(so):
                    _logger.info(
                        "[Monta Invoice Hook] SO %s: renewal invoice %s -- "
                        "Mollie mandate invalid, no delivery created.",
                        so.name, move.name,
                    )
                    continue

                # Skip if an invoice-driven delivery already exists for this period
                if self._monta_renewal_delivery_exists(so):
                    continue

                # Create the renewal delivery
                try:
                    picking = so._monta_create_subscription_delivery(invoice=move)
                    if picking:
                        _logger.info(
                            "[Monta Invoice Hook] Renewal delivery %s created for SO %s.",
                            picking.name, so.name,
                        )
                    else:
                        _logger.warning(
                            "[Monta Invoice Hook] No renewal delivery returned for SO %s invoice %s.",
                            so.name, move.name,
                        )
                except Exception:
                    _logger.exception(
                        "[Monta Invoice Hook] Error creating renewal delivery for SO %s (invoice %s).",
                        so.name, move.name,
                    )

        except Exception:
            # Safety net -- our code must never crash Odoo's invoice posting
            _logger.exception(
                "[Monta Invoice Hook] Unexpected error -- Odoo invoice posting was NOT affected."
            )

        # Step 3: Always return Odoo's result
        return res

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    @api.model
    def _monta_get_subscription_so(self, move):
        """
        Return the in-progress subscription Sale Order linked to this invoice.
        Returns None if not a qualifying subscription.
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
            return None

        # Skip BC orders
        if so.name and so.name.startswith("BC"):
            return None

        # Monta must be configured for this company
        cfg = self.env["monta.config"].sudo().get_for_company(so.company_id)
        if not cfg:
            return None

        return so

    @api.model
    def _monta_is_renewal_invoice(self, move, so):
        """
        Return True only if this is a RENEWAL invoice (not the first/checkout invoice).

        The first posted invoice corresponds to the initial subscription purchase.
        Odoo's native delivery (created at SO confirmation) handles that period.
        Any subsequent invoice is a renewal and needs a new delivery.
        """
        all_posted = so.invoice_ids.filtered(
            lambda inv: inv.move_type == "out_invoice" and inv.state == "posted"
        ).sorted(lambda inv: inv.create_date or inv.invoice_date)

        if not all_posted:
            return False

        if all_posted[0].id == move.id:
            _logger.info(
                "[Monta Invoice Hook] SO %s: invoice %s is the FIRST invoice -- "
                "native delivery handles this period, skip.",
                so.name, move.name,
            )
            return False

        return True

    @api.model
    def _monta_renewal_delivery_exists(self, so):
        """
        Duplicate guard -- return True if an invoice-driven renewal delivery
        already exists for this SO.

        Only looks for deliveries OUR hook created (identified by
        'Subscription Renewal' in origin). Native Odoo deliveries are ignored.
        """
        existing = self.env["stock.picking"].sudo().search([
            ("sale_id", "=", so.id),
            ("picking_type_code", "=", "outgoing"),
            ("state", "!=", "cancel"),
            ("origin", "ilike", "Subscription Renewal"),
        ], limit=1)

        if existing:
            _logger.info(
                "[Monta Invoice Hook] SO %s: renewal delivery %s already exists "
                "(origin: %s) -- skipping duplicate.",
                so.name, existing.name, existing.origin,
            )
            return True

        return False

    @api.model
    def _monta_has_valid_mollie_mandate(self, so):
        """
        Return True if the customer has a valid Mollie mandate.
        Checks: mollie_customer_id, mollie_mandate_id, mollie_mandate_status == 'valid'.
        Returns True (bypass) if Mollie is not installed on this instance.
        """
        partner = so.partner_id

        # If Mollie fields don't exist on this instance, skip the check
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
                "[Monta Invoice Hook] SO %s [%s]: No Mollie Customer ID.",
                so.name, partner.name,
            )
            return False
        if not mollie_mandate:
            _logger.info(
                "[Monta Invoice Hook] SO %s [%s]: No Mollie Mandate ID.",
                so.name, partner.name,
            )
            return False
        if mollie_status != "valid":
            _logger.info(
                "[Monta Invoice Hook] SO %s [%s]: Mandate status '%s' != 'valid'.",
                so.name, partner.name, mollie_status,
            )
            return False

        return True
