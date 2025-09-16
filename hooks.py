# odoo/addons/Monta-Odoo-Integration/hooks.py
from odoo import SUPERUSER_ID

MODULE = "Monta-Odoo-Integration"
CRON_XMLID = f"{MODULE}.ir_cron_monta_status_hourly"

def _ensure_cron(env):
    Cron = env["ir.cron"].sudo()
    IMD = env["ir.model.data"].sudo()
    if env.ref(CRON_XMLID, raise_if_not_found=False):
        return
    cron = Cron.create({
        "name": "Monta: Sync order status hourly",
        "model_id": env.ref(f"{MODULE}.model_monta_order_status").id,
        "state": "code",
        "code": 'env["monta.order.status"].cron_monta_sync_status(batch_limit=200)',
        "interval_type": "hours",
        "interval_number": 1,
        "nextcall": env["ir.fields.datetime"].now(),
        "active": True,
        "user_id": env.ref("base.user_root").id or SUPERUSER_ID,
    })
    IMD.create({
        "module": MODULE,
        "name": "ir_cron_monta_status_hourly",
        "model": "ir.cron",
        "res_id": cron.id,
        "noupdate": True,
    })

def post_init_hook(env):
    _ensure_cron(env)
    # first sync right after install
    env["monta.order.status"].sudo().cron_monta_sync_status(batch_limit=500)

def uninstall_hook(env):
    # remove our cron and clean the status table (keep sale.order intact)
    imd = env["ir.model.data"].sudo().search([
        ("module", "=", MODULE), ("name", "=", "ir_cron_monta_status_hourly"),
        ("model", "=", "ir.cron"),
    ], limit=1)
    if imd:
        imd.sudo().res_id and env["ir.cron"].sudo().browse(imd.res_id).unlink()
        imd.unlink()
    env["monta.order.status"].sudo().search([]).unlink()
