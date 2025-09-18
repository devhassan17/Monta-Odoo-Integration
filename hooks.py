# -*- coding: utf-8 -*-
"""
Odoo 18 hooks for Monta-Odoo-Integration

- post_init_hook(env): ensure the Monta status sync cron exists
- uninstall_hook(env): remove the cron + force-drop custom columns this module added

Notes
- We DO NOT call env.sudo(); we sudo() only on recordsets (env['model'].sudo()).
- We DO NOT unlink ir.model.fields via ORM (that can raise "Invalid Operation").
  Instead, we drop columns via SQL and DELETE ir_model_fields via SQL.
"""

from odoo import SUPERUSER_ID

# ---------- Scheduled Action (Cron) ----------
CRON_XMLID   = "Monta-Odoo-Integration.ir_cron_monta_status_halfhourly"
CRON_NAME    = "Monta: Sync Sales Order Status (half-hourly)"
CRON_MODEL   = "sale.order"
CRON_METHOD  = "cron_monta_sync_status"
CRON_CODE    = f"model.{CRON_METHOD}(batch_limit=200)"

# ---------- Column cleanup ----------
# Adjust this list to match ONLY the columns your addon added to sale.order
SALE_ORDER_COLUMNS = [
    "monta_order_id",
    "monta_sync_state",
    "monta_last_push",
    "monta_needs_sync",
    # If your addon also added these directly on sale.order, keep them; else remove:
    "monta_status",
    "monta_status_code",
    "monta_status_source",
    "monta_track_trace",
    "monta_last_sync",
]

# For extra models/tables your addon created columns on, list them here:
# Each item: (model_name, table_name, [columns...])
EXTRA_MODEL_COLUMNS = [
    # Example:
    # ("monta.order.status", "monta_order_status", ["delivery_date", "track_trace_url"]),
]


# ---------------- helpers ----------------
def _ensure_cron(env):
    """Create the scheduled action if missing (idempotent via XMLID)."""
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


def _drop_columns_if_exist(env, table, columns):
    """
    Force-drop lingering columns with SQL.
    Uses IF EXISTS + CASCADE so it won't error if column already gone.
    """
    cr = env.cr
    for col in columns:
        cr.execute(f'ALTER TABLE "{table}" DROP COLUMN IF EXISTS "{col}" CASCADE')


def _force_drop_db_columns(env):
    """Drop sale.order and any extra model columns our addon created."""
    # sale.order table is "sale_order"
    _drop_columns_if_exist(env, "sale_order", SALE_ORDER_COLUMNS)

    # Any extra models/tables:
    for _model_name, table, cols in EXTRA_MODEL_COLUMNS:
        _drop_columns_if_exist(env, table, cols)


def _purge_ir_model_fields_sql(env):
    """
    Remove ir_model_fields rows by SQL (bypass "manual-only" ORM guard).
    We match by model name and field names to clean registry on next load.
    """
    cr = env.cr
    # sale.order
    cr.execute("""
        DELETE FROM ir_model_fields
        WHERE model = %s
          AND name = ANY(%s)
    """, ('sale.order', SALE_ORDER_COLUMNS))

    # extras
    for model_name, _table, cols in EXTRA_MODEL_COLUMNS:
        cr.execute("""
            DELETE FROM ir_model_fields
            WHERE model = %s
              AND name = ANY(%s)
        """, (model_name, cols))


# ---------- Public Hooks ----------
def post_init_hook(env):
    """Odoo 17/18 signature: receives `env`."""
    _ensure_cron(env)


def uninstall_hook(env):
    """Odoo 17/18 signature: receives `env`."""
    # 1) Remove scheduled action(s)
    _remove_cron(env)

    # 2) Force-drop DB columns (safe, idempotent)
    _force_drop_db_columns(env)

    # 3) Purge ir_model_fields rows for those columns (bypass ORM unlink guard)
    _purge_ir_model_fields_sql(env)
