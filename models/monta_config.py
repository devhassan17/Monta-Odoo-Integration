from odoo import models, fields

class MontaConfig(models.Model):
    _name = 'monta.config'
    _description = 'Monta Configuration'

    name = fields.Char("Configuration Name", required=True)
    endpoint = fields.Char("API Endpoint", required=True)
    username = fields.Char("API Username", required=True)
    password = fields.Char("API Password", required=True)
    active = fields.Boolean(default=True)
