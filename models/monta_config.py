from odoo import models, fields, api, _
from odoo.exceptions import ValidationError
import requests
import logging

_logger = logging.getLogger(__name__)

class MontaConfig(models.Model):
    _name = 'monta.config'
    _description = 'Monta Configuration Settings'
    
    name = fields.Char(
        string='Configuration Name',
        default='Monta Settings',
        required=True
    )
    endpoint = fields.Char(
        string='API Endpoint',
        default='https://api-v6.monta.nl/',
        required=True
    )
    username = fields.Char(
        string='API Username',
        required=True
    )
    password = fields.Char(
        string='API Password',
        required=True
    )
    webhook_secret = fields.Char(
        string='Webhook Secret'
    )
    active = fields.Boolean(
        string='Active',
        default=True
    )

    @api.model
    def get_config(self):
        """Get the active Monta configuration"""
        return self.search([('active', '=', True)], limit=1)

    def test_connection(self):
        """Test connection to Monta API"""
        self.ensure_one()
        try:
            response = requests.get(
                f"{self.endpoint.rstrip('/')}/ping",
                auth=(self.username, self.password),
                timeout=10
            )
            if response.status_code == 200:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': _('Success'),
                        'message': _('Connection to Monta API successful!'),
                        'type': 'success',
                        'sticky': False,
                    }
                }
            raise ValidationError(_('Connection failed: %s') % response.text)
        except Exception as e:
            raise ValidationError(_('Connection error: %s') % str(e))

    @api.model
    def _create_default_config(self):
        """Create default config on module install"""
        if not self.search_count([]):
            self.create({
                'name': 'Default Monta Configuration',
                'endpoint': 'https://api-v6.monta.nl/',
                'active': True
            })