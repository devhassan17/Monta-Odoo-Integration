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

    def _monta_log_not_sent_reason(self, reason):
        """Helper to post plain text chatter logs on picking and Sales Order when skipped/blocked."""
        self.ensure_one()
        msg = f"⚠️ Monta Integration: Subscription delivery {self.name} is not sent to Monta due to: {reason}"
        self.message_post(body=msg)
        if self.sale_id:
            self.sale_id.message_post(body=msg)

    def _is_monta_push_eligible(self):
        """Check if this picking should be pushed to Monta."""
        self.ensure_one()
        if self.picking_type_code != 'outgoing':
            _logger.debug("[Monta Eligible] %s: NOT outgoing -- skip.", self.name)
            return False
        if self.state in ('cancel', 'done'):
            _logger.debug("[Monta Eligible] %s: state=%s -- skip.", self.name, self.state)
            return False
        if not self.sale_id:
            _logger.debug("[Monta Eligible] %s: no sale_id -- skip.", self.name)
            return False
        cfg = self.env["monta.config"].sudo().get_for_company(self.company_id)
        if not cfg:
            _logger.info("[Monta Eligible] %s: no Monta config for company %s -- skip.", self.name, self.company_id.name)
            return False
        if self.sale_id.name and self.sale_id.name.startswith("BC"):
            _logger.debug("[Monta Eligible] %s: BC order -- skip.", self.name)
            return False

        # Block native/regular deliveries of subscription Sales Orders from being sent to Monta.
        # Subscription orders must ONLY send invoice-driven deliveries (origin contains 'Subscription Renewal').
        _sf = self.sale_id._fields
        _is_sub_so = (
            'Subscription Renewal' in (self.origin or '')
            or ('subscription_state' in _sf and getattr(self.sale_id, 'subscription_state', '') in ('2_renewal', '3_progress', '4_paused'))
            or ('is_subscription' in _sf and self.sale_id.is_subscription)
            or ('plan_id' in _sf and bool(self.sale_id.plan_id))
        )
        if _is_sub_so:
            # Strict state check: Subscription state must be strictly '3_progress' (In Progress)
            # to allow pushing, unless this is a newly created delivery explicitly authorized by context.
            if 'subscription_state' in _sf:
                sub_state = getattr(self.sale_id, 'subscription_state', '')
                if sub_state != '3_progress' and not self.env.context.get('monta_create_delivery'):
                    _logger.info(
                        "[Monta Safety Guard Log] Blocked picking %s for SO %s — "
                        "subscription state is %s, not strictly '3_progress' (In Progress).",
                        self.name, self.sale_id.name, sub_state,
                    )
                    self._monta_log_not_sent_reason(f"Subscription state is {sub_state}, not strictly '3_progress' (In Progress).")
                    return False

            _is_renewal_picking = 'Subscription Renewal' in (self.origin or '')
            if not _is_renewal_picking:
                _logger.info(
                    "[Monta] SO %s: Blocking native delivery %s — "
                    "subscriptions only push invoice-driven deliveries.",
                    self.sale_id.name, self.name,
                )
                self._monta_log_not_sent_reason("Subscriptions only push invoice-driven deliveries (blocking Odoo's native delivery).")
                return False
            
            # --- Safety feature: Block existing waiting/ready pickings ---
            # Do not allow sending renewals that are in 'confirmed' (Waiting) or 'assigned' (Ready) state
            # unless explicitly authorized by context (such as creation or manual sync).
            if self.state in ('confirmed', 'assigned') and not self.env.context.get('monta_create_delivery'):
                _logger.info(
                    "[Monta Safety Guard Log] Blocked existing subscription renewal picking %s in state %s for SO %s — "
                    "already in waiting/ready state and not explicitly forced.",
                    self.name, self.state, self.sale_id.name,
                )
                self._monta_log_not_sent_reason(f"Delivery is in state {self.state} and not explicitly authorized for creation/sync.")
                return False

            # --- Safety feature: Only push the absolute newest renewal picking ---
            # Bypassed when monta_create_delivery=True (explicit creation — we know exactly which picking to push).
            if not self.env.context.get('monta_create_delivery'):
                all_renewals = self.sale_id.picking_ids.filtered(
                    lambda p: p.picking_type_code == "outgoing"
                    and p.state not in ("cancel", "done")
                    and 'Subscription Renewal' in (p.origin or '')
                )
                if all_renewals:
                    latest_renewal = all_renewals.sorted(
                        key=lambda p: (p.create_date or fields.Datetime.now(), p.id),
                        reverse=True
                    )[0]
                    if self.id != latest_renewal.id:
                        _logger.info(
                            "[Monta Safety Guard Log] Blocked older renewal picking %s for SO %s — "
                            "only the absolute most recent subscription delivery (%s) can be pushed.",
                            self.name, self.sale_id.name, latest_renewal.name,
                        )
                        self._monta_log_not_sent_reason(f"Only the absolute most recent subscription delivery ({latest_renewal.name}) can be pushed.")
                        return False
                    else:
                        _logger.info(
                            "[Monta Safety Guard Log] Allowed newest renewal picking %s for SO %s.",
                            self.name, self.sale_id.name
                        )

        # Route Filter (Delivery Product Route)
        if cfg.enable_route_filter:
            # --- Subscription bypass ---
            # Detect if this picking belongs to a subscription SO
            _f = self.sale_id._fields
            _is_sub = (
                'Subscription Renewal' in (self.origin or '')
                or ('is_subscription' in _f and self.sale_id.is_subscription)
                or ('plan_id' in _f and bool(self.sale_id.plan_id))
                or ('subscription_state' in _f and getattr(self.sale_id, 'subscription_state', '') in (
                    '2_renewal', '3_progress', '4_paused'
                ))
            )
            if _is_sub and cfg.route_filter_skip_subscriptions:
                # Subscriptions are allowed through regardless of route configuration
                _logger.info(
                    "[Monta Route] Picking %s belongs to a subscription SO — route filter bypassed "
                    "(Route Filter: Skip Subscriptions is ON).",
                    self.name,
                )
            else:
                # If enabled but no routes are selected in config, block everything (per user request)
                if not cfg.monta_route_ids:
                    _logger.info("[Monta Skip] Picking %s skipped because Route Filter is enabled but no routes are selected in Monta Configuration.", self.name)
                    self._monta_log_not_sent_reason("Route Filter is enabled but no routes are configured in Monta settings.")
                    return False

                carrier = getattr(self.sale_id, 'carrier_id', False)
                has_matching_route = False
                debug_checked_routes = []
                
                def get_all_routes_from_obj(obj):
                    routes = set()
                    if not obj:
                        return routes
                    # 1. Standard routes
                    if hasattr(obj, 'route_ids') and obj.route_ids:
                        routes.update(obj.route_ids.ids)
                    # 2. Product Template routes
                    if hasattr(obj, 'product_tmpl_id') and obj.product_tmpl_id and hasattr(obj.product_tmpl_id, 'route_ids'):
                        routes.update(obj.product_tmpl_id.route_ids.ids)
                    # 3. Custom/Studio fields (x_route_id, x_studio_route_ids, etc.)
                    for field_name in ['x_route_id', 'x_route_ids', 'x_studio_route_id', 'x_studio_route_ids', 'x_route_ids', 'x_route_id']:
                        if hasattr(obj, field_name):
                            val = getattr(obj, field_name)
                            if val:
                                routes.update(val.ids if hasattr(val, 'ids') else [val.id])
                    return routes

                # 1. Check the Delivery Method (Carrier) and its Product
                if carrier:
                    carrier_routes = get_all_routes_from_obj(carrier)
                    if carrier.product_id:
                        carrier_routes.update(get_all_routes_from_obj(carrier.product_id))
                    
                    debug_checked_routes = list(carrier_routes)
                    if set(cfg.monta_route_ids.ids).intersection(carrier_routes):
                        has_matching_route = True

                # 2. If no match yet, check all products on the Sales Order (fallback for API/Webshop orders)
                if not has_matching_route:
                    for line in self.sale_id.order_line:
                        if line.product_id:
                            p_routes = get_all_routes_from_obj(line.product_id)
                            if set(cfg.monta_route_ids.ids).intersection(p_routes):
                                has_matching_route = True
                                break
                
                if not has_matching_route:
                    _logger.info(
                        "[Monta Skip] Picking %s skipped because no delivery/order product matches configured Monta Routes. "
                        "(Carrier: %s, Checked Routes: %s, Config Routes: %s)",
                        self.name,
                        carrier.name if carrier else 'None',
                        debug_checked_routes,
                        cfg.monta_route_ids.ids
                    )
                    self._monta_log_not_sent_reason("Route Filter is enabled but no products or delivery method matched the configured Monta Routes.")
                    return False

        _logger.info(
            "[Monta Eligible] %s (SO %s): ELIGIBLE -- will push to Monta.",
            self.name, self.sale_id.name,
        )
        return True


    def _monta_is_first_delivery(self):
        """Returns True if this is the first (successful or pending) Monta push for the related Sales Order."""
        self.ensure_one()
        if not self.sale_id:
            return False
            
        # The first delivery is simply the oldest non-cancelled outgoing picking created for this SO.
        first_picking = self.search([
            ("sale_id", "=", self.sale_id.id),
            ("picking_type_code", "=", "outgoing"),
            ("state", "!=", "cancel")
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

        # Idempotency guard: Monta Status check
        Status = self.env["monta.order.status"].sudo()
        existing = Status.search(
            [
                ("order_name", "=", webshop_order_id),
                ("status", "in", ["Sent", "sent"]),
            ],
            limit=1,
        )
        # STRONG BLOCK: If the order was already sent successfully, NEVER send it again, even if forced.
        if existing:
            return True
            
        # STRONG BLOCK: Prevent pushing ancient unfulfilled base orders in testing/staging environments.
        # If this is a subscription, and it's trying to push the base delivery (not a renewal),
        # but the Sales Order itself was created more than 60 days ago, log a warning (do NOT hard-block).
        f = sale_order._fields
        is_sub = (
            ('is_subscription' in f and sale_order.is_subscription)
            or ('plan_id' in f and bool(sale_order.plan_id))
            or ('subscription_state' in f and getattr(sale_order, 'subscription_state', '') in ('2_renewal', '3_progress', '4_paused'))
        )
        if is_sub and not is_renewal:
            if sale_order.create_date and (fields.Datetime.now() - sale_order.create_date).days > 60:
                _logger.warning(
                    "[Monta Push] %s: Subscription SO %s is over 60 days old — "
                    "pushing base delivery anyway (remove this warning if not intended).",
                    self.name, sale_order.name,
                )

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
            
            # Post success chatter log
            msg_success = (
                f"✅ Monta Integration: Subscription delivery {self.name} is sent to Monta."
            )
            self.message_post(body=msg_success)
            if sale_order:
                sale_order.message_post(body=msg_success)

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

        # Post API failure chatter log
        msg_err_post = (
            f"❌ Monta Integration: Subscription delivery {self.name} is not sent to Monta "
            f"due to API error: Status {status} (Reason: {json.dumps(body or {})})"
        )
        self.message_post(body=msg_err_post)
        if sale_order:
            sale_order.message_post(body=msg_err_post)

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
        ctx = self.env.context
        for picking in self:
            picking_ctx = picking.with_context(ctx)
            if picking_ctx._is_monta_push_eligible() and not picking.monta_pushed:
                picking_ctx.action_push_to_monta()
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
        return self.with_context(monta_create_delivery=True).action_push_to_monta(sale_order=sale_order)
