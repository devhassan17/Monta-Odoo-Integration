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

CRON_NAMES = []   # if you create any crons programmatically, add their names here
MENU_NAMES = []   # if you create menus programmatically, add their names here

def _ensure_env(env_or_cr, maybe_registry=None):
    """
    Accept both modern and legacy hook signatures:
      - uninstall_hook(env)
      - uninstall_hook(cr, registry)
    Return a sudoed Environment.
    """
    try:
        # Modern style: got an Environment-like object
        # Some builds pass a "thin" env without .sudo(); rebuild a proper one.
        cr = getattr(env_or_cr, "cr", None)
        uid = getattr(env_or_cr, "uid", SUPERUSER_ID)
        if cr:
            return api.Environment(cr, uid or SUPERUSER_ID, {}).sudo()
    except Exception:
        pass

    # Legacy: first arg is cursor
    cr = env_or_cr
    return api.Environment(cr, SUPERUSER_ID, {}).sudo()

def uninstall_hook(env_or_cr, registry=None):
    env = _ensure_env(env_or_cr, registry)
    ICP = env["ir.config_parameter"]

    # 1) scrub system params (never block uninstall)
    for k in PARAM_KEYS:
        try:
            ICP.set_param(k, "")
        except Exception:
            pass

    # 2) remove any programmatic records you may have created
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

    # 3) defensive: ensure module not pinned in server_wide_modules
    try:
        swm = (ICP.get_param("server_wide_modules") or "").strip()
        if swm:
            parts = [p.strip() for p in swm.split(",") if p.strip()]
            new = ",".join(p for p in parts if p.lower() != "monta-odoo-integration")
            if new != swm:
                ICP.set_param("server_wide_modules", new)
    except Exception:
        pass
