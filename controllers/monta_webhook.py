from odoo import http
from odoo.http import request

class MontaWebhook(http.Controller):
    @http.route('/monta/webhook', type='json', auth='public', csrf=False)
    def webhook_handler(self, **post):
        order_ref = post.get('order_reference')
        status = post.get('status')

        sale_order = request.env['sale.order'].sudo().search([('name', '=', order_ref)], limit=1)
        if sale_order:
            sale_order.write({'delivery_status': status})
        return {'status': 'ok'}
