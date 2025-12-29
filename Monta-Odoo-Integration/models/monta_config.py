# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class MontaConfig(models.Model):
    _name = "monta.config"
    _description = "Monta Configuration"
    _rec_name = "name"

    name = fields.Char(default="Monta Configuration", required=True)

    # API
    base_url = fields.Char(string="Base URL", default="https://api-v6.monta.nl", required=True)
    username = fields.Char(string="Username")
    password = fields.Char(string="Password")
    timeout = fields.Integer(string="Timeout (seconds)", default=20)
    channel = fields.Char(string="Channel")

    # Guards / behavior
    allowed_base_urls = fields.Text(
        string="Allowed Odoo Base URLs",
        help="Comma-separated list of Odoo web.base.url values allowed to push to Monta. Leave empty to allow all."
    )
    origin = fields.Char(string="Origin", help="Optional Monta 'Origin' field (send only if set).")
    match_loose = fields.Boolean(string="Loose Matching", default=True)

    # Companies
    allowed_company_ids = fields.Many2many(
        "res.company",
        "monta_config_company_rel",
        "config_id",
        "company_id",
        string="Allowed Companies",
        help="Only these companies are allowed to push/sync with Monta. Leave empty to allow all companies."
    )

    # Inbound Forecast settings
    inbound_enable = fields.Boolean(string="Enable Inbound Forecast", default=False)
    warehouse_tz = fields.Char(string="Warehouse Timezone", default="Europe/Amsterdam")
    inbound_warehouse_display_name = fields.Char(string="Inbound Warehouse Display Name")

    supplier_code_override = fields.Char(string="Supplier Code Override")
    supplier_code_map = fields.Text(string="Supplier Code Map (JSON)", default="{}")
    default_supplier_code = fields.Char(string="Default Supplier Code")

    # ---- singleton helpers ----
    @api.model
    def get_singleton(self):
        rec = self.sudo().search([], limit=1)
        if not rec:
            rec = self.sudo().create({"name": "Monta Configuration"})
        return rec

    @api.model
    def get_for_company(self, company):
        cfg = self.get_singleton()
        if cfg.allowed_company_ids and company and company.id not in cfg.allowed_company_ids.ids:
            return None
        return cfg

    def ensure_company_allowed(self, company):
        cfg = self.get_singleton()
        if cfg.allowed_company_ids and company and company.id not in cfg.allowed_company_ids.ids:
            raise ValidationError(_("Company '%s' is not allowed in Monta Configuration.") % company.display_name)
        return True
