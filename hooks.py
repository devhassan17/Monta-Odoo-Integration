# -*- coding: utf-8 -*-
from odoo import api, SUPERUSER_ID, fields

MODULE = "Monta-Odoo-Integration"
CRON_XMLID = f"{MODULE}.ir_cron_monta_status_halfhourly"
CRON_NAME = "Monta: Sync order status every 30 min"
STUDIO_DATE_FIELD = "x_monta_delivery_date"   # custom field on sale.order


def _ensure_studio_date_field(env):
    """
    Create a Studio-like custom field on sale.order if missing.
    Name: x_monta_delivery_date (ttype=date, store=True)
    """
    Fields = env["ir.model.fields"].sudo()
    if Fields.search([("model", "=", "sale.order"), ("name", "=", STUDIO_DATE_FIELD)], limit=1):
        return

    SaleModel = env["ir.model"].sudo().search([("model", "=", "sale.order")], limit=1)
    if not SaleModel:
        return

    Fields.create({
        "name": STUDIO_DATE_FIELD,
        "model": "sale.order",
        "model_id": SaleModel.id,
        "ttype": "date",
        "field_description": "Monta Delivery Date",
        "store": True,
        "state": "manual",   # mark as custom
    })
    env.cr.commit()


def _ensure_cron(env):
    """
    Create (and register xmlid for) a half-hourly cron that calls:
        env["monta.order.status"].cron_monta_sync_status(batch_limit=500)
    """
    IMD = env["ir.model.data"].sudo()
    Cron = env["ir.cron"].sudo()

    # if already registered with xmlid, return it
    try:
        return env.ref(CRON_XMLID)
    except ValueError:
        pass

    # find model_id without relying on an xmlid
    model = env["ir.model"].sudo().search([("model", "=", "monta.order.status")], limit=1)
    if not model:
        return

    cron = Cron.create({
        "name": CRON_NAME,
        "model_id": model.id,
        "state": "code",
        "code": 'env["monta.order.status"].cron_monta_sync_status(batch_limit=500)',
        "interval_type": "minutes",
        "interval_number": 30,
        "nextcall": fields.Datetime.now(),  # run from now
        "user_id": env.ref("base.user_root").id,
        "active": True,
    })

    # register a stable external id so upgrades/uninstall can find it
    IMD.create({
        "module": MODULE,
        "name": "ir_cron_monta_status_halfhourly",
        "model": "ir.cron",
        "res_id": cron.id,
        "noupdate": True,
    })
    env.cr.commit()
    return cron


def _initial_sync(env):
    """
    Kick a first pass so the Sales dashboard has data mirrored immediately.
    (If Monta config is missing, it will no-op with the warning you saw.)
    """
    env["monta.order.status"].sudo().cron_monta_sync_status(batch_limit=300)


# ---------------- Odoo lifecycle hooks ----------------

def post_init_hook(cr, registry):
    """
    Runs after install/upgrade:
      1) creates x_monta_delivery_date on sale.order (if missing)
      2) ensures the 30-min cron exists
      3) runs one sync pass
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    _ensure_studio_date_field(env)
    _ensure_cron(env)
    _initial_sync(env)


def uninstall_hook(cr, registry):
    """
    Cleanly remove the cron and the Studio date field on uninstall.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    # remove cron if present
    try:
        env.ref(CRON_XMLID).sudo().unlink()
    except ValueError:
        pass

    # remove the custom field
    env["ir.model.fields"].sudo().search([
        ("model", "=", "sale.order"),
        ("name", "=", STUDIO_DATE_FIELD),
    ]).unlink()
