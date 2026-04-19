# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    """
    Hooks into invoice posting to detect subscription renewals and create
    a new outgoing delivery for each renewed period, then push it to Monta.
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
                # Also try via invoice_origin (fallback)
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

            # Only create a new delivery if ALL existing outgoing deliveries
            # for this SO are already done/cancelled (renewal scenario).
            open_pickings = so.picking_ids.filtered(
                lambda p: p.picking_type_code == 'outgoing'
                and p.state not in ('done', 'cancel')
            )
            if open_pickings:
                _logger.info(
                    "[Monta] Renewal invoice %s for SO %s: skipping new delivery "
                    "(there are still %d open deliveries)",
                    move.name, so.name, len(open_pickings),
                )
                continue

            # ------------------------------------------------------------------
            # CRITICAL GUARD: skip if this is the FIRST invoice for this SO.
            # The first delivery was already pushed to Monta automatically when
            # the SO was confirmed (via stock_picking.action_confirm() override).
            # Only 2nd+ invoices represent genuine subscription renewals that
            # need a fresh delivery. Without this check, manually clicking
            # "Create Invoice" would send a duplicate delivery to Monta.
            # ------------------------------------------------------------------
            prior_invoices = so.invoice_ids.filtered(
                lambda inv: inv.id != move.id
                and inv.move_type == 'out_invoice'
                and inv.state == 'posted'
            )
            if not prior_invoices:
                _logger.info(
                    "[Monta] Invoice %s is the first invoice for SO %s — "
                    "skipping renewal delivery (initial delivery already sent via action_confirm).",
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

        # Odoo 17/18: is_subscription boolean
        if 'is_subscription' in f and so.is_subscription:
            return True

        # Odoo 17/18: plan_id indicates a recurring subscription plan
        if 'plan_id' in f and so.plan_id:
            return True

        # Odoo 16/17: subscription_id links to parent subscription SO
        if 'subscription_id' in f and so.subscription_id:
            return True

        # Odoo 16: subscription_management flag
        if 'subscription_management' in f and so.subscription_management in ('renew', 'upsell'):
            return True

        # Odoo 17/18: subscription_state is set for recurring orders
        if 'subscription_state' in f and so.subscription_state in (
            '1_draft', '2_renewal', '3_progress', '4_paused', '5_closed', '6_churn'
        ):
            return True

        # Odoo 17/18: recurring_monthly or recurring_total is set
        if 'recurring_monthly' in f and so.recurring_monthly:
            return True

        return False

    def _monta_create_renewal_delivery(self, so, invoice):
        """
        Create a new outgoing stock picking for a subscription renewal.
        Lines are taken from the invoice (so quantities match the invoiced period).
        The picking is confirmed immediately, which triggers action_push_to_monta()
        via the stock_picking.action_confirm() override.
        """
        # ---- Find the outgoing picking type ----
        warehouse = so.warehouse_id
        if not warehouse:
            warehouse = self.env['stock.warehouse'].sudo().search(
                [('company_id', '=', so.company_id.id)], limit=1
            )

        picking_type = None
        if warehouse:
            picking_type = warehouse.out_type_id
        if not picking_type:
            picking_type = self.env['stock.picking.type'].sudo().search([
                ('code', '=', 'outgoing'),
                ('company_id', '=', so.company_id.id),
            ], limit=1)

        if not picking_type:
            _logger.warning("[Monta] No outgoing picking type for SO %s", so.name)
            return None

        src_loc = (
            picking_type.default_location_src_id
            or warehouse.lot_stock_id
        )
        dest_loc = (
            picking_type.default_location_dest_id
            or self.env.ref('stock.stock_location_customers', raise_if_not_found=False)
        )
        if not src_loc or not dest_loc:
            _logger.warning("[Monta] Cannot resolve locations for renewal delivery of SO %s", so.name)
            return None

        # ---- Build move lines from the invoice lines ----
        move_vals = []
        for inv_line in invoice.invoice_line_ids:
            product = inv_line.product_id
            if not product:
                continue
            if product.type not in ('product', 'consu'):
                continue  # Skip service lines
            if inv_line.quantity <= 0:
                continue

            # Try to get the sale order line for proper linkage
            sol = inv_line.sale_line_ids[:1]

            move_vals.append({
                'name': product.name or product.display_name,
                'product_id': product.id,
                'product_uom_qty': inv_line.quantity,
                'product_uom': inv_line.product_uom_id.id or product.uom_id.id,
                'location_id': src_loc.id,
                'location_dest_id': dest_loc.id,
                'sale_line_id': sol.id if sol else False,
                'company_id': so.company_id.id,
            })

        if not move_vals:
            _logger.warning(
                "[Monta] No deliverable product lines found in invoice %s for SO %s",
                invoice.name, so.name,
            )
            return None

        # ---- Create the picking ----
        # Strip all 'default_*' context keys coming from the invoice form
        # (e.g. default_move_type='out_invoice') which would bleed into the
        # stock.picking creation and cause "Wrong value for move_type" errors.
        clean_ctx = {k: v for k, v in self.env.context.items() if not k.startswith('default_')}
        picking = self.env['stock.picking'].sudo().with_context(clean_ctx).create({
            'picking_type_id': picking_type.id,
            'partner_id': so.partner_id.id,
            'origin': f"{so.name} (Renewal: {invoice.name})",
            'sale_id': so.id,
            'location_id': src_loc.id,
            'location_dest_id': dest_loc.id,
            'company_id': so.company_id.id,
            'move_type': 'direct',  # "As soon as possible" — explicit to avoid any context bleeding
            'move_ids': [(0, 0, v) for v in move_vals],
        })

        # Confirm the picking → triggers stock_picking.action_confirm()
        # which calls action_push_to_monta() automatically
        picking.action_confirm()

        # Post a note on the SO for traceability
        so.message_post(
            body=(
                f"📦 Renewal delivery <b>{picking.name}</b> created automatically "
                f"for invoice <b>{invoice.name}</b> and queued for Monta."
            )
        )

        return picking
