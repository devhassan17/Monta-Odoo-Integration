# addons/Monta-Odoo-Integration/hooks.py
# -*- coding: utf-8 -*-
from odoo import SUPERUSER_ID

CRON_XMLID  = "Monta-Odoo-Integration.ir_cron_monta_status_halfhourly"
CRON_NAME   = "Monta: Sync Sales Order Status (half-hourly)"
CRON_MODEL  = "sale.order"
CRON_METHOD = "cron_monta_sync_status"
CRON_CODE   = f"model.{CRON_METHOD}(batch_limit=200)"

def _ensure_cron(env):
    IrCron      = env["ir.cron"].sudo()
    IrModel     = env["ir.model"].sudo()
    IrModelData = env["ir.model.data"].sudo()

    # Already exists?
    try:
        env.ref(CRON_XMLID)
        return
    except ValueError:
        pass

    model_rec = IrModel._get(CRON_MODEL)
    if not model_rec:
        return

    cron = IrCron.create({
        "name": CRON_NAME,
        "model_id": model_rec.id,
        "state": "code",
        "code": CRON_CODE,
        "interval_number": 30,
        "interval_type": "minutes",
        # Odoo 18: DO NOT set 'numbercall' or 'doall'
        "active": True,
        "user_id": SUPERUSER_ID,
    })

    module, name = CRON_XMLID.split(".")
    IrModelData.create({
        "name": name,
        "module": module,
        "model": "ir.cron",
        "res_id": cron.id,
        "noupdate": True,
    })

def _remove_cron(env):
    IrCron = env["ir.cron"].sudo()
    try:
        rec = env.ref(CRON_XMLID)
        if rec:
            rec.unlink()
            return
    except ValueError:
        pass
    crons = IrCron.search([
        ("name", "=", CRON_NAME),
        ("state", "=", "code"),
        ("code", "ilike", CRON_METHOD),
    ])
    if crons:
        crons.unlink()

def post_init_hook(env):
    _ensure_cron(env)

def uninstall_hook(env):
    _remove_cron(env)
