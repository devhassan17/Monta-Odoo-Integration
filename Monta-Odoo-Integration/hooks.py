# -*- coding: utf-8 -*-
from odoo import SUPERUSER_ID

# Existing (sales status) cron
CRON_XMLID  = "Monta-Odoo-Integration.ir_cron_monta_status_halfhourly"
CRON_NAME   = "Monta: Sync Sales Order Status (half-hourly)"
CRON_MODEL  = "sale.order"
CRON_METHOD = "cron_monta_sync_status"
CRON_CODE   = f"model.{CRON_METHOD}(batch_limit=200)"
CRON_INTERVAL_MIN = 30  # minutes

# New crons for stock/qty sync (product model)
CRON_QTY_XMLID   = "Monta-Odoo-Integration.ir_cron_monta_qty_sync"
CRON_QTY_NAME    = "Monta: Sync StockAvailable + MinStock (6h)"
CRON_QTY_MODEL   = "product.product"
CRON_QTY_METHOD  = "cron_monta_qty_sync"
CRON_QTY_CODE    = f"model.{CRON_QTY_METHOD}()"
CRON_QTY_HOURS   = 6

CRON_PULL_XMLID  = "Monta-Odoo-Integration.ir_cron_monta_stock_pull"
CRON_PULL_NAME   = "Monta: Pull stock list (/stock) (6h)"
CRON_PULL_MODEL  = "product.product"
CRON_PULL_METHOD = "cron_monta_stock_pull"
CRON_PULL_CODE   = f"model.{CRON_PULL_METHOD}()"
CRON_PULL_HOURS  = 6

def _create_cron_record(env, xmlid, name, model, code, interval_number, interval_type, user_id=SUPERUSER_ID):
    """
    Create a single cron and its ir.model.data mapping if it doesn't exist.
    Idempotent: if env.ref(xmlid) exists we skip creation.
    """
    IrCron = env["ir.cron"].sudo()
    IrModel = env["ir.model"].sudo()
    IrModelData = env["ir.model.data"].sudo()

    try:
        env.ref(xmlid)
        return
    except ValueError:
        pass

    model_rec = IrModel._get(model)
    if not model_rec:
        # Model missing in this database; can't create the cron.
        return

    cron = IrCron.create({
        "name": name,
        "model_id": model_rec.id,
        "state": "code",
        "code": code,
        "interval_number": int(interval_number),
        "interval_type": interval_type,
        "active": True,
        "user_id": user_id,
    })

    module, name_part = xmlid.split(".")
    IrModelData.create({
        "name": name_part,
        "module": module,
        "model": "ir.cron",
        "res_id": cron.id,
        "noupdate": True,
    })


def _ensure_cron(env):
    """
    Ensure all crons we need exist.
    Called from post_init_hook.
    """
    # keep the legacy sales-order cron creation
    _create_cron_record(
        env,
        CRON_XMLID,
        CRON_NAME,
        CRON_MODEL,
        CRON_CODE,
        CRON_INTERVAL_MIN,
        "minutes",
    )

    # create the qty-sync cron (6 hours) which calls MontaQtySync via product.product model
    _create_cron_record(
        env,
        CRON_QTY_XMLID,
        CRON_QTY_NAME,
        CRON_QTY_MODEL,
        CRON_QTY_CODE,
        CRON_QTY_HOURS,
        "hours",
    )

    # create the stock-pull cron (6 hours) which calls MontaStockPull wrapper via product.product model
    _create_cron_record(
        env,
        CRON_PULL_XMLID,
        CRON_PULL_NAME,
        CRON_PULL_MODEL,
        CRON_PULL_CODE,
        CRON_PULL_HOURS,
        "hours",
    )


def _remove_cron(env):
    """
    Remove our crons on uninstall. We try env.ref first (preferred), otherwise search by name/code.
    """
    IrCron = env["ir.cron"].sudo()

    # Try to remove by xmlid references first
    for xmlid in (CRON_XMLID, CRON_QTY_XMLID, CRON_PULL_XMLID):
        try:
            rec = env.ref(xmlid)
            if rec:
                rec.unlink()
        except ValueError:
            pass

    # fallback cleanup by searching similar crons (defensive)
    fallback_filters = [
        ("name", "=", CRON_NAME),
        ("name", "=", CRON_QTY_NAME),
        ("name", "=", CRON_PULL_NAME),
    ]
    for name in (CRON_NAME, CRON_QTY_NAME, CRON_PULL_NAME):
        crons = IrCron.search([
            ("name", "=", name),
            ("state", "=", "code"),
            ("code", "ilike", "cron_monta_"),
        ])
        if crons:
            crons.unlink()


# post-init and uninstall hooks (Odoo expects these names if referenced in manifest)
def post_init_hook(env):
    _ensure_cron(env)


def uninstall_hook(env):
    _remove_cron(env)
