# -*- coding: utf-8 -*-
CRON_XMLID = "Monta-Odoo-Integration.ir_cron_monta_status_hourly"

def _ensure_hourly_cron(env):
    cron = env.ref(CRON_XMLID, raise_if_not_found=False)
    if cron:
        return cron
    cron = env["ir.cron"].sudo().create({
        "name": "Monta: Sync Order Status (hourly, no SO write)",
        "model_id": env["ir.model"]._get_id("monta.order.status"),
        "state": "code",
        "code": "env['monta.order.status'].cron_monta_sync_status(batch_limit=50)",
        "interval_number": 1,
        "interval_type": "hours",
        "numbercall": -1,
        "active": True,
    })
    env["ir.model.data"].sudo().create({
        "name": "ir_cron_monta_status_hourly",
        "module": "Monta-Odoo-Integration",
        "res_id": cron.id,
        "model": "ir.cron",
        "noupdate": True,
    })
    return cron

def post_init_hook(env):
    _ensure_hourly_cron(env)
    env["monta.order.status"].sudo().cron_monta_sync_status(batch_limit=25)

def uninstall_hook(env):
    cron = env.ref(CRON_XMLID, raise_if_not_found=False)
    if cron:
        cron.sudo().unlink()
