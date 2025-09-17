from odoo import api, SUPERUSER_ID

MODULE = "Monta-Odoo-Integration"
CRON_XMLID = f"{MODULE}.ir_cron_monta_status_halfhourly"
CRON_NAME = "Monta: Sync order status every 30 min"
STUDIO_DATE_FIELD = "x_monta_delivery_date"   # custom field on sale.order

def _ensure_studio_date_field(env):
    Fields = env["ir.model.fields"].sudo()
    SaleModel = env["ir.model"].sudo().search([("model", "=", "sale.order")], limit=1)
    exists = Fields.search([("model", "=", "sale.order"), ("name", "=", STUDIO_DATE_FIELD)], limit=1)
    if not exists and SaleModel:
        Fields.create({
            "name": STUDIO_DATE_FIELD,
            "model": "sale.order",
            "model_id": SaleModel.id,
            "ttype": "date",
            "field_description": "Monta Delivery Date",
            "store": True,
        })
        env.cr.commit()

def _ensure_cron(env):
    Cron = env["ir.cron"].sudo()
    IMD = env["ir.model.data"].sudo()
    try:
        cron = env.ref(CRON_XMLID)
        return cron
    except ValueError:
        pass

    model_id = env.ref(f"{MODULE}.model_monta_order_status").id
    cron = Cron.create({
        "name": CRON_NAME,
        "model_id": model_id,
        "state": "code",
        "code": 'env["monta.order.status"].cron_monta_sync_status(batch_limit=500)',
        "interval_type": "minutes",
        "interval_number": 30,
        "nextcall": fields.Datetime.now(env),
        "user_id": env.ref("base.user_root").id,
        "active": True,
    })
    # register xmlid for future upgrades
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
    # run one pass and mirror to sale.order so Studio widgets update now
    MOS = env["monta.order.status"].sudo()
    MOS.cron_monta_sync_status(batch_limit=300)

@api.model
def post_init_hook(cr, registry):
    env = api.Environment(cr, SUPERUSER_ID, {})
    # 1) make sure Studio date exists
    _ensure_studio_date_field(env)
    # 2) create cron if missing
    _ensure_cron(env)
    # 3) first sync to populate list + mirror onto sale.order
    _initial_sync(env)

@api.model
def uninstall_hook(cr, registry):
    env = api.Environment(cr, SUPERUSER_ID, {})
    # delete the cron
    try:
        env.ref(CRON_XMLID).sudo().unlink()
    except ValueError:
        pass
    # delete the Studio field
    Fields = env["ir.model.fields"].sudo()
    Fields.search([("model", "=", "sale.order"), ("name", "=", STUDIO_DATE_FIELD)]).unlink()
