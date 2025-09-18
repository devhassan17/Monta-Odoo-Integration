# -*- coding: utf-8 -*-
"""
Odoo 18 hooks for Monta-Odoo-Integration

- post_init_hook(env): ensure the Monta status sync cron exists
- uninstall_hook(env): remove the cron cleanly on uninstall

Safe-by-default:
- Uses env (Odoo 17/18 convention)
- Idempotent creation (skips if the xmlid already exists)
- Defensive unlink on uninstall (by xmlid, and fallback search)
"""

from odoo import api, SUPERUSER_ID

# Keep a single source of truth for the cron xmlid
CRON_XMLID = "Monta-Odoo-Integration.ir_cron_monta_status_halfhourly"

# Human-readable cron name (visible in Settings > Technical > Scheduled Actions)
CRON_NAME = "Monta: Sync Sales Order Status (half-hourly)"

# Target model and method (must exist on sale.order)
CRON_MODEL = "sale.order"
CRON_METHOD = "cron_monta_sync_status"  # signature should accept **kwargs, e.g. (batch_limit=200)


def _ensure_cron(env):
    """
    Create the scheduled action if missing. If it exists, leave it as-is.
    The cron calls `sale.order.cron_monta_sync_status(batch_limit=200)` every 30 minutes.
    """
    IrModel = env["ir.model"].sudo()
    IrCron = env["ir.cron"].sudo()
    IrModelData = env["ir.model.data"].sudo()

    # If the XMLID already exists, assume it's configured and bail out
    try:
        IrModelData.get_object_reference(CRON_XMLID.split(".")[0], CRON_XMLID.split(".")[1])
        return
    except Exception:
        # Not found — create it
        pass

    # Resolve model_id for sale.order
    model_id = IrModel._get_id(CRON_MODEL)
    if not model_id:
        # If sale.order isn't available, don't crash the install
        return

    # Create the cron
    cron = IrCron.create({
        "name": CRON_NAME,
        "model_id": model_id,
        "state": "code",
        "code": "model.%s(batch_limit=200)" % CRON_METHOD,
        "interval_number": 30,
        "interval_type": "minutes",
        "numbercall": -1,
        "active": True,
        # Optional: run as superuser to avoid ACL surprises
        "user_id": SUPERUSER_ID,
    })

    # Bind the XMLID so future installs/updates detect it
    IrModelData.create({
        "name": CRON_XMLID.split(".")[1],
        "module": CRON_XMLID.split(".")[0],
        "model": "ir.cron",
        "res_id": cron.id,
        "noupdate": True,
    })


def _remove_cron(env):
    """
    Remove the scheduled action on uninstall:
    - First try by XMLID
    - Fallback: search by name and code signature to be thorough
    """
    IrCron = env["ir.cron"].sudo()
    IrModelData = env["ir.model.data"].sudo()

    # Try unlinking by xmlid
    try:
        module, name = CRON_XMLID.split(".")
        rec = IrModelData.get_object(module, name)
        if rec:
            rec.sudo().unlink()
            return
    except Exception:
        # If xmlid doesn’t exist (e.g., manually deleted), fallback to search
        pass

    # Fallback search: match by name + code
    domain = [
        ("name", "=", CRON_NAME),
        ("state", "=", "code"),
        ("code", "ilike", CRON_METHOD),
    ]
    crons = IrCron.search(domain)
    if crons:
        crons.unlink()


def post_init_hook(env):
    """
    Odoo 17/18 signature: receives `env`.
    Ensure the Monta sync cron is present.
    """
    # Use a superuser env to avoid ACL issues during installation
    with api.Environment.manage():
        su_env = api.Environment(env.cr, SUPERUSER_ID, env.context)
        _ensure_cron(su_env)


def uninstall_hook(env):
    """
    Odoo 17/18 signature: receives `env`.
    Clean up the cron to avoid dangling scheduled actions.
    """
    with api.Environment.manage():
        su_env = api.Environment(env.cr, SUPERUSER_ID, env.context)
        _remove_cron(su_env)
