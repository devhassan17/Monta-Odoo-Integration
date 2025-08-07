import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

class MontaIntegration(models.Model):
    _name = 'monta.integration'
    _description = 'Monta Integration'

    def _get_monta_config(self):
        return self.env['res.config.settings'].sudo()

    def sync_shipments(self):
        """Sync shipment data from Monta to Odoo"""
        config = self._get_monta_config()
        try:
            response = config._make_monta_request('GET', 'shipments')
            if response.status_code != 200:
                _logger.error("Failed to fetch shipments: %s - %s", response.status_code, response.text)
                return

            shipments = response.json().get('data', [])
            for shipment in shipments:
                self._process_monta_shipment(shipment)

        except Exception as e:
            _logger.error("Error syncing shipments from Monta: %s", str(e))

    def _process_monta_shipment(self, shipment_data):
        """Process a single shipment from Monta"""
        monta_order_id = shipment_data.get('orderId')
        tracking_number = shipment_data.get('trackingNumber')
        status = shipment_data.get('status')
        date_shipped = shipment_data.get('dateShipped')

        if not monta_order_id:
            return

        sale_order = self.env['sale.order'].search([
            ('monta_order_id', '=', monta_order_id)
        ], limit=1)

        if not sale_order:
            _logger.warning("No Odoo order found for Monta order ID %s", monta_order_id)
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

        message = _("Shipment updated via Monta: %s<br>Tracking: %s<br>Status: %s") % (
            date_shipped,
            tracking_number,
            status
        )
        sale_order.message_post(body=message)

    def sync_inventory(self):
        """Sync inventory levels from Monta to Odoo"""
        config = self._get_monta_config()
        try:
            response = config._make_monta_request('GET', 'inventory')
            if response.status_code != 200:
                _logger.error("Failed to fetch inventory: %s - %s", response.status_code, response.text)
                return

            inventory_data = response.json().get('data', [])
            for item in inventory_data:
                self._process_inventory_item(item)

        except Exception as e:
            _logger.error("Error syncing inventory from Monta: %s", str(e))

    def _process_inventory_item(self, item_data):
        """Process a single inventory item from Monta"""
        sku = item_data.get('sku')
        quantity = float(item_data.get('quantity', 0))
        
        product = self.env['product.product'].search([
            '|',
            ('default_code', '=', sku),
            ('barcode', '=', sku)
        ], limit=1)

        if not product:
            _logger.warning("No product found for SKU %s", sku)
            return

        # Create inventory adjustment
        inventory = self.env['stock.inventory'].create({
            'name': f"Monta Sync - {sku}",
            'product_ids': [(4, product.id)],
            'location_ids': [(4, self.env.ref('stock.stock_location_stock').id)],
            'prefill_counted_quantity': 'zero',
        })
        
        inventory.action_start()
        
        line = self.env['stock.inventory.line'].search([
            ('inventory_id', '=', inventory.id),
            ('product_id', '=', product.id),
            ('location_id', '=', self.env.ref('stock.stock_location_stock').id)
        ], limit=1)

        if line:
            line.write({'product_qty': quantity})
        else:
            self.env['stock.inventory.line'].create({
                'inventory_id': inventory.id,
                'product_id': product.id,
                'location_id': self.env.ref('stock.stock_location_stock').id,
                'product_qty': quantity,
            })

        inventory.action_validate()
        _logger.info("Updated inventory for product %s to %s", sku, quantity)

    def sync_returns(self):
        """Sync return data from Monta to Odoo"""
        config = self._get_monta_config()
        try:
            response = config._make_monta_request('GET', 'returns')
            if response.status_code != 200:
                _logger.error("Failed to fetch returns: %s - %s", response.status_code, response.text)
                return

            returns = response.json().get('data', [])
            for return_data in returns:
                self._process_monta_return(return_data)

        except Exception as e:
            _logger.error("Error syncing returns from Monta: %s", str(e))

    def _process_monta_return(self, return_data):
        """Process a single return from Monta"""
        monta_order_id = return_data.get('orderId')
        return_reason = return_data.get('reason')
        return_date = return_data.get('returnDate')
        items = return_data.get('items', [])

        if not monta_order_id:
            return

        sale_order = self.env['sale.order'].search([
            ('monta_order_id', '=', monta_order_id)
        ], limit=1)

        if not sale_order:
            _logger.warning("No Odoo order found for Monta order ID %s", monta_order_id)
            return

        # Create return picking
        picking_type = self.env['stock.picking.type'].search([
            ('code', '=', 'incoming'),
            ('warehouse_id', '=', sale_order.warehouse_id.id)
        ], limit=1)

        if not picking_type:
            picking_type = self.env['stock.picking.type'].search([
                ('code', '=', 'incoming')
            ], limit=1)

        return_picking = self.env['stock.picking'].create({
            'picking_type_id': picking_type.id,
            'location_id': picking_type.default_location_dest_id.id,
            'location_dest_id': sale_order.warehouse_id.lot_stock_id.id,
            'origin': _("Return for %s") % sale_order.name,
            'partner_id': sale_order.partner_id.id,
        })

        for item in items:
            product = self.env['product.product'].search([
                '|',
                ('default_code', '=', item.get('sku')),
                ('barcode', '=', item.get('sku'))
            ], limit=1)

            if product:
                self.env['stock.move'].create({
                    'name': product.name,
                    'product_id': product.id,
                    'product_uom_qty': item.get('quantity', 0),
                    'product_uom': product.uom_id.id,
                    'picking_id': return_picking.id,
                    'location_id': picking_type.default_location_dest_id.id,
                    'location_dest_id': sale_order.warehouse_id.lot_stock_id.id,
                })

        message = _("Return processed via Monta: %s<br>Reason: %s") % (
            return_date,
            return_reason
        )
        sale_order.message_post(body=message)
        return_picking.action_confirm()
        return_picking.action_assign()