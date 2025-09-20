# -*- coding: utf-8 -*-
"""
Odoo 18 hooks for Monta-Odoo-Integration

- post_init_hook(env): ensure the Monta status sync cron exists
- uninstall_hook(env): remove the cron; optionally drop DB columns
"""

from odoo import SUPERUSER_ID

# ---------------- Cron ----------------
CRON_XMLID   = "Monta-Odoo-Integration.ir_cron_monta_status_halfhourly"
CRON_NAME    = "Monta: Sync Sales Order Status (half-hourly)"
CRON_MODEL   = "sale.order"
CRON_METHOD  = "cron_monta_sync_status"
CRON_CODE    = f"model.{CRON_METHOD}(batch_limit=200)"

# ---------------- Column cleanup toggle ----------------
# Set to True if you want the uninstall hook to DROP the physical columns.
# (Safer to leave False; if you want to drop later, run the SQL after uninstall.)
DROP_COLUMNS = False

# Columns this addon added to sale.order (adjust to match your module)
SALE_ORDER_COLUMNS = [
    "monta_order_id",
    "monta_sync_state",
    "monta_last_push",
    "monta_needs_sync",
    # Keep these only if your addon defined them on sale.order
    "monta_status",
    "monta_status_code",
    "monta_status_source",
    "monta_track_trace",
    "monta_last_sync",
]

# Extra tables your addon may have added columns to:
# Each entry: (table_name, [columns...])
EXTRA_TABLE_COLUMNS = [
    # Example:
    # ("monta_order_status", ["delivery_date", "track_trace_url"]),
]

# ---------------- helpers ----------------
def _ensure_cron(env):
    IrCron      = env["ir.cron"].sudo()
    IrModel     = env["ir.model"].sudo()
    IrModelData = env["ir.model.data"].sudo()

    try:
        env.ref(CRON_XMLID)
        return  # already exists
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
        "numbercall": -1,
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


def _drop_columns_if_exist(env, table, columns):
    cr = env.cr
    for col in columns:
        # IF EXISTS so it’s idempotent
        cr.execute(f'ALTER TABLE "{table}" DROP COLUMN IF EXISTS "{col}" CASCADE')


def _maybe_drop_columns(env):
    if not DROP_COLUMNS:
        return
    # sale.order -> sale_order
    _drop_columns_if_exist(env, "sale_order", SALE_ORDER_COLUMNS)
    for table, cols in EXTRA_TABLE_COLUMNS:
        _drop_columns_if_exist(env, table, cols)

# ---------------- public hooks ----------------
def post_init_hook(env):
    _ensure_cron(env)

def uninstall_hook(env):
    # 1) remove cron
    _remove_cron(env)
    # 2) optionally drop physical columns (do NOT delete ir_model_fields rows)
    _maybe_drop_columns(env)
