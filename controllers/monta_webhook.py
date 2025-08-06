from odoo import http
from odoo.http import request

class MontaWebhookController(http.Controller):

    @http.route('/monta/webhook', type='json', auth='public', methods=['POST'], csrf=False)
    def monta_webhook(self, **kwargs):
        payload = request.jsonrequest

        monta_order_id = payload.get("order_id")
        tracking_number = payload.get("tracking_number")
        status = payload.get("status")

        order = request.env['sale.order'].sudo().search([('monta_order_id', '=', monta_order_id)], limit=1)
        if order:
            order.sudo().write({
                'tracking_ref': tracking_number,
                'delivery_status': status
            })
            return {"success": True}
        return {"success": False, "error": "Order not found"}
