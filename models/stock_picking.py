# -*- coding: utf-8 -*-
import json
import logging

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = "stock.picking"

    monta_pushed = fields.Boolean(string="Pushed to Monta", default=False, copy=False)
    monta_webshop_order_id = fields.Char(string="Monta Webshop Order ID", copy=False, index=True)
    monta_last_push = fields.Datetime(string="Monta Last Push", copy=False)

    monta_status = fields.Char(string="Monta Status", copy=False, index=True)
    monta_status_code = fields.Char(string="Monta Status Code", copy=False)
    monta_track_trace = fields.Char(string="Monta Track & Trace", copy=False)
    monta_delivery_date = fields.Date(string="Monta Delivery Date", copy=False)

    def _is_monta_push_eligible(self):
        """Check if this picking should be pushed to Monta."""
        self.ensure_one()
        if self.picking_type_code != 'outgoing':
            return False
        if not self.sale_id:
            return False
        cfg = self.env["monta.config"].sudo().get_for_company(self.company_id)
        if not cfg:
            return False
        if self.sale_id.name and self.sale_id.name.startswith("BC"):
            return False
            
        # Route Filter (Delivery Product Route)
        if cfg.enable_route_filter and cfg.monta_route_ids:
            carrier = getattr(self.sale_id, 'carrier_id', False)
            has_matching_route = False
            debug_checked_routes = []
            
            def get_all_routes_for_product(product):
                routes = set()
                if not product:
                    return routes
                # 1. Product routes
                if hasattr(product, 'route_ids') and product.route_ids:
                    routes.update(product.route_ids.ids)
                # 2. Product Template routes
                if hasattr(product, 'product_tmpl_id') and product.product_tmpl_id and hasattr(product.product_tmpl_id, 'route_ids'):
                    routes.update(product.product_tmpl_id.route_ids.ids)
                # 3. Delivery Carrier custom routes (e.g. Odoo Studio fields)
                carriers = product.env['delivery.carrier'].sudo().search([('product_id', '=', product.id)])
                for c in carriers:
                    for field_name in ['route_ids', 'x_studio_route_ids', 'x_route_ids', 'x_route_id']:
                        if hasattr(c, field_name):
                            val = getattr(c, field_name)
                            if val:
                                # Handle both Many2one and Many2many fields
                                routes.update(val.ids if hasattr(val, 'ids') else [val.id])
                return routes

            # 1. Try to check the carrier product first
            if carrier and carrier.product_id:
                debug_checked_routes = list(get_all_routes_for_product(carrier.product_id))
                if set(cfg.monta_route_ids.ids).intersection(set(debug_checked_routes)):
                    has_matching_route = True

            # 2. If no match yet, check all products on the Sales Order (useful for API/Webshop orders where carrier_id is empty)
            if not has_matching_route:
                for line in self.sale_id.order_line:
                    if line.product_id:
                        p_routes = get_all_routes_for_product(line.product_id)
                        if set(cfg.monta_route_ids.ids).intersection(p_routes):
                            has_matching_route = True
                            break
                        if getattr(line, 'is_delivery', False):
                            debug_checked_routes = list(p_routes)  # Keep delivery line routes for debug log
            
            if not has_matching_route:
                _logger.info(
                    "[Monta Skip] Picking %s skipped because no delivery/order product matches configured Monta Routes. "
                    "(Carrier: %s, Checked Routes: %s, Config Routes: %s)",
                    self.name,
                    carrier.name if carrier else 'None',
                    debug_checked_routes,
                    cfg.monta_route_ids.ids
                )
                return False

        # Subscription Mandate Filter (only required for renewals)
        f = self.sale_id._fields
        is_sub = (
            ('is_subscription' in f and self.sale_id.is_subscription)
            or ('plan_id' in f and bool(self.sale_id.plan_id))
            or ('subscription_state' in f and getattr(self.sale_id, 'subscription_state', '') in (
                '2_renewal', '3_progress', '4_paused'
            ))
        )
        if is_sub and not self._monta_is_first_delivery():
            order = self.sale_id
            partner = order.partner_id
            mollie_cust = getattr(partner, 'mollie_customer_id', False) or getattr(order, 'mollie_customer_id', False)
            mollie_mandate = getattr(partner, 'mollie_mandate_id', False) or getattr(order, 'mollie_mandate_id', False)
            mollie_status = getattr(partner, 'mollie_mandate_status', '') or getattr(order, 'mollie_mandate_status', '')
            
            if not mollie_cust or not mollie_mandate or mollie_status != 'valid':
                return False

        return True

    def _monta_is_first_delivery(self):
        """Returns True if this is the first (successful or pending) Monta push for the related Sales Order."""
        self.ensure_one()
        if not self.sale_id:
            return False
            
        # The first delivery is simply the oldest outgoing picking created for this SO.
        first_picking = self.search([
            ("sale_id", "=", self.sale_id.id),
            ("picking_type_code", "=", "outgoing")
        ], order="id asc", limit=1)
        
        return first_picking and first_picking.id == self.id

    def _monta_make_webshop_order_id(self, so):
        self.ensure_one()
        if self.monta_webshop_order_id:
            return self.monta_webshop_order_id

        so_name = (so.name or "").replace("/", "-")
        
        # Smart ID: First delivery uses SO name, subsequent use unique ID
        if self._monta_is_first_delivery():
            webshop_order_id = so.name
        else:
            webshop_order_id = f"{so_name}-PICK{self.id}"
            
        self.write({"monta_webshop_order_id": webshop_order_id})
        return webshop_order_id

    def _prepare_monta_lines(self):
        """Build Monta lines from stock moves in the picking."""
        self.ensure_one()
        components = []
        # Use move_ids for better compatibility across Odoo versions/configs
        for m in self.move_ids:
            if m.product_id and m.product_uom_qty > 0:
                components.append((m.product_id, m.product_uom_qty))
        
        if not self.sale_id:
             return []
             
        lines = self.sale_id._prepare_monta_lines_from_components(components)
        
        # Log the specific lines being sent
        product_summary = ", ".join([f"{l['Sku']} (qty {l['OrderedQuantity']})" for l in lines])
        _logger.info("[Monta Push] %s: Preparing payload with %s products: %s", self.name, len(lines), product_summary)
        
        return lines

    def _monta_prepare_payload(self, so, webshop_order_id):
        """Reuse existing sale.order payload generator but overwrite lines with picking contents."""
        self.ensure_one()
        payload = so._prepare_monta_order_payload()
        
        # Overwrite lines with what's actually in THIS picking
        payload["Lines"] = self._prepare_monta_lines()
        
        payload["WebshopOrderId"] = webshop_order_id
        payload["Reference"] = (self.name or "").strip()
        
        # Recalculate invoice amount if possible? 
        # Actually, if it's a partial shipment, we might still send the total amount?
        # Usually WMS wants the order total for the first shipment.
        # For now, keeping the SO total to avoid complexity.
        
        payload["Invoice"]["WebshopFactuurID"] = int(self.id) if self.id else 9999
        return payload

    def _monta_ensure_untracked_products(self):
        """Disables Lot/Serial tracking for all products in this picking to prevent validation blockers."""
        self.ensure_one()
        for move in self.move_ids:
            if move.product_id.tracking != 'none':
                _logger.info("[Monta] Disabling tracking for product %s to allow WMS fulfillment", move.product_id.display_name)
                move.product_id.sudo().write({'tracking': 'none'})

    def _monta_auto_validate_immediately(self):
        """Helper to quickly set quantities and validate picking."""
        self.ensure_one()
        if self.state in ("done", "cancel"):
            return
        _logger.info("[Monta] Immediately auto-validating picking %s after successful push", self.name)
        for move in self.move_ids:
            if move.state not in ("done", "cancel"):
                # Odoo 18 quantity field assignment
                move.quantity = move.product_uom_qty
        return self.with_context(skip_backorder=True, picking_label_report=False).button_validate()

    def action_push_to_monta(self, sale_order=None):
        """Pushes the picking to Monta."""
        self.ensure_one()
        if not self._is_monta_push_eligible():
            return False

        if not sale_order:
            sale_order = self.sale_id
            
        # Clear lot tracking blockers before sending to Monta
        self._monta_ensure_untracked_products()

        webshop_order_id = self._monta_make_webshop_order_id(sale_order)
        is_renewal = not (webshop_order_id == sale_order.name)

        # Idempotency guard
        Status = self.env["monta.order.status"].sudo()
        existing = Status.search(
            [
                ("order_name", "=", webshop_order_id),
                ("status", "in", ["Sent", "sent"]),
            ],
            limit=1,
        )
        if existing and not self.env.context.get("force_send_to_monta"):
            return True

        # Snapshot creation
        kind = "renewal" if is_renewal else "sale"
        if is_renewal:
            Status.upsert_for_renewal(
                sale_order, self, webshop_order_id,
                status="Not sent", status_code=0, source="orders",
                status_raw=json.dumps({"note": "Push initiated"}, ensure_ascii=False)
            )
        else:
            Status.upsert_for_order(
                sale_order,
                status="Not sent", status_code=0, source="orders",
                status_raw=json.dumps({"note": "Push initiated"}, ensure_ascii=False)
            )

        payload = self._monta_prepare_payload(sale_order, webshop_order_id)
        status, body = sale_order._monta_request("POST", "/order", payload)

        if 200 <= status < 300 or self._monta_is_duplicate_exists_error(status, body):
            monta_ref = self._monta_extract_monta_ref(body, webshop_order_id)
            vals_status = {
                "status": "Sent",
                "status_code": status,
                "source": "orders",
                "monta_order_ref": monta_ref,
                "status_raw": json.dumps(body or {}, ensure_ascii=False),
                "last_sync": fields.Datetime.now(),
            }
            if is_renewal:
                Status.upsert_for_renewal(sale_order, self, webshop_order_id, **vals_status)
            else:
                Status.upsert_for_order(sale_order, **vals_status)

            self.write({
                "monta_pushed": True,
                "monta_last_push": fields.Datetime.now(),
                "monta_status": "Sent to Monta",  # Will be updated by status sync cron
            })
            
            # Immediately validate in Odoo so user doesn't have to click it
            self._monta_auto_validate_immediately()
            
            return True

        # Error handling
        vals_err = {
            "status": "Error",
            "status_code": status,
            "source": "orders",
            "status_raw": json.dumps(body or {}, ensure_ascii=False),
            "last_sync": fields.Datetime.now(),
        }
        if is_renewal:
            Status.upsert_for_renewal(sale_order, self, webshop_order_id, **vals_err)
        else:
            Status.upsert_for_order(sale_order, **vals_err)
        return False

    def _monta_is_duplicate_exists_error(self, status, body):
        if status != 400 or not isinstance(body, dict):
            return False
        reasons = body.get("OrderInvalidReasons") or []
        for r in reasons:
            msg = (r or {}).get("Message") or ""
            if "already exists" in msg.lower():
                return True
        return False

    def _monta_extract_monta_ref(self, body, fallback):
        if isinstance(body, dict):
            for k in ("OrderId", "orderId", "Id", "id", "MontaOrderId", "montaOrderId", "OrderNumber", "orderNumber"):
                v = body.get(k)
                if v:
                    return str(v)
        return fallback

    def action_confirm(self):
        res = super(StockPicking, self).action_confirm()
        for picking in self:
            if picking._is_monta_push_eligible() and not picking.monta_pushed:
                picking.action_push_to_monta()
        return res

    def action_cancel(self):
        res = super(StockPicking, self).action_cancel()
        for picking in self:
            if picking.monta_pushed:
                # Attempt to cancel in Monta if pushed
                try:
                    webshop_id = picking.monta_webshop_order_id or picking.name
                    sale_order = picking.sale_id
                    if sale_order:
                        headers = {"Content-Type": "application/json-patch+json", "Accept": "application/json"}
                        sale_order._monta_request("DELETE", f"/order/{webshop_id}", {"Note": "Delivery Cancelled in Odoo"}, headers=headers)
                except Exception:
                    _logger.warning("Failed to cancel delivery %s in Monta", picking.name)
        return res

    def action_send_renewal_to_monta(self, sale_order=None):
        """Deprecated/Legacy method kept for status button compatibility if needed."""
        return self.action_push_to_monta(sale_order=sale_order)
