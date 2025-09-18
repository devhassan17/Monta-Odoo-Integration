# -*- coding: utf-8 -*-
"""
Odoo 18 hooks for Monta-Odoo-Integration

- post_init_hook(env): ensure the Monta status sync cron exists
- uninstall_hook(env): remove the cron cleanly on uninstall
"""

from odoo import SUPERUSER_ID

CRON_XMLID = "Monta-Odoo-Integration.ir_cron_monta_status_halfhourly"
CRON_NAME = "Monta: Sync Sales Order Status (half-hourly)"
CRON_MODEL = "sale.order"
CRON_METHOD = "cron_monta_sync_status"  # should exist on sale.order


def _ensure_cron(env):
    """Create the scheduled action if missing. Idempotent by XMLID."""
    su = env.sudo()
    IrCron = su["ir.cron"]
    IrModel = su["ir.model"]
    IrModelData = su["ir.model.data"]

    # If the XMLID already exists, skip creation
    try:
        su.ref(CRON_XMLID)
        return
    except ValueError:
        # Not found — create it
        pass

    # Resolve model_id for sale.order
    model_rec = IrModel._get(CRON_MODEL)
    if not model_rec:
        # Model unavailable (shouldn't happen in normal installs)
        return

    cron = IrCron.create({
        "name": CRON_NAME,
        "model_id": model_rec.id,
        "state": "code",
        "code": f"model.{CRON_METHOD}(batch_limit=200)",
        "interval_number": 30,
        "interval_type": "minutes",
        "numbercall": -1,
        "active": True,
        "user_id": SUPERUSER_ID,  # run as admin to avoid ACL issues
    })

    # Bind XMLID so future upgrades detect it
    module, name = CRON_XMLID.split(".")
    IrModelData.create({
        "name": name,
        "module": module,
        "model": "ir.cron",
        "res_id": cron.id,
        "noupdate": True,
    })


def _remove_cron(env):
    """Remove the scheduled action on uninstall (by XMLID, then by heuristic)."""
    su = env.sudo()
    IrCron = su["ir.cron"]

    # Try unlinking by XMLID
    try:
        rec = su.ref(CRON_XMLID)
        if rec:
            rec.unlink()
            return
    except ValueError:
        # XMLID not present; keep going
        pass

    # Fallback: search by name + code pattern
    crons = IrCron.search([
        ("name", "=", CRON_NAME),
        ("state", "=", "code"),
        ("code", "ilike", CRON_METHOD),
    ])
    if crons:
        crons.unlink()


def post_init_hook(env):
    """Odoo 17/18 signature: receives `env`."""
    _ensure_cron(env)


def uninstall_hook(env):
    """Odoo 17/18 signature: receives `env`."""
    _remove_cron(env)
