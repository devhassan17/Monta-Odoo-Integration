# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    """
    Hooks into invoice posting to detect subscription renewals and create
    a new outgoing delivery for each renewed period, then push it to Monta.

    We hook here because the Odoo subscription invoice cron posts the invoice
    via action_post() — that is the natural renewal event, without needing a
    separate cron job.

    NO subscription dates, NO confirm calls, NO interference with Odoo's
    native subscription behaviour.
    """
    _inherit = "account.move"

    # ------------------------------------------------------------------
    # Legacy / deprecated fields kept so database views don't break
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Invoice posting hook
    # ------------------------------------------------------------------
    def action_post(self):
        res = super().action_post()

        for move in self:
            # Only customer invoices
            if move.move_type != 'out_invoice':
                continue

            # Resolve the sale order linked to this invoice
            so = move.invoice_line_ids.mapped('sale_line_ids.order_id')[:1]
            if not so:
                if move.invoice_origin:
                    so = self.env['sale.order'].sudo().search(
                        [('name', '=', move.invoice_origin)], limit=1
                    )
            if not so:
                continue

            # Only process subscription SOs
            if not self._monta_is_subscription_so(so):
                continue

            # Skip BC orders and company-not-allowed
            if so.name and so.name.startswith('BC'):
                continue
            cfg = self.env['monta.config'].sudo().get_for_company(so.company_id)
            if not cfg:
                continue

            # Skip if there are still open (not-done) deliveries for this SO
            open_pickings = so.picking_ids.filtered(
                lambda p: p.picking_type_code == 'outgoing'
                and p.state not in ('done', 'cancel')
            )
            if open_pickings:
                _logger.info(
                    "[Monta] Invoice %s for SO %s: open deliveries exist, skipping renewal.",
                    move.name, so.name,
                )
                continue

            # ---------------------------------------------------------------
            # FIRST-INVOICE GUARD
            # The very first invoice corresponds to the initial delivery that
            # was already pushed to Monta when the SO was confirmed (via the
            # stock_picking.action_confirm() hook).
            # Only 2nd+ invoices represent genuine subscription renewals that
            # need a fresh delivery.
            # Without this guard, manually clicking "Create Invoice" on a new
            # subscription would send a duplicate delivery to Monta.
            # ---------------------------------------------------------------
            prior_invoices = so.invoice_ids.filtered(
                lambda inv: inv.id != move.id
                and inv.move_type == 'out_invoice'
                and inv.state == 'posted'
            )
            if not prior_invoices:
                _logger.info(
                    "[Monta] Invoice %s is the first invoice for SO %s — "
                    "initial delivery already sent via stock_picking hook. Skipping.",
                    move.name, so.name,
                )
                continue

            # Create a new delivery for this renewal period and push to Monta
            try:
                picking = self._monta_create_renewal_delivery(so, move)
                if picking:
                    _logger.info(
                        "[Monta] Created renewal delivery %s for SO %s (invoice %s)",
                        picking.name, so.name, move.name,
                    )
            except Exception as e:
                _logger.warning(
                    "[Monta] Failed to create renewal delivery for SO %s (invoice %s): %s",
                    so.name, move.name, e,
                )

        return res

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @api.model
    def _monta_is_subscription_so(self, so):
        """Return True if the sale order is a subscription (any Odoo version)."""
        f = so._fields

        if 'is_subscription' in f and so.is_subscription:
            return True
        if 'plan_id' in f and so.plan_id:
            return True
        if 'subscription_id' in f and so.subscription_id:
            return True
        if 'subscription_management' in f and so.subscription_management in ('renew', 'upsell'):
            return True
        if 'subscription_state' in f and so.subscription_state in (
            '1_draft', '2_renewal', '3_progress', '4_paused', '5_closed', '6_churn'
        ):
            return True
        if 'recurring_monthly' in f and so.recurring_monthly:
            return True

        return False

    def _monta_create_renewal_delivery(self, so, invoice):
        """
        Create a new outgoing stock picking for a subscription renewal.
        Lines are taken from the SO lines (same products/qty as the original).
        Confirming the picking triggers stock_picking.action_confirm() which
        calls action_push_to_monta() automatically.
        """
        # ---- Locate outgoing picking type ----
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

        # ---- Build move lines from SO order lines ----
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
            _logger.warning("[Monta] No storable lines on SO %s", so.name)
            return None

        # ---- Create picking — strip default_* context to avoid move_type clash ----
        clean_ctx = {k: v for k, v in self.env.context.items() if not k.startswith('default_')}
        picking = self.env['stock.picking'].sudo().with_context(clean_ctx).create({
            'picking_type_id': picking_type.id,
            'partner_id': so.partner_id.id,
            'origin': f"{so.name} (Renewal: {invoice.name})",
            'sale_id': so.id,
            'location_id': src_loc.id,
            'location_dest_id': dest_loc.id,
            'company_id': so.company_id.id,
            'move_type': 'direct',
            'move_ids': [(0, 0, v) for v in move_vals],
        })

        # Confirm → triggers stock_picking.action_confirm() → action_push_to_monta()
        picking.action_confirm()

        so.message_post(
            body=(
                f"📦 Renewal delivery <b>{picking.name}</b> created automatically "
                f"for invoice <b>{invoice.name}</b> and queued for Monta."
            )
        )
        return picking
