from odoo import models, fields

class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    monta_username = fields.Char(
        string="Monta Username",
        config_parameter='monta_integration.username'
    )
    monta_password = fields.Char(
        string="Monta Password",
        config_parameter='monta_integration.password'
    )
    monta_endpoint = fields.Char(
        string="Monta API Endpoint",
        config_parameter='monta_integration.endpoint',
        default="https://api-v6.monta.nl/"
    )
    monta_webhook_secret = fields.Char(
    string="Webhook Secret",
    config_parameter='monta_integration.webhook_secret',
    help="Secret token for verifying webhook requests"
)

    def test_monta_connection(self):
        self.ensure_one()
        try:
            response = self._make_monta_request('GET', 'ping')
            if response.status_code == 200:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': 'Success',
                        'message': 'Connection to Monta API successful!',
                        'sticky': False,
                        'type': 'success',
                    }
                }
            else:
                raise Exception(f"HTTP {response.status_code}: {response.text}")
        except Exception as e:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Connection Failed',
                    'message': f'Could not connect to Monta: {str(e)}',
                    'sticky': False,
                    'type': 'danger',
                }
            }

    def _make_monta_request(self, method, endpoint, data=None):
        """Helper method to make authenticated requests to Monta API"""
        username = self.monta_username or self.env['ir.config_parameter'].sudo().get_param('monta_integration.username')
        password = self.monta_password or self.env['ir.config_parameter'].sudo().get_param('monta_integration.password')
        base_url = self.monta_endpoint or self.env['ir.config_parameter'].sudo().get_param('monta_integration.endpoint', 'https://api-v6.monta.nl/')
        
        url = f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        
        return requests.request(
            method,
            url,
            auth=(username, password),
            headers=headers,
            json=data,
            timeout=10
        )
    