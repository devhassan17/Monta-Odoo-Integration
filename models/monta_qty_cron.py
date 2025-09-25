# -*- coding: utf-8 -*-
import logging
from datetime import datetime, timedelta

from odoo import api, fields, models, SUPERUSER_ID

from odoo.addons.Monta_Odoo_Integration.services.monta_qty_sync import MontaQtySync

_logger = logging.getLogger(__name__)


class ProductProduct(models.Model):
    _inherit = "product.product"

    @api.model
    def cron_monta_qty_sync(self, limit=None):
        """Entry point for the 6-hour cron job."""
        _logger.info("Running Monta Qty Sync (limit=%s)", limit)
        MontaQtySync(self.env).run(limit=limit)
        return True


def _ensure_server_action_and_cron(env):
    """Create/update a server action and a 6-hour cron for the sync."""
    IrActionsServer = env["ir.actions.server"].sudo()
    IrCron = env["ir.cron"].sudo()

    # Server Action: call model method (no safe_eval code)
    srv = IrActionsServer.search(
        [("name", "=", "Monta: Sync StockAvailable + MinStock"), ("state", "=", "code")],
        limit=1,
    )
    if not srv:
        srv = IrActionsServer.create(
            {
                "name": "Monta: Sync StockAvailable + MinStock",
                "state": "code",
                "model_id": env.ref("product.model_product_product").id,
                "code": "env['product.product'].cron_monta_qty_sync()",
            }
        )
    else:
        # keep the code aligned
        srv.write(
            {
                "model_id": env.ref("product.model_product_product").id,
                "code": "env['product.product'].cron_monta_qty_sync()",
            }
        )

    # Cron: every 6 hours, active
    cron = IrCron.search(
        [("name", "=", "Monta: Sync StockAvailable + MinStock (6h)")], limit=1
    )
    # compute nextcall a bit in the future to avoid immediate burst on install
    nextcall = fields.Datetime.to_string(fields.Datetime.now() + timedelta(minutes=5))

    vals = {
        "name": "Monta: Sync StockAvailable + MinStock (6h)",
        "active": True,
        "interval_number": 6,
        "interval_type": "hours",  # Odoo 18 valid values: minutes/hours/days/weeks/months
        "numbercall": -1,          # ignored by Odoo 18 but accepted; remove if your run complains
        "user_id": SUPERUSER_ID,
        "ir_actions_server_id": srv.id,
        "nextcall": nextcall,
        "doall": False,            # do not catch up missed runs
    }

    if not cron:
        IrCron.create(vals)
    else:
        # Keep schedule in sync, but don't reset nextcall if already set
        update_vals = vals.copy()
        update_vals.pop("nextcall", None)
        cron.write(update_vals)


def post_init_hook(cr, registry):
    """Hook if you prefer post-init from __manifest__."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    _ensure_server_action_and_cron(env)


class _Bootstrap(models.AbstractModel):
    """Lightweight init via registry model init, for environments without hooks."""

    _name = "monta.qty.cron.bootstrap"
    _description = "Bootstrap Monta Qty Cron"

    @api.model
    def init(self):
        env = self.env
        try:
            _ensure_server_action_and_cron(env)
        except Exception as e:
            _logger.warning("Could not ensure Monta cron/server action: %s", e)
