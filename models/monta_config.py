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
    origin = fields.Char(string="Origin")
    match_loose = fields.Boolean(string="Loose Matching", default=True)

    # Companies
    allowed_company_ids = fields.Many2many(
        "res.company",
        "monta_config_company_rel",
        "config_id",
        "company_id",
        string="Allowed Companies",
        help="Only these companies are allowed to push/sync with Monta. Leave empty to allow all."
    )

    # ---- Singleton helpers ----
    @api.model
    def _get_singleton(self):
        rec = self.sudo().search([], limit=1)
        if not rec:
            rec = self.sudo().create({})
        return rec

    @api.model
    def action_open_config(self):
        cfg = self._get_singleton()
        return {
            "type": "ir.actions.act_window",
            "name": "Monta Configuration",
            "res_model": "monta.config",
            "view_mode": "form",
            "target": "current",
            "res_id": cfg.id,
        }

    # Used by services
    @api.model
    def get_config(self):
        return self._get_singleton()

    def ensure_company_allowed(self, company):
        self.ensure_one()
        if self.allowed_company_ids and company and company.id not in self.allowed_company_ids.ids:
            raise ValidationError(
                _("Company '%s' is not allowed in Monta Configuration.") % company.display_name
            )
        return True
