from odoo import models, fields

class MontaConfig(models.Model):
    _name = 'monta.config'
    _description = 'Monta API Configuration'

    name = fields.Char(string='Configuration Name')
    api_key = fields.Char(string='API Key', required=True)
    endpoint = fields.Char(string='API Endpoint', default='https://api-v6.monta.nl')
    active = fields.Boolean(default=True)
