# -*- coding: utf-8 -*-
from odoo import api, SUPERUSER_ID, fields

MODULE = "Monta-Odoo-Integration"

CRON_STATUS_XMLID = f"{MODULE}.ir_cron_monta_status_halfhourly"
CRON_STATUS_NAME  = "Monta: Sync order status every 30 min"

CRON_MIRROR_XMLID = f"{MODULE}.ir_cron_monta_mirror_hourly"
CRON_MIRROR_NAME  = "Monta: Mirror status to Sale Order every hour"

STUDIO_DATE_FIELD = "x_monta_delivery_date"   # custom field on sale.order


def _ensure_studio_date_field(env):
    """Create x_monta_delivery_date on sale.order if not present (Studio-style)."""
    Fields = env["ir.model.fields"].sudo()
    if Fields.search([("model", "=", "sale.order"), ("name", "=", STUDIO_DATE_FIELD)], limit=1):
        return
    sale_model = env["ir.model"].sudo().search([("model", "=", "sale.order")], limit=1)
    if not sale_model:
        return
    Fields.create({
        "name": STUDIO_DATE_FIELD,
        "model": "sale.order",
        "model_id": sale_model.id,
        "ttype": "date",
        "field_description": "Monta Delivery Date",
        "store": True,
        "state": "manual",
    })
    env.cr.commit()


def _ensure_status_cron(env):
    """Every 30 minutes: pull from Monta and upsert snapshots."""
    IMD  = env["ir.model.data"].sudo()
    Cron = env["ir.cron"].sudo()
    try:
        return env.ref(CRON_STATUS_XMLID)
    except ValueError:
        pass

    model = env["ir.model"].sudo().search([("model", "=", "monta.order.status")], limit=1)
    if not model:
        return

    cron = Cron.create({
        "name": CRON_STATUS_NAME,
        "model_id": model.id,
        "state": "code",
        "code": 'env["monta.order.status"].cron_monta_sync_status(batch_limit=500)',
        "interval_type": "minutes",
        "interval_number": 30,
        "nextcall": fields.Datetime.now(),
        "user_id": env.ref("base.user_root").id,
        "active": True,
    })
    IMD.create({
        "module": MODULE,
        "name": "ir_cron_monta_status_halfhourly",
        "model": "ir.cron",
        "res_id": cron.id,
        "noupdate": True,
    })
    env.cr.commit()
    return cron


def _ensure_mirror_cron(env):
    """Every hour: mirror latest snapshot to sale.order fields (dashboard)."""
    IMD  = env["ir.model.data"].sudo()
    Cron = env["ir.cron"].sudo()
    try:
        return env.ref(CRON_MIRROR_XMLID)
    except ValueError:
        pass

    model = env["ir.model"].sudo().search([("model", "=", "monta.order.status")], limit=1)
    if not model:
        return

    cron = Cron.create({
        "name": CRON_MIRROR_NAME,
        "model_id": model.id,
        "state": "code",
        "code": (
            'recs = env["monta.order.status"].sudo().search([])\n'
            'for r in recs:\n'
            '    env["monta.order.status"].sudo()._mirror_to_sale(r)\n'
            'True'
        ),
        "interval_type": "hours",
        "interval_number": 1,
        "nextcall": fields.Datetime.now(),
        "user_id": env.ref("base.user_root").id,
        "active": True,
    })
    IMD.create({
        "module": MODULE,
        "name": "ir_cron_monta_mirror_hourly",
        "model": "ir.cron",
        "res_id": cron.id,
        "noupdate": True,
    })
    env.cr.commit()
    return cron


def _initial_sync(env):
    """Run a first pass so the list view isn’t empty after install/upgrade."""
    env["monta.order.status"].sudo().cron_monta_sync_status(batch_limit=300)


def post_init_hook(cr, registry):
    """Create Studio field, ensure both crons, run initial sync."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    _ensure_studio_date_field(env)
    _ensure_status_cron(env)
    _ensure_mirror_cron(env)
    _initial_sync(env)


def uninstall_hook(cr, registry):
    """Clean up crons + Studio field to prevent uninstall errors."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    for xmlid in (CRON_STATUS_XMLID, CRON_MIRROR_XMLID):
        try:
            env.ref(xmlid).sudo().unlink()
        except ValueError:
            # xmlid never created; ignore
            pass
    env["ir.model.fields"].sudo().search([
        ("model", "=", "sale.order"),
        ("name", "=", STUDIO_DATE_FIELD),
    ]).unlink()
