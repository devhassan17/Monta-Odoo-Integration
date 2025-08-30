# -*- coding: utf-8 -*-
import json
import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)

DUMMY_ETA_STR = "2099-01-01 00:00:00"  # always UTC

class SaleOrderInbound(models.Model):
    _inherit = 'sale.order'

    def _monta__eta_from_body(self, body):
        """
        Decide which ETA we use for commitment_date.
        Priority (first non-empty):
          1) EstimatedDeliveryTo
          2) EstimatedDeliveryFrom
          3) DeliveryDate
          4) LatestDeliveryDate
          5) DeliveryDateRequested
          6) PlannedShipmentDate
          else -> DUMMY_ETA_STR
        """
        # Pull all candidates as plain strings (Monta returns ISO-like strings or null)
        get = lambda k: (body or {}).get(k)
        cands = [
            get('EstimatedDeliveryTo'),
            get('EstimatedDeliveryFrom'),
            get('DeliveryDate'),
            get('LatestDeliveryDate'),
            get('DeliveryDateRequested'),
            get('PlannedShipmentDate'),
        ]
        chosen = next((c for c in cands if c), None)

        # If Monta literally says “Unknown” (or blank), use dummy
        if not chosen or (isinstance(chosen, str) and chosen.strip().lower() == 'unknown'):
            chosen = DUMMY_ETA_STR

        # If Monta gave a timestamp like 2025-08-29T12:34:56, normalize to Odoo “YYYY-MM-DD HH:MM:SS”
        if 'T' in chosen and len(chosen) >= 19:
            chosen = chosen.replace('T', ' ')[:19]

        return chosen

    def _monta__vals_from_order_body(self, body):
        """
        Compute values to apply to sale.order from Monta GET /order/{id} body.
        We keep it small and explicit.
        """
        vals = {}
        # Status text if available
        remote_status = (body or {}).get('DeliveryStatusDescription') or (body or {}).get('Status') or (body or {}).get('ShipperDescription') or 'Received'
        vals['monta_remote_status'] = remote_status

        # Delivered at (prefer Shipped/DeliveryDate if present)
        shipped = (body or {}).get('Shipped') or (body or {}).get('DeliveryDate')
        if shipped and 'T' in shipped:
            shipped = shipped.replace('T', ' ')[:19]
        if shipped:
            vals['monta_delivered_at'] = shipped

        # commitment_date (ETA)
        chosen_eta = self._monta__eta_from_body(body)
        vals['commitment_date'] = chosen_eta

        # We set a small flag to show this order still needs an outbound sync (harmless)
        vals['monta_needs_sync'] = True
        return vals

    def action_monta_pull_now(self, channel='manual'):
        """
        Pull latest state for each order from Monta, update commitment_date + status.
        Creates two logs per call:
          - 'Monta Pull': raw API status/body (short)
          - 'Monta ETA' : the ETA decision (fields considered + chosen)
        """
        for order in self:
            try:
                webshop_id = order.monta_order_id or order.name
                if not webshop_id:
                    continue

                # GET /order/{webshop_id}
                client = order.env['monta.client.proxy'] if 'monta.client.proxy' in order.env else None
                if client:
                    status, body = client.request(order, 'GET', f'/order/{webshop_id}')
                else:
                    # Use the helper on sale.order if you have it:
                    status, body = order._monta_request('GET', f'/order/{webshop_id}')

                # Log API call (short)
                order._create_monta_log(
                    {'status': status, 'path': f'/order/{webshop_id}'},
                    level='info', tag='Monta Pull',
                    console_summary='[Monta Pull] GET /order/%s -> %s' % (webshop_id, status)
                )

                if 200 <= (status or 0) < 300 and isinstance(body, dict):
                    # Decide ETA + values to write
                    vals = self._monta__vals_from_order_body(body)

                    # Persist ETA decision as a separate readable log
                    eta_log_payload = {
                        'eta': {
                            'chosen_for_commitment_date': vals.get('commitment_date'),
                            'used_dummy_2099': vals.get('commitment_date') == DUMMY_ETA_STR,
                            'raw_fields': {
                                'EstimatedDeliveryFrom': body.get('EstimatedDeliveryFrom'),
                                'EstimatedDeliveryTo': body.get('EstimatedDeliveryTo'),
                                'DeliveryDate': body.get('DeliveryDate'),
                                'LatestDeliveryDate': body.get('LatestDeliveryDate'),
                                'DeliveryDateRequested': body.get('DeliveryDateRequested'),
                                'PlannedShipmentDate': body.get('PlannedShipmentDate'),
                                'Blocked': body.get('Blocked'),
                                'BlockedMessage': body.get('BlockedMessage'),
                                'Comment': body.get('Comment'),
                            }
                        }
                    }
                    order._create_monta_log(eta_log_payload, level='info', tag='Monta ETA', console_summary='[Monta ETA] decision saved')

                    # Apply changes
                    order.write(vals)

                    # Optional: info in server log about what changed
                    _logger.info("[Monta Pull] %s -> changed keys: %s", order.name, list(vals.keys()))
                else:
                    order._create_monta_log(
                        {'status': status, 'body': body or {}},
                        level='error', tag='Monta Pull',
                        console_summary='[Monta Pull] non-200 status'
                    )
            except Exception as e:
                _logger.error("[Monta Pull] Failure for %s: %s", order.name, e, exc_info=True)
        return True
