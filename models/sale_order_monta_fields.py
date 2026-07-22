# -*- coding: utf-8 -*-
from odoo import fields, models


class SaleOrder(models.Model):
    _inherit = "sale.order"

    # Existing Monta mirrors
    monta_order_ref = fields.Char(
        string="Monta Order Ref",
        copy=False,
        index=True,
        help="Reference of the order in Monta.",
    )
    monta_delivery_message = fields.Char(
        string="Monta Delivery Message",
        copy=False,
    )
    monta_delivery_date = fields.Date(
        string="Monta Delivery Date",
        copy=False,
    )
    monta_status_raw = fields.Text(
        string="Monta Status Raw (JSON)",
        copy=False,
    )

    # Mirror of 'Available on Monta'
    monta_on_monta = fields.Boolean(
        string="Available on Monta",
        copy=False,
        index=True,
        help="Checked if this order is known in Monta (monta_order_ref exists).",
    )

    monta_shipper_code = fields.Char(
        string="Monta Shipper Code",
        copy=False,
        help="The shipper code selected for pickup point delivery.",
    )
    monta_shipper_options = fields.Text(
        string="Monta Shipper Options (JSON)",
        copy=False,
        help="JSON string representing the shipper options for pickup point delivery.",
    )
    monta_delivery_type = fields.Selection(
        selection=[
            ("standard", "Standard Delivery"),
            ("next_day", "Next Day Delivery"),
            ("one_day", "1-Day Delivery"),
            ("two_day", "2-Day Delivery"),
            ("pickup", "Delivery Point (Pickup)"),
        ],
        string="Monta Delivery Type",
        default="standard",
        copy=False,
        help="The delivery speed/type selected for Monta fulfillment.",
    )
    monta_requested_delivery_date = fields.Datetime(
        string="Monta Requested Delivery Date",
        copy=False,
        help="Target delivery date sent to Monta (DeliveryDateRequested).",
    )

    monta_delivery_status = fields.Char(
        string="Monta Delivery Status",
        compute="_compute_monta_delivery_status",
        store=False,
        help="The current Monta delivery status — shows the latest renewal picking "
             "status if available, otherwise the SO-level Monta status.",
    )

    def _compute_monta_delivery_status(self):
        for so in self:
            # Prefer the most recent Monta-pushed outgoing picking's status
            pushed_pickings = so.picking_ids.filtered(
                lambda p: p.picking_type_code == "outgoing"
                and p.monta_pushed
                and p.monta_status
            )
            if pushed_pickings:
                latest = pushed_pickings.sorted(
                    key=lambda p: (p.create_date or fields.Datetime.now(), p.id),
                    reverse=True,
                )[0]
                so.monta_delivery_status = latest.monta_status
            elif so.monta_status:
                so.monta_delivery_status = so.monta_status
            else:
                so.monta_delivery_status = False

