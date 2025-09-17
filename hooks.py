import logging
from datetime import datetime, timedelta

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

CRON_XMLID = "Monta-Odoo-Integration.ir_cron_monta_status_halfhourly"

def _safe_ref(env, xmlid):
    try:
        return env.ref(xmlid)
    except Exception:
        return env['ir.model.data']

def post_init_hook(cr, registry):
    """Create a 30-min cron and run one initial sync + mirror (idempotent)."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    Cron = env["ir.cron"].sudo()
    IMD = env["ir.model.data"].sudo()

    # 1) Ensure cron exists (no XML data file, do it programmatically)
    try:
        env.ref(CRON_XMLID)
        _logger.info("[Monta] Cron already exists: %s", CRON_XMLID)
    except Exception:
        model_id = env.ref("Monta-Odoo-Integration.model_monta_order_status").id
        cron = Cron.create({
            "name": "Monta: Sync order status (every 30 min)",
            "model_id": model_id,
            "state": "code",
            "code": 'env["monta.order.status"].cron_monta_sync_status(batch_limit=300)',
            "interval_type": "minutes",
            "interval_number": 30,
            "nextcall": datetime.utcnow(),  # run soon
            "active": True,
            "user_id": env.ref("base.user_root").id,
        })
        IMD.create({
            "module": "Monta-Odoo-Integration",
            "name": "ir_cron_monta_status_halfhourly",
            "model": "ir.cron",
            "res_id": cron.id,
            "noupdate": True,
        })
        _logger.info("[Monta] Cron created and registered: %s (id=%s)", CRON_XMLID, cron.id)

    # 2) Initial quick pass (does not fail install if HTTP not yet configured)
    try:
        env["monta.order.status"].sudo().cron_monta_sync_status(batch_limit=100)
    except Exception as e:
        _logger.warning("[Monta] Initial sync skipped: %s", e)


def uninstall_hook(cr, registry):
    """Remove the cron cleanly so uninstall doesn’t error."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    try:
        cron = env.ref(CRON_XMLID)
        cron.sudo().unlink()
        _logger.info("[Monta] Removed cron %s during uninstall", CRON_XMLID)
    except Exception:
        _logger.info("[Monta] Cron %s was not present, nothing to remove", CRON_XMLID)
