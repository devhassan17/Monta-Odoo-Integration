# -*- coding: utf-8 -*-
from odoo import api, SUPERUSER_ID

CRON_XMLID = "Monta-Odoo-Integration.ir_cron_monta_status_halfhourly"

def post_init_hook(cr, registry):
    """Create/ensure the Monta status sync cron WITHOUT data files."""
    env = api.Environment(cr, SUPERUSER_ID, {})

    # Ensure model/method exist
    model_name = "monta.order.status"
    method = "cron_monta_sync_status"
    if not hasattr(env[model_name], method):
        return

    # If the xmlid already exists, do nothing
    try:
        env.ref(CRON_XMLID)
        return
    except Exception:
        pass

    # Create the cron
    cron = env["ir.cron"].sudo().create({
        "name": "Monta: Sync Order Status (every 30 min)",
        "model_id": env["ir.model"].sudo().search([("model", "=", model_name)], limit=1).id,
        "state": "code",
        "code": f"model.{method}(batch_limit=300)",
        "interval_number": 30,
        "interval_type": "minutes",
        "numbercall": -1,
        "active": True,
    })

    # Bind an external id so env.ref(...) works
    env["ir.model.data"].sudo().create({
        "module": "Monta-Odoo-Integration",
        "name": "ir_cron_monta_status_halfhourly",
        "model": "ir.cron",
        "res_id": cron.id,
        "noupdate": True,
    })


def uninstall_hook(cr, registry):
    """Cleanup to avoid RPC errors on uninstall."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    # Drop the cron if it exists
    try:
        cron = env.ref(CRON_XMLID)
        cron.sudo().unlink()
    except Exception:
        pass
