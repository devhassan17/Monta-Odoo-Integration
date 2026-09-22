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
        help="Comma-separated list of Odoo web.base.url values allowed to push to Monta. Leave empty to allow all.",
    )
    enabled = fields.Boolean(string="Global Sync Enabled", default=True, help="Turn off to completely disable all Monta push/sync operations.")
    origin = fields.Char(string="Origin", help="Optional Monta 'Origin' field (send only if set).")
    match_loose = fields.Boolean(string="Loose Matching", default=True)
    is_staging_mode = fields.Boolean(
        string="Staging / Test Mode", 
        default=False,
        help="If enabled, 'Test ' is automatically prefixed to the Order Number sent to Monta."
    )

    # Companies
    allowed_company_ids = fields.Many2many(
        "res.company",
        "monta_config_company_rel",
        "config_id",
        "company_id",
        string="Allowed Companies",
        help="Only these companies are allowed to push/sync with Monta. Leave empty to allow all companies.",
    )

    # Inbound Forecast settings
    inbound_enable = fields.Boolean(string="Enable Inbound Forecast", default=False)
    warehouse_tz = fields.Char(string="Warehouse Timezone", default="Europe/Amsterdam")
    inbound_warehouse_display_name = fields.Char(string="Inbound Warehouse Display Name")

    supplier_code_override = fields.Char(string="Supplier Code Override")
    supplier_code_map = fields.Text(string="Supplier Code Map (JSON)", default="{}")
    default_supplier_code = fields.Char(string="Default Supplier Code")

    # Delivery Filter
    enable_route_filter = fields.Boolean(
        string="Enable Route Filter",
        default=False,
        help="If enabled, only orders with delivery products matching the selected Monta Routes will be pushed."
    )
    monta_route_ids = fields.Many2many(
        "stock.route",
        string="Monta Routes",
        help="If route filter is enabled, only orders using a delivery product with one of these routes will be pushed to Monta.",
    )
    route_filter_skip_subscriptions = fields.Boolean(
        string="Route Filter: Skip Subscriptions",
        default=True,
        help="When enabled, the Route Filter is NOT applied to subscription orders (new subscriptions and renewals). "
             "They will always be pushed to Monta regardless of the route configuration.",
    )
    # Delivery Options Visibility
    enable_next_day_delivery = fields.Boolean(
        string="Enable Next Day Delivery",
        default=True,
        help="If enabled, Next Day Delivery option will be available to customers at checkout."
    )
    next_day_allowed_partner_ids = fields.Many2many(
        "res.partner",
        "monta_config_next_day_partner_rel",
        "config_id",
        "partner_id",
        string="Allowed Customers (Next Day)",
        help="If specified, only these customers will see Next Day Delivery. Leave empty to allow all customers."
    )
    enable_pickup_points = fields.Boolean(
        string="Enable Delivery Points",
        default=True,
        help="If enabled, Delivery/Pickup Points option will be available to customers at checkout."
    )
    pickup_allowed_partner_ids = fields.Many2many(
        "res.partner",
        "monta_config_pickup_partner_rel",
        "config_id",
        "partner_id",
        string="Allowed Customers (Delivery Points)",
        help="If specified, only these customers will see Delivery Points. Leave empty to allow all customers."
    )

    # -------------------------
    # Singleton helpers
    # -------------------------
    @api.model
    def get_singleton(self):
        """Always keep exactly one config record in the DB."""
        rec = self.sudo().search([], limit=1)
        if not rec:
            rec = self.sudo().create({"name": "Monta Configuration"})
        return rec

    @api.model
    def get_config(self):
        """Preferred getter used by services."""
        return self.get_singleton()

    @api.model
    def get_for_company(self, company):
        """
        Backward-compatible helper used by other models.
        Returns config if enabled AND company is allowed, otherwise None.
        """
        cfg = self.get_singleton()
        if not cfg.enabled:
            return None
        if cfg.allowed_company_ids and company and company.id not in cfg.allowed_company_ids.ids:
            return None
        return cfg

    def ensure_company_allowed(self, company):
        """Raise if company is not allowed."""
        cfg = self.get_singleton()
        if cfg.allowed_company_ids and company and company.id not in cfg.allowed_company_ids.ids:
            raise ValidationError(_("Company '%s' is not allowed in Monta Configuration.") % company.display_name)
        return True

    def is_partner_allowed(self, partner, allowed_partners=None):
        """Check if a specific partner is allowed to use a delivery option.
        If allowed_partners is empty or None, all customers are allowed.
        If allowed_partners is set, only selected partners (or parent company) are allowed."""
        cfg = self.get_singleton()
        if not cfg.enabled:
            return False
        if not allowed_partners:
            return True
        if not partner:
            return False
        # If partner has a parent (company), check the parent as well
        partner_ids = [partner.id]
        if partner.parent_id:
            partner_ids.append(partner.parent_id.id)
        return bool(allowed_partners.filtered(lambda p: p.id in partner_ids))

    # -------------------------
    # UI Action: always open singleton
    # -------------------------
    @api.model
    def action_open_config(self):
        cfg = self.get_singleton()
        return {
            "type": "ir.actions.act_window",
            "name": "Monta Configuration",
            "res_model": "monta.config",
            "view_mode": "form",
            "target": "current",
            "res_id": cfg.id,
        }
