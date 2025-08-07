from odoo import models, fields, api, _
from odoo.exceptions import ValidationError
import requests
import logging

_logger = logging.getLogger(__name__)

class MontaConfig(models.Model):
    _name = 'monta.config'
    _description = 'Monta Configuration'
    _order = 'id desc'  # Show newest first
    
    # Fields
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
        default='testmoyeeMONTADDOOCONNECTOR',
        required=True
    )
    password = fields.Char(
        string='API Password',
        default='91C4%@$=VL42',
        required=True,
        password=True  # This makes the field masked in UI
    )
    webhook_secret = fields.Char(
        string='Webhook Secret',
        password=True
    )
    active = fields.Boolean(
        string='Active',
        default=True
    )

    # Default view definition in Python
    @api.model
    def _get_default_views(self):
        """Generate default views programmatically"""
        return [
            (0, 0, {
                'name': 'monta.config.tree',
                'model': self._name,
                'arch': """
                    <tree>
                        <field name="name"/>
                        <field name="endpoint"/>
                        <field name="active"/>
                    </tree>
                """
            }),
            (0, 0, {
                'name': 'monta.config.form',
                'model': self._name,
                'arch': """
                    <form>
                        <sheet>
                            <group>
                                <field name="name"/>
                                <field name="endpoint"/>
                                <field name="username"/>
                                <field name="password" password="True"/>
                                <field name="webhook_secret" password="True"/>
                                <field name="active"/>
                            </group>
                            <footer>
                                <button name="test_connection" string="Test Connection" 
                                        type="object" class="btn-primary"/>
                            </footer>
                        </sheet>
                    </form>
                """
            })
        ]

    # Create default views when model is initialized
    @api.model
    def _init_views(self):
        view_obj = self.env['ir.ui.view']
        for view in self._get_default_views():
            view_obj.create(view)

    # Test connection method
    def test_connection(self):
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

    # Create default record
    @api.model
    def _create_default_config(self):
        if not self.search_count([]):
            self.create({
                'name': _('Default Monta Config'),
                'username': 'testmoyeeMONTAODOOCONNECTOR',
                'password': '91C4%@$=VL42',
                'active': True
            })

    # Initialize module
    @api.model
    def _initialize(self):
        self._init_views()
        self._create_default_config()