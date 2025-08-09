
from . import monta_sale_log  # Your main model file

def _initialize_monta_credentials(cr, registry):
    """Initialize Monta credentials on module install"""
    from odoo import api, SUPERUSER_ID
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['sale.order']._init_monta_credentials()