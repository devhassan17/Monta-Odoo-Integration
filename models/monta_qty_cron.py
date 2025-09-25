# Monta-Odoo-Integration/models/monta_qty_cron.py
# Sync product qty + min stock from Monta every 6 hours

from odoo import api, models, SUPERUSER_ID
from ..services.monta_qty_sync import MontaQtySync
import logging

_logger = logging.getLogger(__name__)


class ProductProduct(models.Model):
    _inherit = "product.product"

    @api.model
    def cron_monta_qty_sync(self, limit=None):
        """
        Entry point for the 6-hour cron job or manual run.

        We sudo() HERE and pass that env into the service.
        The service no longer calls env.sudo() internally (fixes RPC_ERROR).
        """
        _logger.info("Running Monta Qty Sync (limit=%s)", limit)
        MontaQtySync(self.env.sudo()).run(limit=limit)


def post_init_hook(cr, registry):
    """Create the cron if it doesn't exist yet (idempotent)."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    cron_xmlid = "Monta-Odoo-Integration.ir_cron_monta_qty_sync"
    cron = env.ref(cron_xmlid, raise_if_not_found=False)
    if not cron:
        cron = env["ir.cron"].create({
            "name": "Monta: Sync StockAvailable + MinStock (6h)",
            "model_id": env.ref("product.model_product_product").id,
            "state": "code",
            "code": "model.cron_monta_qty_sync()",
            "interval_number": 6,
            "interval_type": "hours",
            "active": True,
        })
        _logger.info("✅ Created Monta stock sync cron (id=%s)", cron.id)
    else:
        _logger.info("ℹ️ Monta stock sync cron already exists (id=%s)", cron.id)
