# -*- coding: utf-8 -*-
from odoo import SUPERUSER_ID


# Existing (sales status) cron
CRON_XMLID = "Monta-Odoo-Integration.ir_cron_monta_status_halfhourly"
CRON_NAME = "Monta: Sync Sales Order Status (half-hourly)"
CRON_MODEL = "sale.order"
CRON_METHOD = "cron_monta_sync_status"
CRON_CODE = f"model.{CRON_METHOD}(batch_limit=200)"
CRON_INTERVAL_MIN = 30  # minutes

# Stock/qty sync (product model)
CRON_QTY_XMLID = "Monta-Odoo-Integration.ir_cron_monta_qty_sync"
CRON_QTY_NAME = "Monta: Sync StockAvailable + MinStock (6h)"
CRON_QTY_MODEL = "product.product"
CRON_QTY_METHOD = "cron_monta_qty_sync"
CRON_QTY_CODE = f"model.{CRON_QTY_METHOD}()"
CRON_QTY_HOURS = 6

CRON_PULL_XMLID = "Monta-Odoo-Integration.ir_cron_monta_stock_pull"
CRON_PULL_NAME = "Monta: Pull stock list (/stock) (6h)"
CRON_PULL_MODEL = "product.product"
CRON_PULL_METHOD = "cron_monta_stock_pull"
CRON_PULL_CODE = f"model.{CRON_PULL_METHOD}()"
CRON_PULL_HOURS = 6


def _create_cron_record(env, xmlid, name, model, code, interval_number, interval_type, user_id=SUPERUSER_ID):
    """Idempotent cron creation: if env.ref(xmlid) exists -> skip."""
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
    _create_cron_record(env, CRON_XMLID, CRON_NAME, CRON_MODEL, CRON_CODE, CRON_INTERVAL_MIN, "minutes")
    _create_cron_record(env, CRON_QTY_XMLID, CRON_QTY_NAME, CRON_QTY_MODEL, CRON_QTY_CODE, CRON_QTY_HOURS, "hours")
    _create_cron_record(env, CRON_PULL_XMLID, CRON_PULL_NAME, CRON_PULL_MODEL, CRON_PULL_CODE, CRON_PULL_HOURS, "hours")


def _remove_cron(env):
    IrCron = env["ir.cron"].sudo()

    for xmlid in (CRON_XMLID, CRON_QTY_XMLID, CRON_PULL_XMLID):
        try:
            rec = env.ref(xmlid)
            if rec:
                rec.unlink()
        except ValueError:
            pass

    for name in (CRON_NAME, CRON_QTY_NAME, CRON_PULL_NAME):
        crons = IrCron.search([
            ("name", "=", name),
            ("state", "=", "code"),
            ("code", "ilike", "cron_monta_"),
        ])
        if crons:
            crons.unlink()


def _migrate_icp_to_monta_config(env):
    """
    One-time migration: copy existing ir.config_parameter values to monta.config singleton.
    Safe if already migrated.
    """
    ICP = env["ir.config_parameter"].sudo()
    Config = env["monta.config"].sudo()

    config = Config.search([], limit=1)
    if not config:
        config = Config.create({"name": "Monta Configuration"})

    # Only set values if config fields are empty (don’t overwrite UI changes)
    def _set_if_empty(field, value):
        if value is None:
            return
        if not getattr(config, field):
            config.write({field: value})

    _set_if_empty("base_url", (ICP.get_param("monta.base_url") or ICP.get_param("monta.api.base_url") or "").strip() or "https://api-v6.monta.nl")
    _set_if_empty("username", (ICP.get_param("monta.username") or ICP.get_param("monta.api.user") or "").strip())
    _set_if_empty("password", (ICP.get_param("monta.password") or ICP.get_param("monta.api.password") or "").strip())
    _set_if_empty("channel", (ICP.get_param("monta.channel") or ICP.get_param("monta.api.channel") or "").strip())
    _set_if_empty("timeout", int(ICP.get_param("monta.timeout") or ICP.get_param("monta.api.timeout") or 20))

    _set_if_empty("allowed_base_urls", (ICP.get_param("monta.allowed_base_urls") or "").strip())
    _set_if_empty("origin", (ICP.get_param("monta.origin") or "").strip())
    _set_if_empty("match_loose", (ICP.get_param("monta.match_loose") or "1").strip() != "0")

    _set_if_empty("warehouse_tz", (ICP.get_param("monta.warehouse_tz") or "Europe/Amsterdam").strip())
    _set_if_empty("inbound_warehouse_display_name", (ICP.get_param("monta.inbound_warehouse_display_name") or "").strip())
    _set_if_empty("inbound_enable", (ICP.get_param("monta.inbound_enable") or "").strip().lower() in ("1", "true", "yes", "on"))

    _set_if_empty("supplier_code_override", (ICP.get_param("monta.supplier_code_override") or "").strip())
    _set_if_empty("supplier_code_map", (ICP.get_param("monta.supplier_code_map") or "{}").strip())
    _set_if_empty("default_supplier_code", (ICP.get_param("monta.default_supplier_code") or "").strip())


def post_init_hook(env):
    _ensure_cron(env)
    _migrate_icp_to_monta_config(env)


def uninstall_hook(env):
    _remove_cron(env)
