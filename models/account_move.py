# -*- coding: utf-8 -*-
"""
Monta Subscription Renewal - Invoice Post Hook
"""
import logging
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    """
    Extends account.move to trigger Monta delivery creation whenever a
    subscription invoice is posted by Odoo's default subscription cron.

    Deprecated legacy fields are kept so existing DB columns don't break.
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

        Execution order:
          1. super().action_post() runs first (Odoo's full default logic)
          2. Our code runs AFTER, completely isolated in try/except
          3. We always return Odoo's result regardless of what our code does

        Odoo's default cron is NEVER affected by our code.
        """
        # Step 1: Let Odoo complete its full posting flow first
        res = super().action_post()

        # Step 2: Create Monta delivery for qualifying subscription invoices.
        # The entire block is wrapped in try/except - any bug in our code is
        # silently logged and NEVER propagates to break Odoo's cron.
        try:
            for move in self:
                if move.state != "posted":
                    continue
                if move.move_type != "out_invoice":
                    continue

                # Resolve linked in-progress subscription SO
                so = self._monta_get_subscription_so(move)
                if not so:
                    if move.invoice_line_ids.mapped("sale_line_ids"):
                        _logger.debug(
                            "[Monta Invoice Hook] Invoice %s: not an in-progress "
                            "subscription or no Monta config -- skip.",
                            move.name,
                        )
                    continue

                _logger.info(
                    "[Monta Invoice Hook] Invoice %s posted for SO %s "
                    "(subscription_state=%s) -- evaluating delivery.",
                    move.name, so.name,
                    getattr(so, 'subscription_state', 'n/a'),
                )

                # Guard: must be a valid subscription invoice
                if not self._monta_is_subscription_invoice(move, so):
                    _logger.info(
                        "[Monta Invoice Hook] SO %s: invoice %s not eligible -- skip.",
                        so.name, move.name,
                    )
                    continue

                # Detect first vs renewal invoice.
                # First invoice: brand new customer, no Mollie mandate yet.
                # Mollie only creates a mandate after the first successful payment.
                # Renewal (2nd+): Mollie mandate is required.
                all_posted_invoices = so.invoice_ids.filtered(
                    lambda inv: inv.move_type == "out_invoice" and inv.state == "posted"
                ).sorted(lambda inv: inv.create_date or inv.invoice_date)
                is_first_invoice = (
                    len(all_posted_invoices) == 1
                    and all_posted_invoices[0].id == move.id
                )

                if is_first_invoice:
                    _logger.info(
                        "[Monta Invoice Hook] SO %s: FIRST invoice %s "
                        "-- no Mollie check required (new customer).",
                        so.name, move.name,
                    )
                else:
                    # Renewal invoices require a valid Mollie mandate
                    if not self._monta_has_valid_mollie_mandate(so):
                        _logger.info(
                            "[Monta Invoice Hook] SO %s: RENEWAL invoice %s -- "
                            "Mollie mandate invalid, no delivery created.",
                            so.name, move.name,
                        )
                        continue

                # Guard: no duplicate delivery for this period
                if self._monta_delivery_already_exists(move, so):
                    continue

                # Create the delivery
                try:
                    _logger.info(
                        "[Monta Invoice Hook] Creating delivery for SO %s "
                        "invoice %s (%s).",
                        so.name, move.name,
                        "first invoice" if is_first_invoice else "renewal",
                    )
                    picking = so._monta_create_subscription_delivery(invoice=move)
                    if picking:
                        _logger.info(
                            "[Monta Invoice Hook] Delivery %s created for SO %s.",
                            picking.name, so.name,
                        )
                    else:
                        _logger.warning(
                            "[Monta Invoice Hook] No delivery created for SO %s "
                            "invoice %s. Check logs above.",
                            so.name, move.name,
                        )
                except Exception:
                    _logger.exception(
                        "[Monta Invoice Hook] Error creating delivery "
                        "for SO %s (invoice %s).",
                        so.name, move.name,
                    )

        except Exception:
            # Safety net: our code must NEVER crash Odoo's invoice posting.
            _logger.exception(
                "[Monta Invoice Hook] Unexpected error in Monta hook -- "
                "Odoo's invoice posting was NOT affected."
            )

        # Step 3: Always return Odoo's result
        return res

    # -------------------------------------------------------------------------
    # Guard helpers
    # -------------------------------------------------------------------------

    @api.model
    def _monta_get_subscription_so(self, move):
        """
        Return the in-progress subscription Sale Order linked to this invoice.
        Returns None if the invoice is not from a qualifying subscription.

        Link path (Odoo 17/18):
          account.move -> invoice_line_ids -> sale_line_ids -> order_id
        """
        so = move.invoice_line_ids.mapped("sale_line_ids.order_id")[:1]
        if not so:
            return None

        # Must be an in-progress subscription
        f = so._fields
        if "subscription_state" in f:
            if so.subscription_state != "3_progress":
                _logger.debug(
                    "[Monta Invoice Hook] SO %s: subscription_state='%s' != '3_progress' -- skip.",
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
                "[Monta Invoice Hook] SO %s: no Monta config for company %s -- skip.",
                so.name, so.company_id.name,
            )
            return None

        return so

    @api.model
    def _monta_is_subscription_invoice(self, move, so):
        """
        Return True if this invoice should trigger a Monta delivery.
        All posted subscription invoices qualify (first and renewals).
        """
        all_posted = so.invoice_ids.filtered(
            lambda inv: inv.move_type == "out_invoice" and inv.state == "posted"
        )
        return bool(all_posted)

    @api.model
    def _monta_has_valid_mollie_mandate(self, so):
        """
        Return True if the SO's customer has a valid Mollie mandate.
        Returns True (no block) if Mollie is not installed on this instance.
        Only called for renewal invoices (2nd invoice onwards).
        """
        partner = so.partner_id
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
                "[Monta Invoice Hook] SO %s [%s]: No Mollie Customer ID -- skip.",
                so.name, partner.name,
            )
            return False
        if not mollie_mandate:
            _logger.info(
                "[Monta Invoice Hook] SO %s [%s]: No Mollie Mandate ID -- skip.",
                so.name, partner.name,
            )
            return False
        if mollie_status != "valid":
            _logger.info(
                "[Monta Invoice Hook] SO %s [%s]: Mandate status '%s' != 'valid' -- skip.",
                so.name, partner.name, mollie_status,
            )
            return False

        return True

    @api.model
    def _monta_delivery_already_exists(self, move, so):
        """
        Duplicate guard - return True if a non-cancelled outgoing delivery
        already exists for this SO created at or after this invoice was posted.

        Prevents double-delivery if the invoice is reset-to-draft and re-posted.
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
                "invoice %s (created %s) -- skipping duplicate.",
                so.name, existing.name, move.name, existing.create_date,
            )
            return True

        return False
