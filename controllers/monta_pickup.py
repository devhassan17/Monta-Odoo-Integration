# -*- coding: utf-8 -*-
import json
import logging
import re
from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

class MontaPickupController(http.Controller):

    @http.route('/shop/monta/get_pickup_points', type='json', auth='public', website=True)
    def get_pickup_points(self, zip_code=None, country_code='NL', street=None, house_number=None, city=None, postal_code=None, **kwargs):
        """Fetch pickup points from Monta WMS REST API v6 using /shippingoptions."""
        order = request.website.sale_get_order()
        if not order:
            return {'status': 'error', 'message': 'No active sales order.'}

        # Check if the order has monta config
        cfg = order._monta_config() if hasattr(order, '_monta_config') else None
        if not cfg:
            return {'status': 'error', 'message': 'Monta integration is not configured or disabled.'}

        if not cfg.origin:
            _logger.warning("Monta Origin is not set in the configuration.")
            return {'status': 'error', 'message': 'Monta Origin is not configured. Please set the Origin in Monta Configuration.'}

        # Fallback to order address if not explicitly passed
        partner = order.partner_shipping_id or order.partner_id
        zip_val = (zip_code or postal_code or partner.zip or "").strip().replace(" ", "").upper()
        country_val = country_code or (partner.country_id.code if partner.country_id else 'NL')
        street_val = (street or partner.street or "").strip()
        city_val = (city or partner.city or "").strip()
        house_num_val = (house_number or partner.street2 or "").strip()

        # Compile products for accurate size/weight constraints in Monta
        products = []
        for line in order.order_line:
            if line.product_id and not line.is_delivery and not getattr(line, 'is_cs_packaging', False) and not getattr(line, 'is_cs_box', False):
                products.append({
                    "Sku": line.product_id.default_code or line.product_id.barcode or "",
                    "Quantity": int(line.product_uom_qty)
                })

        lang = request.env.context.get('lang') or order.partner_id.lang or 'en_US'
        lang_code = lang.replace('_', '-') if lang else 'nl-NL'

        addr_dict = {
            "PostalCode": zip_val,
            "CountryCode": country_val,
        }
        if street_val:
            addr_dict["Street"] = street_val
        if house_num_val:
            addr_dict["HouseNumber"] = house_num_val
        if city_val:
            addr_dict["City"] = city_val

        payload = {
            "Origin": cfg.origin.strip(),
            "Currency": order.currency_id.name or "EUR",
            "Language": lang_code,
            "Address": addr_dict,
            "MaxNumberOfPickupPoints": 10,
            "OnlyPickupPoints": True
        }
        if products:
            payload["Products"] = products

        try:
            status, body = order._monta_request("POST", "/shippingoptions", payload)
            if status != 200:
                _logger.warning("Monta pickup options API failed: %s %s", status, body)
                return {'status': 'error', 'message': 'Failed to fetch pickup points from Monta.'}

            # Filter and parse pickup points
            timeframes = body.get('Timeframes') or []
            pickup_points = []
            for tf in timeframes:
                if tf.get('IsPickupPoint') and tf.get('PickupPointDetails'):
                    details = tf['PickupPointDetails']
                    # Extract the shipping options for this point
                    shipper_options = tf.get('ShippingOptions') or []
                    if not shipper_options:
                        continue

                    best_option = shipper_options[0]
                    shipper_codes = best_option.get('ShipperCodes') or []
                    shipper_code = shipper_codes[0] if shipper_codes else 'PostNL'
                    
                    # Shipping option code (e.g. pakjegemak or pickuppoint)
                    option_code = best_option.get('Code') or 'pakjegemak'
                    
                    price = best_option.get('SellPrice') or 0.0
                    currency = best_option.get('SellPriceCurrency') or 'EUR'

                    pickup_points.append({
                        'code': details.get('Code') or '',
                        'company': details.get('Company') or '',
                        'street': details.get('Street') or '',
                        'house_number': details.get('HouseNumber') or '',
                        'postal_code': details.get('PostalCode') or '',
                        'city': details.get('City') or '',
                        'country_code': details.get('CountryCode') or 'NL',
                        'distance': details.get('DistanceMeters') or 0.0,
                        'phone': details.get('Phone') or '',
                        'image_url': details.get('ImageUrl') or '',
                        'shipper_code': shipper_code,
                        'option_code': option_code,
                        'price': price,
                        'currency': currency,
                        'opening_times': details.get('OpeningTimes') or []
                    })

            # Sort by distance
            pickup_points.sort(key=lambda x: x['distance'])

            return {
                'status': 'success',
                'pickup_points': pickup_points
            }

        except Exception as e:
            _logger.exception("Error querying Monta pickup points: %s", str(e))
            return {'status': 'error', 'message': 'An unexpected error occurred.'}

    @http.route('/shop/monta/select_pickup_point', type='json', auth='public', website=True)
    def select_pickup_point(self, name=None, street=None, house_number=None, zip=None, city=None, country_code=None, shipper_code=None, option_code=None, point_code=None, price=0.0, **kwargs):
        """Update checkout order with selected pickup point address and monta fields."""
        order = request.website.sale_get_order()
        if not order:
            return {'status': 'error', 'message': 'No active sales order.'}

        try:
            # Revert/Clear if shipper_code is missing
            if not shipper_code:
                order.write({
                    'partner_shipping_id': order.partner_id.id,
                    'monta_shipper_code': False,
                    'monta_shipper_options': False,
                    'monta_delivery_type': 'standard',
                })
                # Re-evaluate delivery carrier
                carrier = order.carrier_id or request.env['delivery.carrier'].sudo().search([], limit=1)
                if carrier:
                    if hasattr(order, '_set_delivery_line'):
                        order._set_delivery_line(carrier, 0.0)
                    elif hasattr(order, 'set_delivery_line'):
                        order.set_delivery_line(carrier, 0.0)
                return {'status': 'success', 'cleared': True}

            # 1. Find or create delivery partner
            partner_vals = {
                'parent_id': order.partner_id.id,
                'type': 'delivery',
                'name': order.partner_id.name or order.partner_invoice_id.name or "Consumer",
                'company_name': name,
                'street': street,
                'street2': house_number or '',
                'zip': zip,
                'city': city,
                'country_id': request.env['res.country'].sudo().search([('code', '=', country_code)], limit=1).id,
                'email': order.partner_id.email or order.partner_invoice_id.email,
                'phone': order.partner_id.phone or order.partner_invoice_id.phone,
            }
            
            domain = [
                ('parent_id', '=', order.partner_id.id),
                ('type', '=', 'delivery'),
                ('company_name', '=', name),
                ('street', '=', street),
                ('zip', '=', zip),
            ]
            shipping_partner = request.env['res.partner'].sudo().search(domain, limit=1)
            if not shipping_partner:
                shipping_partner = request.env['res.partner'].sudo().create(partner_vals)
            else:
                shipping_partner.sudo().write(partner_vals)

            # 2. Update order with shipping partner and shipper options
            shipper_options = [{
                "ShipperCode": shipper_code,
                "Code": option_code,
                "Value": point_code
            }]

            order.write({
                'partner_shipping_id': shipping_partner.id,
                'monta_shipper_code': shipper_code,
                'monta_shipper_options': json.dumps(shipper_options),
                'monta_delivery_type': 'pickup',
            })

            # 3. Update delivery carrier and price
            carrier = request.env['delivery.carrier'].sudo().search([
                '|', 
                ('name', 'ilike', shipper_code),
                ('name', 'ilike', 'Monta Pickup')
            ], limit=1)
            if not carrier:
                carrier = request.env['delivery.carrier'].sudo().search([('name', 'ilike', 'pickup')], limit=1)
            if not carrier:
                carrier = order.carrier_id or request.env['delivery.carrier'].sudo().search([], limit=1)

            if carrier:
                order.write({'carrier_id': carrier.id})
                if hasattr(order, '_set_delivery_line'):
                    order._set_delivery_line(carrier, price)
                elif hasattr(order, 'set_delivery_line'):
                    order.set_delivery_line(carrier, price)
                else:
                    delivery_line = order.order_line.filtered(lambda l: l.is_delivery)
                    if delivery_line:
                        delivery_line[0].write({
                            'product_id': carrier.product_id.id,
                            'price_unit': price,
                            'name': carrier.name,
                        })
                    else:
                        order.env['sale.order.line'].sudo().create({
                            'order_id': order.id,
                            'product_id': carrier.product_id.id,
                            'name': carrier.name,
                            'product_uom_qty': 1.0,
                            'price_unit': price,
                            'is_delivery': True,
                        })

            delivery_display = "%s\n%s %s\n%s %s" % (name, street, house_number or '', zip, city)

            return {
                'status': 'success',
                'delivery_partner_id': shipping_partner.id,
                'delivery_display': delivery_display
            }

        except Exception as e:
            _logger.exception("Error selecting Monta pickup point: %s", str(e))
            return {'status': 'error', 'message': str(e)}

    @http.route('/shop/monta/select_delivery_type', type='json', auth='public', website=True)
    def select_delivery_type(self, delivery_type='standard', **kwargs):
        """Update checkout order delivery speed: standard, next_day, or two_day."""
        order = request.website.sale_get_order()
        if not order:
            return {'status': 'error', 'message': 'No active sales order.'}

        try:
            from datetime import timedelta
            from odoo import fields

            now = fields.Datetime.now()
            vals = {
                'monta_delivery_type': delivery_type,
            }

            # Clear pickup options if switching away from pickup
            if delivery_type != 'pickup':
                vals.update({
                    'monta_shipper_code': False,
                    'monta_shipper_options': False,
                    'partner_shipping_id': order.partner_id.id,
                })

            if delivery_type == 'next_day':
                vals['monta_requested_delivery_date'] = now + timedelta(days=1)
            elif delivery_type == 'two_day':
                vals['monta_requested_delivery_date'] = now + timedelta(days=2)
            else:
                vals['monta_requested_delivery_date'] = False

            order.write(vals)

            return {
                'status': 'success',
                'delivery_type': delivery_type,
                'requested_date': fields.Datetime.to_string(vals.get('monta_requested_delivery_date')) if vals.get('monta_requested_delivery_date') else None,
            }

        except Exception as e:
            _logger.exception("Error updating delivery type: %s", str(e))
            return {'status': 'error', 'message': str(e)}
