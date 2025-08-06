from odoo import models, fields

class MontaConfig(models.Model):
    _name = 'monta.config'
    _description = 'Monta API Configuration'

    name = fields.Char(string="Configuration Name", required=True)
    endpoint = fields.Char(string="API Endpoint", required=True)
    username = fields.Char(string="API Username", required=True)
    password = fields.Char(string="API Password", required=True)
    is_active = fields.Boolean(string="Active", default=True)
