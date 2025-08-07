from odoo import models, fields, api, _
from odoo.exceptions import UserError
import requests
import logging
from datetime import datetime

_logger = logging.getLogger(__name__)

class MontaIntegration(models.Model):
    _name = 'monta.integration'
    _description = 'Monta Integration Operations'

    def _get_monta_config(self):
        config = self.env['monta.config'].get_config()
        if not config:
            raise UserError(_('Monta configuration not found'))
        return config

    def sync_shipments(self):
        """Sync shipment status from Monta"""
        config = self._get_monta_config()
        try:
            response = requests.get(
                f"{config.endpoint.rstrip('/')}/shipments",
                auth=(config.username, config.password),
                timeout=10
            )
            
            if response.status_code == 200:
                shipments = response.json().get('shipments', [])
                for shipment in shipments:
                    self._process_shipment(shipment)
        except Exception as e:
            _logger.error("Failed to sync shipments: %s", str(e))
            raise

    def _process_shipment(self, shipment_data):
        """Update Odoo based on Monta shipment data"""
        sale_order = self.env['sale.order'].search([
            ('monta_order_id', '=', shipment_data.get('orderId'))
        ], limit=1)
        
        if sale_order:
            picking = sale_order.picking_ids.filtered(
                lambda p: p.state not in ('done', 'cancel')
            )
            if picking:
                picking.write({
                    'carrier_tracking_ref': shipment_data.get('trackingNumber', ''),
                })
                if shipment_data.get('status') == 'shipped':
                    picking.button_validate()