import logging
import json
from odoo import http
from odoo.http import request, Response

_logger = logging.getLogger(__name__)

class MontaWebhookController(http.Controller):
    
    @http.route('/monta/webhook', type='json', auth='public', methods=['POST'], csrf=False)
    def handle_webhook(self):
        """Handle incoming webhooks from Monta"""
        try:
            data = request.jsonrequest
            _logger.info("Received Monta webhook: %s", json.dumps(data, indent=2))
            
            # Verify webhook origin (basic example)
            config = request.env['res.config.settings'].sudo()
            secret_token = config.get_param('monta_integration.webhook_secret')
            
            if secret_token and data.get('secret') != secret_token:
                _logger.warning("Invalid webhook secret")
                return Response(status=403)
            
            # Process based on event type
            event_type = data.get('event')
            if event_type == 'shipment.updated':
                self._process_shipment_update(data.get('data'))
            elif event_type == 'order.updated':
                self._process_order_update(data.get('data'))
            elif event_type == 'inventory.updated':
                self._process_inventory_update(data.get('data'))
            
            return Response(status=200)
        except Exception as e:
            _logger.error("Error processing webhook: %s", str(e))
            return Response(status=500)

    def _process_shipment_update(self, data):
        """Process shipment update from webhook"""
        monta_order_id = data.get('orderId')
        tracking_number = data.get('trackingNumber')
        status = data.get('status')
        
        sale_order = request.env['sale.order'].sudo().search([
            ('monta_order_id', '=', monta_order_id)
        ], limit=1)
        
        if not sale_order:
            _logger.warning("No order found for Monta ID %s", monta_order_id)
            return
        
        # Update all related pickings
        for picking in sale_order.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel')):
            picking.write({
                'carrier_tracking_ref': tracking_number,
            })
            
            if status == 'shipped' and picking.state != 'done':
                picking.action_confirm()
                picking.action_assign()
                picking.button_validate()

        message = f"Shipment update via webhook: {status}\nTracking: {tracking_number}"
        sale_order.message_post(body=message)

    def _process_order_update(self, data):
        """Process order update from webhook"""
        monta_order_id = data.get('id')
        status = data.get('status')
        
        sale_order = request.env['sale.order'].sudo().search([
            ('monta_order_id', '=', monta_order_id)
        ], limit=1)
        
        if not sale_order:
            _logger.warning("No order found for Monta ID %s", monta_order_id)
            return
        
        if status == 'cancelled' and sale_order.state != 'cancel':
            sale_order.action_cancel()
            sale_order.message_post(body="Order cancelled via Monta webhook")

    def _process_inventory_update(self, data):
        """Process inventory update from webhook"""
        sku = data.get('sku')
        quantity = float(data.get('quantity', 0))
        
        product = request.env['product.product'].sudo().search([
            '|',
            ('default_code', '=', sku),
            ('barcode', '=', sku)
        ], limit=1)
        
        if not product:
            _logger.warning("No product found for SKU %s", sku)
            return
        
        # Create inventory adjustment
        inventory = request.env['stock.inventory'].sudo().create({
            'name': f"Monta Webhook Sync - {sku}",
            'product_ids': [(4, product.id)],
            'location_ids': [(4, request.env.ref('stock.stock_location_stock').id)],
            'prefill_counted_quantity': 'zero',
        })
        
        inventory.action_start()
        
        line = request.env['stock.inventory.line'].sudo().search([
            ('inventory_id', '=', inventory.id),
            ('product_id', '=', product.id),
            ('location_id', '=', request.env.ref('stock.stock_location_stock').id)
        ], limit=1)

        if line:
            line.write({'product_qty': quantity})
        else:
            request.env['stock.inventory.line'].sudo().create({
                'inventory_id': inventory.id,
                'product_id': product.id,
                'location_id': request.env.ref('stock.stock_location_stock').id,
                'product_qty': quantity,
            })

        inventory.action_validate()