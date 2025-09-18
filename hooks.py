# -*- coding: utf-8 -*-
"""
Odoo 18 hooks for Monta-Odoo-Integration

- post_init_hook(env): ensure the Monta status sync cron exists
- uninstall_hook(env): remove the cron + drop custom fields/columns added by this module
"""

from odoo import SUPERUSER_ID

# ---------- Scheduled Action (Cron) ----------
CRON_XMLID   = "Monta-Odoo-Integration.ir_cron_monta_status_halfhourly"
CRON_NAME    = "Monta: Sync Sales Order Status (half-hourly)"
CRON_MODEL   = "sale.order"
CRON_METHOD  = "cron_monta_sync_status"  # must exist on sale.order
CRON_CODE    = f"model.{CRON_METHOD}(batch_limit=200)"


def _ensure_cron(env):
    """Create the scheduled action if missing. Idempotent by XMLID."""
    IrCron      = env["ir.cron"].sudo()
    IrModel     = env["ir.model"].sudo()
    IrModelData = env["ir.model.data"].sudo()

    # If xmlid exists, nothing to do
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
        "numbercall": -1,
        "active": True,
        "user_id": SUPERUSER_ID,  # run as superuser to avoid ACL surprises
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
    """Remove scheduled action on uninstall (try XMLID, then heuristic search)."""
    IrCron = env["ir.cron"].sudo()

    # Try by xmlid
    try:
        rec = env.ref(CRON_XMLID)
        if rec:
            rec.unlink()
            return
    except ValueError:
        pass

    # Fallback by signature
    crons = IrCron.search([
        ("name", "=", CRON_NAME),
        ("state", "=", "code"),
        ("code", "ilike", CRON_METHOD),
    ])
    if crons:
        crons.unlink()


# ---------- Custom Field/Column Cleanup ----------
# SAFE LIST of columns to remove from DB (if they were created by this module).
# Adjust this list to match the fields you actually added in this addon.
SALE_ORDER_COLUMNS = [
    # Core Monta fields often added by this module:
    "monta_order_id",
    "monta_sync_state",
    "monta_last_push",
    "monta_needs_sync",
    # If you added Monta status fields directly on sale.order (optional):
    "monta_status",
    "monta_status_code",
    "monta_status_source",
    "monta_track_trace",
    "monta_last_sync",
]

# If your module added fields to other models/tables (example):
#   ("model.technical.name", "table_name", ["col_a", "col_b"])
EXTRA_MODEL_COLUMNS = [
    # Example:
    # ("monta.order.status", "monta_order_status", ["delivery_date", "track_trace_url"]),
]


def _unlink_ir_model_fields(env):
    """
    Try to remove fields via ORM so Odoo drops columns cleanly.
    We target fields created by this module on the specified models.
    """
    Fields = env["ir.model.fields"].sudo()
    Model  = env["ir.model"].sudo()

    # sale.order fields
    sale_model = Model._get("sale.order")
    if sale_model:
        f_recs = Fields.search([
            ("model_id", "=", sale_model.id),
            ("name", "in", SALE_ORDER_COLUMNS),
        ])
        if f_recs:
            f_recs.unlink()

    # Extra models (if any)
    for model_name, _table, cols in EXTRA_MODEL_COLUMNS:
        m = Model._get(model_name)
        if not m:
            continue
        f_recs = Fields.search([
            ("model_id", "=", m.id),
            ("name", "in", cols),
        ])
        if f_recs:
            f_recs.unlink()


def _drop_columns_if_exist(env, table, columns):
    """
    Last-resort SQL drop to clean up any lingering columns.
    Uses IF EXISTS so it won't error if already gone.
    """
    cr = env.cr
    for col in columns:
        # Sanitize identifiers by quoting (Odoo uses lowercase by default)
        cr.execute(f'ALTER TABLE "{table}" DROP COLUMN IF EXISTS "{col}" CASCADE')


def _force_drop_db_columns(env):
    """
    In case ORM unlink did not remove the columns (e.g., field was redefined or
    not tracked), force-drop them from DB.
    """
    # sale.order table is "sale_order"
    _drop_columns_if_exist(env, "sale_order", SALE_ORDER_COLUMNS)

    # Any extra models/tables:
    for _model_name, table, cols in EXTRA_MODEL_COLUMNS:
        _drop_columns_if_exist(env, table, cols)


# ---------- Public Hooks ----------
def post_init_hook(env):
    """Odoo 17/18 signature: receives `env`."""
    _ensure_cron(env)


def uninstall_hook(env):
    """Odoo 17/18 signature: receives `env`."""
    # 1) Remove scheduled action(s)
    _remove_cron(env)

    # 2) Remove fields via ORM (preferred)
    _unlink_ir_model_fields(env)

    # 3) Force-drop any lingering DB columns (safety net)
    _force_drop_db_columns(env)
