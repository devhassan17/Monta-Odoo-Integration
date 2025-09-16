from odoo import api, SUPERUSER_ID

STUDIO_FIELD = "x_monta_delivery_date"

def _ensure_studio_delivery_field(env):
    """Create Studio-like field on sale.order if missing (optional)."""
    IMF = env["ir.model.fields"].sudo()
    existing = IMF.search([("model", "=", "sale.order"), ("name", "=", STUDIO_FIELD)], limit=1)
    if existing:
        return
    model_id = env["ir.model"]._get_id("sale.order")
    IMF.create({
        "name": STUDIO_FIELD,
        "field_description": "Monta Delivery Date",
        "model": "sale.order",
        "model_id": model_id,
        "ttype": "date",
        "store": True,
        "state": "manual",   # marks it like a Studio custom field
    })

def post_init_hook(cr, registry):
    env = api.Environment(cr, SUPERUSER_ID, {})
    # optional – only create the Studio field if you want it available
    _ensure_studio_delivery_field(env)

def uninstall_hook(cr, registry):
    env = api.Environment(cr, SUPERUSER_ID, {})
    # Remove the Studio field we created (safe if not found)
    IMF = env["ir.model.fields"].sudo()
    fld = IMF.search([("model", "=", "sale.order"), ("name", "=", STUDIO_FIELD)])
    fld.unlink()
