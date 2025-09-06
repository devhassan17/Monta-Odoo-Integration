# -*- coding: utf-8 -*-
from odoo import api, SUPERUSER_ID
from odoo.api import Environment as OdooEnvironment

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

CRON_NAMES = []   # add any programmatically-created crons here if you make them later
MENU_NAMES = []   # add any programmatically-created menus here if you make them later


def _get_env_any(env_or_cr, registry=None):
    """
    Normalize to a real Environment regardless of what Odoo passes:
      - uninstall_hook(env)
      - uninstall_hook(cr, registry)
      - odd 'env-like' objects without .sudo()
    """
    # Case 1: real Environment
    if isinstance(env_or_cr, OdooEnvironment):
        try:
            return env_or_cr.sudo()
        except Exception:
            # Rare: env lacks sudo; rebuild from its cursor if present
            cr = getattr(env_or_cr, "cr", None)
            if cr is not None:
                return api.Environment(cr, SUPERUSER_ID, {})
            # As last resort, fall through to cursor path

    # Case 2: has a cursor attribute (env-like)
    cr_attr = getattr(env_or_cr, "cr", None)
    if cr_attr is not None:
        return api.Environment(cr_attr, SUPERUSER_ID, {}).sudo()

    # Case 3: raw cursor (legacy signature)
    # Heuristic: cursor usually has .execute
    if hasattr(env_or_cr, "execute"):
        return api.Environment(env_or_cr, SUPERUSER_ID, {}).sudo()

    # Last resort: try to get cursor off registry
    if registry is not None and hasattr(registry, "cursor"):
        with registry.cursor() as cr:
            return api.Environment(cr, SUPERUSER_ID, {}).sudo()

    # If all else fails, raise a clean error rather than an AssertionError
    raise RuntimeError("Uninstall hook could not obtain an Odoo Environment")


def uninstall_hook(env_or_cr, registry=None):
    env = _get_env_any(env_or_cr, registry)
    ICP = env["ir.config_parameter"]

    # 1) scrub system parameters (never block uninstall on errors)
    for k in PARAM_KEYS:
        try:
            ICP.set_param(k, "")
        except Exception:
            pass

    # 2) remove programmatic records if you ever create them
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

    # 3) defensive: ensure module not left in server_wide_modules
    try:
        swm = (ICP.get_param("server_wide_modules") or "").strip()
        if swm:
            parts = [p.strip() for p in swm.split(",") if p.strip()]
            new = ",".join(p for p in parts if p.lower() != "monta-odoo-integration")
            if new != swm:
                ICP.set_param("server_wide_modules", new)
    except Exception:
        pass
