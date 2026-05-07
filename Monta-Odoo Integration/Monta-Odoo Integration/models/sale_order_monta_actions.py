# -*- coding: utf-8 -*-
from odoo import models


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def action_open_monta_order_status(self):
        self.ensure_one()

        act = self.env.ref(
            "Monta-Odoo-Integration.action_monta_order_status",
            raise_if_not_found=False,
        )
        if not act:
            return False

        action = dict(act.read()[0])
        action["domain"] = [("order_name", "=", self.name)]
        action["context"] = {"search_default_order_name": self.name}
        return action
