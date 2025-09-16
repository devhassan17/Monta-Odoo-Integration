# -*- coding: utf-8 -*-
from odoo import SUPERUSER_ID

CRON_XMLID = "Monta-Odoo-Integration.ir_cron_monta_status_hourly"

def _ensure_hourly_cron(env):
    cron = env.ref(CRON_XMLID, raise_if_not_found=False)
    if cron:
        return cron
    cron = env["ir.cron"].sudo().create({
        "name": "Monta: Sync Order Status (hourly, no SO write)",
        "model_id": env["ir.model"]._get_id("monta.order.status"),
        "state": "code",
        "code": "env['monta.order.status'].cron_monta_sync_status(batch_limit=50)",
        "interval_number": 1,
        "interval_type": "hours",
        "numbercall": -1,
        "active": True,
    })
    env["ir.model.data"].sudo().create({
        "name": "ir_cron_monta_status_hourly",
        "module": "Monta-Odoo-Integration",
        "res_id": cron.id,
        "model": "ir.cron",
        "noupdate": True,
    })
    return cron

def post_init_hook(env):
    _ensure_hourly_cron(env)
    # Prime the table so you see data immediately
    env["monta.order.status"].sudo().cron_monta_sync_status(batch_limit=25)


def uninstall_hook(env):
    """Executed right before uninstall is finalized.

    - Remove cron
    - Drop our custom columns in case they linger
    - (Optional) remove Studio fields that start with 'x_monta_' on sale.order
    """
    # 1) Remove cron if present
    cron = env.ref(CRON_XMLID, raise_if_not_found=False)
    if cron:
        cron.sudo().unlink()

    # 2) Drop columns (idempotent)
    env.cr.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name='sale_order' AND column_name='monta_delivery_date') THEN
                EXECUTE 'ALTER TABLE sale_order DROP COLUMN IF EXISTS monta_delivery_date CASCADE';
            END IF;
        END$$;
    """)

    # 3) OPTIONAL — purge Studio fields on sale.order beginning with x_monta_
    #    Comment this block if you don't want to touch Studio records
    studio_fields = env['ir.model.fields'].sudo().search([
        ('model', '=', 'sale.order'),
        ('name', 'like', 'x_monta_%'),
    ])
    for f in studio_fields:
        # try drop column if exists
        try:
            env.cr.execute(f"ALTER TABLE sale_order DROP COLUMN IF EXISTS {f.name} CASCADE;")
        except Exception:
            pass
        f.unlink()
