from . import models

def _initialize_monta_config(cr, registry):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['monta.config']._initialize()