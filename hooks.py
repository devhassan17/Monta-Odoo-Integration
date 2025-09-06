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
]

# If you ever create crons/menus programmatically, list their names here to delete on uninstall.
CRON_NAMES = []
MENU_NAMES = []

def uninstall_hook(cr, registry):
    """Full cleanup in Python so uninstall is safe & quiet."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    ICP = env["ir.config_parameter"].sudo()

    # 1) scrub system params
    for k in PARAM_KEYS:
        try:
            ICP.set_param(k, "")
        except Exception:
            pass

    # 2) delete any programmatic crons/menus (by name; we don't use XML)
    if CRON_NAMES:
        try:
            env["ir.cron"].sudo().search([("name", "in", CRON_NAMES)]).unlink()
        except Exception:
            pass
    if MENU_NAMES:
        try:
            env["ir.ui.menu"].sudo().search([("name", "in", MENU_NAMES)]).unlink()
        except Exception:
            pass

    # 3) ensure not in server_wide_modules
    try:
        swm = ICP.get_param("server_wide_modules") or ""
        if "Monta-Odoo-Integration" in swm:
            new = ",".join(x for x in swm.split(",") if x.strip() != "Monta-Odoo-Integration")
            ICP.set_param("server_wide_modules", new)
    except Exception:
        pass
