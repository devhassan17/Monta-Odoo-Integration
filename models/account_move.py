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
    # Override _post - hook AFTER Odoo's default posting
    # -------------------------------------------------------------------------

    def _post(self, soft=True):
        """
        Odoo's core invoice posting method - extended for Monta renewals.

        Overriding _post instead of action_post ensures that all invoices,
        including those posted automatically via background payment transactions,
        e-commerce checkout, or cron jobs, trigger this hook.

        Order of execution:
          1. super()._post(soft=soft) runs first (100% of Odoo's default logic)
          2. Our code runs after, isolated in try/except
          3. Odoo's result is always returned regardless
        """
        # Step 1: Odoo's full default posting runs first
        res = super()._post(soft=soft)

        # Step 2: Create renewal delivery for qualifying invoices.
        # Wrapped in try/except -- any bug here is logged silently and
        # NEVER propagates to affect Odoo's cron or invoice posting.
        try:
            _logger.info("[Monta Invoice Hook] _post triggered for %s invoices.", len(self))
            for move in self:
                _logger.info(
                    "[Monta Invoice Hook] Evaluating invoice %s: state=%s, type=%s, origin=%s",
                    move.name, move.state, move.move_type, move.invoice_origin
                )
                if move.state != "posted":
                    _logger.info("[Monta Invoice Hook] Invoice %s is not posted. Skipping.", move.name)
                    continue
                if move.move_type != "out_invoice":
                    _logger.info("[Monta Invoice Hook] Invoice %s is not out_invoice (type=%s). Skipping.", move.name, move.move_type)
                    continue

                # ── Refund / reversal guards ──────────────────────────────────
                # When someone creates a refund (credit note), Odoo may also
                # generate a replacement out_invoice in the same flow.  We must
                # NOT treat these replacement invoices as subscription renewals.

                # Guard 1: This invoice is itself a reversal of another invoice
                if hasattr(move, 'reversed_entry_id') and move.reversed_entry_id:
                    _logger.info(
                        "[Monta Invoice Hook] Invoice %s is a reversal of %s. Skipping.",
                        move.name, move.reversed_entry_id.name,
                    )
                    continue

                # Guard 2: This invoice was created by reversing another invoice
                # (replacement invoice in a "cancel and re-create" refund flow)
                if hasattr(move, 'reversal_move_id') and move.reversal_move_id:
                    _logger.info(
                        "[Monta Invoice Hook] Invoice %s has reversal_move_id (linked to reversal). Skipping.",
                        move.name,
                    )
                    continue

                # Guard 3: Invoice has a debit origin (debit note / correction)
                if hasattr(move, 'debit_origin_id') and move.debit_origin_id:
                    _logger.info(
                        "[Monta Invoice Hook] Invoice %s has debit_origin_id %s. Skipping.",
                        move.name, move.debit_origin_id.name,
                    )
                    continue

                # Guard 4: Zero or negative total amount (likely a credit/adjustment)
                if move.amount_total <= 0:
                    _logger.info(
                        "[Monta Invoice Hook] Invoice %s has amount_total=%.2f (≤ 0). Skipping.",
                        move.name, move.amount_total,
                    )
                    continue

                # Guard 5: Context-based — check if this was triggered from a refund wizard
                if self.env.context.get('active_model') == 'account.move.reversal':
                    _logger.info(
                        "[Monta Invoice Hook] Invoice %s created from refund wizard context. Skipping.",
                        move.name,
                    )
                    continue

                # Resolve the linked in-progress subscription SO
                so = self._monta_get_subscription_so(move)
                if not so:
                    _logger.info("[Monta Invoice Hook] Invoice %s: Could not resolve a qualifying subscription SO.", move.name)
                    continue

                _logger.info(
                    "[Monta Invoice Hook] Invoice %s posted for SO %s -- creating delivery.",
                    move.name, so.name,
                )

                # Skip if a delivery already exists for this specific invoice (idempotency guard)
                if self._monta_renewal_delivery_exists(move, so):
                    _logger.info(
                        "[Monta Invoice Hook] Delivery already exists for invoice %s on SO %s — skipping.",
                        move.name, so.name,
                    )
                    continue

                # Create the delivery and ensure it is pushed to Monta
                try:
                    picking = so._monta_create_subscription_delivery(invoice=move)
                    if picking:
                        _logger.info(
                            "[Monta Invoice Hook] Delivery %s created for SO %s (invoice %s).",
                            picking.name, so.name, move.name,
                        )
                        # If delivery was created but push didn't happen, force it now
                        if not picking.monta_pushed:
                            _logger.info(
                                "[Monta Invoice Hook] Delivery %s not yet pushed — pushing now.",
                                picking.name,
                            )
                            picking.with_context(monta_create_delivery=True).action_push_to_monta()
                    else:
                        _logger.warning(
                            "[Monta Invoice Hook] No delivery returned for SO %s invoice %s.",
                            so.name, move.name,
                        )
                except Exception as e:
                    _logger.exception(
                        "[Monta Invoice Hook] Error creating renewal delivery for SO %s (invoice %s): %s",
                        so.name, move.name, e,
                    )

        except Exception:
            # Safety net — our code must never crash Odoo's invoice posting
            _logger.exception(
                "[Monta Invoice Hook] Unexpected error — Odoo invoice posting was NOT affected."
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
        _logger.info("[Monta Invoice Hook] _monta_get_subscription_so for %s", move.name)
        
        # 1. Try sale_line_ids
        so = move.invoice_line_ids.mapped("sale_line_ids.order_id")[:1]
        _logger.info("[Monta Invoice Hook] SO from sale_line_ids mapping: %s", so.name if so else "None")
        
        # 2. Try invoice_origin fallback
        if not so and move.invoice_origin:
            _logger.info("[Monta Invoice Hook] Fallback: Searching SO by origin name '%s'", move.invoice_origin)
            so = self.env["sale.order"].sudo().search([("name", "=", move.invoice_origin)], limit=1)
            _logger.info("[Monta Invoice Hook] Fallback SO found: %s", so.name if so else "None")
        
        if not so:
            _logger.info("[Monta Invoice Hook] No Sale Order found for invoice %s.", move.name)
            return None

        # Must be a subscription SO — regular orders already get Odoo-native delivery at SO confirmation.
        # plan_id is the most reliable indicator; fall back to is_subscription / subscription_state.
        f = so._fields
        is_sub = (
            ('plan_id' in f and bool(so.plan_id))
            or ('is_subscription' in f and so.is_subscription)
            or ('subscription_state' in f and getattr(so, 'subscription_state', '') in (
                '1_draft', '2_renewal', '3_progress', '4_paused',
            ))
        )
        if not is_sub:
            _logger.info(
                "[Monta Invoice Hook] SO %s: not a subscription — skipping "
                "(Odoo native delivery already handles regular orders).",
                so.name,
            )
            return None

        # Skip BC orders
        if so.name and so.name.startswith("BC"):
            _logger.info("[Monta Invoice Hook] SO %s: Skipping BC order.", so.name)
            return None

        # SO must be confirmed (sale or done)
        if so.state not in ("sale", "done"):
            _logger.info("[Monta Invoice Hook] SO %s: state=%s, not confirmed — skipping.", so.name, so.state)
            return None

        # Monta must be configured for this company
        cfg = self.env["monta.config"].sudo().get_for_company(so.company_id)
        if not cfg:
            _logger.info("[Monta Invoice Hook] SO %s: No Monta config for company %s — skipping.", so.name, so.company_id.name)
            return None

        return so

    @api.model
    def _monta_renewal_delivery_exists(self, move, so):
        """
        Duplicate guard -- return True if an invoice-driven delivery
        already exists for this SO and this specific invoice.

        Looks for a delivery with origin containing 'Subscription Renewal - {invoice_name}'.
        """
        invoice_ref = move.name if move else "Renewal"
        existing = self.env["stock.picking"].sudo().search([
            ("sale_id", "=", so.id),
            ("picking_type_code", "=", "outgoing"),
            ("state", "!=", "cancel"),
            ("origin", "like", f"Subscription Renewal - {invoice_ref}"),
        ], limit=1)

        if existing:
            _logger.info(
                "[Monta Invoice Hook] SO %s: delivery %s already exists for invoice %s "
                "-- skipping duplicate.",
                so.name, existing.name, invoice_ref,
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
