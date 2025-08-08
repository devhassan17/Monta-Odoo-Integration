from odoo import models, fields, api

class MontaOrderLog(models.Model):
    _name = 'monta.order.log'
    _description = 'Monta Order Log'

    sale_order_id = fields.Many2one('sale.order', string="Sale Order")
    customer_name = fields.Char(string="Customer Name")
    email = fields.Char(string="Email")
    order_total = fields.Float(string="Order Total")
    created_at = fields.Datetime(string="Logged At", default=fields.Datetime.now)

    @api.model
    def create_log_from_order(self, order):
        self.create({
            'sale_order_id': order.id,
            'customer_name': order.partner_id.name,
            'email': order.partner_id.email,
            'order_total': order.amount_total
        })
