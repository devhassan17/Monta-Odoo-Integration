# -*- coding: utf-8 -*-
from odoo import api, SUPERUSER_ID

PARAM_KEYS = [
    "monta.base_url",
    "monta.username",
    "monta.password",
    "monta.warehouse_tz",
    "monta.inbound_warehouse_display_name",
    "monta.supplier_code_map",
    "monta.default_supplier_code",
    "monta.supplier_code_override",
    "monta.allowed_base_urls",
]

CRON_NAMES = []   # add any cron names you create programmatically
MENU_NAMES = []   # add any menu names you create programmatically

def uninstall_hook(env):
    """Odoo 17/18: uninstall_hook(env). Clean up without throwing errors."""
    # env is already sudo-capable; keep all ops sudo just in case
    env = env.sudo()
    ICP = env["ir.config_parameter"]

    # 1) scrub system params
    for k in PARAM_KEYS:
        try:
            ICP.set_param(k, "")
        except Exception:
            # never block uninstall because of params
            pass

    # 2) delete any programmatic crons/menus (we don't create any by default)
    if CRON_NAMES:
        try:
            env["ir.cron"].search([("name", "in", CRON_NAMES)]).unlink()
        except Exception:
            pass
    if MENU_NAMES:
        try:
            env["ir.ui.menu"].search([("name", "in", MENU_NAMES)]).unlink()
        except Exception:
            pass

    # 3) ensure module not in server_wide_modules (defensive)
    try:
        swm = (ICP.get_param("server_wide_modules") or "").strip()
        if swm:
            parts = [p.strip() for p in swm.split(",") if p.strip()]
            new = ",".join(p for p in parts if p != "Monta-Odoo-Integration")
            if new != swm:
                ICP.set_param("server_wide_modules", new)
    except Exception:
        pass
