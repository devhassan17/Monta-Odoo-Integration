# -*- coding: utf-8 -*-
"""
Programmatic cron (no XML) for Monta status sync.

Creates/updates a single ir.cron that calls:
    model.cron_monta_sync_status()

Logging is minimal here; the heavy per-order logs live in models/monta_status_sync.py
"""

from datetime import datetime, timedelta
from odoo import api, SUPERUSER_ID


_CRON_XMLID = "monta_order_status_sync.cron_monta_status_sync"  # internal name; not from XML
_CRON_NAME = "Monta: Sync Order Status (hourly)"
_CRON_MODEL = "sale.order"
_CRON_METHOD = "cron_monta_sync_status"
_CRON_INTERVAL_NUMBER = 1
_CRON_INTERVAL_TYPE = "hours"


def _ensure_cron(env):
    """Create or update the cron safely."""
    IrModel = env["ir.model"].sudo()
    IrCron = env["ir.cron"].sudo()
    IrModelData = env["ir.model.data"].sudo()

    # Resolve model id
    sale_model = IrModel.search([("model", "=", _CRON_MODEL)], limit=1)
    if not sale_model:
        return

    # Try to find existing via ir.model.data xmlid-like record
    rec = IrModelData.search(
        [("module", "=", "monta_order_status_sync"), ("name", "=", "cron_monta_status_sync")],
        limit=1,
    )
    cron = IrCron.browse(rec.res_id).exists() if rec else IrCron.search(
        [("name", "=", _CRON_NAME), ("model_id", "=", sale_model.id)], limit=1
    )

    vals = {
        "name": _CRON_NAME,
        "model_id": sale_model.id,
        "state": "code",
        "code": "model.%s()" % _CRON_METHOD,
        "interval_number": _CRON_INTERVAL_NUMBER,
        "interval_type": _CRON_INTERVAL_TYPE,
        "numbercall": -1,
        "doall": False,
        "active": True,
        # start a few minutes from now to avoid top-of-hour thundering herd
        "nextcall": (datetime.utcnow() + timedelta(minutes=7)).strftime("%Y-%m-%d %H:%M:%S"),
    }

    if cron:
        # Update existing cron in place (keep id stable)
        cron.write(vals)
    else:
        cron = IrCron.create(vals)
        # create a model data row so future upgrades find it again
        IrModelData.create({
            "name": "cron_monta_status_sync",
            "module": "monta_order_status_sync",
            "model": "ir.cron",
            "res_id": cron.id,
            "noupdate": True,
        })


def post_init_hook(cr, registry):
    """Called right after module install."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    _ensure_cron(env)


def uninstall_hook(cr, registry):
    """Clean up the cron on uninstall (optional but tidy)."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    IrModel = env["ir.model"].sudo()
    IrCron = env["ir.cron"].sudo()
    IrModelData = env["ir.model.data"].sudo()

    sale_model = IrModel.search([("model", "=", _CRON_MODEL)], limit=1)
    if not sale_model:
        return

    # Find via our model data entry
    rec = IrModelData.search(
        [("module", "=", "monta_order_status_sync"), ("name", "=", "cron_monta_status_sync")],
        limit=1,
    )
    if rec:
        IrCron.browse(rec.res_id).unlink()
        rec.unlink()
    else:
        # Fallback cleanup by name/model
        crons = IrCron.search([("name", "=", _CRON_NAME), ("model_id", "=", sale_model.id)])
        crons.unlink()
