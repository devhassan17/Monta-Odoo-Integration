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
                    _logger.info("[Monta Invoice Hook] Invoice %s is not out_invoice. Skipping.", move.name)
                    continue

                # Resolve the linked in-progress subscription SO
                so = self._monta_get_subscription_so(move)
                if not so:
                    _logger.info("[Monta Invoice Hook] Invoice %s: Could not resolve a qualifying subscription SO.", move.name)
                    continue

                _logger.info(
                    "[Monta Invoice Hook] Invoice %s posted for SO %s -- evaluating renewal delivery.",
                    move.name, so.name,
                )

                # Detect if this is the first posted invoice for this subscription
                all_posted = so.invoice_ids.filtered(
                    lambda inv: inv.move_type == "out_invoice" and inv.state == "posted"
                ).sorted(lambda inv: inv.create_date or inv.invoice_date)

                is_first_invoice = bool(all_posted and all_posted[0].id == move.id)

                if is_first_invoice:
                    _logger.info(
                        "[Monta Invoice Hook] SO %s: First invoice %s -- "
                        "Mollie mandate check bypassed.",
                        so.name, move.name,
                    )
                else:
                    # Renewal invoice (2nd+) requires valid Mollie mandate
                    if not self._monta_has_valid_mollie_mandate(so):
                        _logger.info(
                            "[Monta Invoice Hook] SO %s: renewal invoice %s -- "
                            "Mollie mandate invalid, no delivery created.",
                            so.name, move.name,
                        )
                        # Post detailed chatter logs
                        msg_fail = (
                            f"⚠️ <b>Monta Integration:</b> Mollie mandate validation failed. "
                            f"No delivery created for subscription SO <b>{so.name}</b>."
                        )
                        move.message_post(body=msg_fail)
                        so.message_post(body=msg_fail)
                        continue

                # Skip if an invoice-driven delivery already exists for this specific invoice
                if self._monta_renewal_delivery_exists(move, so):
                    msg_dup = (
                        f"ℹ️ <b>Monta Integration:</b> A delivery for invoice <b>{move.name}</b> "
                        f"already exists. Skipping duplicate delivery creation."
                    )
                    move.message_post(body=msg_dup)
                    continue

                # Create the renewal delivery
                try:
                    picking = so._monta_create_subscription_delivery(invoice=move)
                    if picking:
                        _logger.info(
                            "[Monta Invoice Hook] Renewal/Invoice delivery %s created for SO %s.",
                            picking.name, so.name,
                        )
                        product_details = []
                        for move_line in picking.move_ids:
                            product_details.append(f"• {move_line.product_id.name} (Qty: {move_line.product_uom_qty})")
                        details_html = "<br/>".join(product_details)
                        
                        msg_success = (
                            f"📦 <b>Monta Integration:</b> Delivery <b>{picking.name}</b> has been created successfully "
                            f"from this invoice's items:<br/>{details_html}<br/>and queued for Monta."
                        )
                        move.message_post(body=msg_success)
                    else:
                        _logger.warning(
                            "[Monta Invoice Hook] No delivery returned for SO %s invoice %s.",
                            so.name, move.name,
                        )
                        msg_none = (
                            f"⚠️ <b>Monta Integration:</b> Delivery generation returned empty/no products. "
                            f"No Monta picking was created."
                        )
                        move.message_post(body=msg_none)
                except Exception as e:
                    _logger.exception(
                        "[Monta Invoice Hook] Error creating renewal delivery for SO %s (invoice %s).",
                        so.name, move.name,
                    )
                    msg_err = (
                        f"❌ <b>Monta Integration:</b> Error during delivery generation: {str(e)}"
                    )
                    move.message_post(body=msg_err)

        except Exception as e:
            # Safety net -- our code must never crash Odoo's invoice posting
            _logger.exception(
                "[Monta Invoice Hook] Unexpected error -- Odoo invoice posting was NOT affected."
            )
            try:
                self.message_post(body=f"❌ <b>Monta Integration:</b> Unexpected hook error: {str(e)}")
            except Exception:
                pass

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

        # 3. Check if it's a subscription
        f = so._fields
        is_sub = (
            ('is_subscription' in f and so.is_subscription)
            or ('plan_id' in f and bool(so.plan_id))
            or ('subscription_state' in f and getattr(so, 'subscription_state', '') in ('2_renewal', '3_progress', '4_paused', 'draft', 'sent', 'sale'))
        )
        _logger.info(
            "[Monta Invoice Hook] SO %s subscription check: is_subscription=%s, plan_id=%s, subscription_state=%s -> qualified=%s",
            so.name,
            getattr(so, 'is_subscription', 'N/A'),
            getattr(so, 'plan_id', 'N/A'),
            getattr(so, 'subscription_state', 'N/A'),
            is_sub
        )
        if not is_sub:
            return None

        # Skip BC orders
        if so.name and so.name.startswith("BC"):
            _logger.info("[Monta Invoice Hook] SO %s: Skipping BC order.", so.name)
            return None

        # Monta must be configured for this company
        cfg = self.env["monta.config"].sudo().get_for_company(so.company_id)
        if not cfg:
            _logger.info("[Monta Invoice Hook] SO %s: No Monta config found for company %s.", so.name, so.company_id.name)
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
