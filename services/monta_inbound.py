# # -*- coding: utf-8 -*-
# import json
# import logging
# from datetime import datetime
# from typing import Dict, Tuple, Any, Optional

# from odoo import fields

# from .monta_client import MontaClient  # reuse your existing client

# _logger = logging.getLogger(__name__)


# def _norm_iso_dt(value) -> Optional[str]:
#     if not value:
#         return None
#     if isinstance(value, datetime):
#         return fields.Datetime.to_string(value)
#     s = str(value).strip()
#     if not s:
#         return None
#     if s.endswith('Z'):
#         s = s[:-1] + '+00:00'
#     try:
#         dt = datetime.fromisoformat(s)
#         return fields.Datetime.to_string(dt.replace(tzinfo=None))
#     except Exception:
#         try:
#             s2 = s.replace('T', ' ').split('.')[0]
#             dt = datetime.strptime(s2[:19], '%Y-%m-%d %H:%M:%S')
#             return fields.Datetime.to_string(dt)
#         except Exception:
#             return None


# class MontaInbound:
#     """
#     GET /order/{webshoporderid} and map ETA -> sale.order.commitment_date.
#     If unknown/missing -> dummy '2099-01-01 00:00:00'.
#     """

#     DUMMY_ETA_STR = "2099-01-01 00:00:00"

#     def __init__(self, env):
#         self.env = env

#     def fetch_order(self, order, webshop_id: str, channel: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
#         path = f"/order/{webshop_id}"
#         if channel:
#             path = f"{path}?channel={channel}"
#         client = MontaClient(self.env)
#         status, body = client.request(order, "GET", path, payload=None, headers={"Accept": "application/json"})
#         order._create_monta_log(
#             {'pull': {'status': status, 'webshop_id': webshop_id, 'channel': channel, 'body_excerpt': (body if isinstance(body, dict) else {})}},
#             level='info' if (200 <= (status or 0) < 300) else 'error',
#             tag='Monta Pull',
#             console_summary=f"[Monta Pull] GET {path} -> {status}",
#         )
#         return status, body or {}

#     def _extract_eta_for_commitment(self, payload: Dict[str, Any]) -> Tuple[str, Dict[str, Any], bool]:
#         raw = {
#             'EstimatedDeliveryFrom': payload.get('EstimatedDeliveryFrom'),
#             'EstimatedDeliveryTo': payload.get('EstimatedDeliveryTo'),
#             'DeliveryDate': payload.get('DeliveryDate'),
#             'LatestDeliveryDate': payload.get('LatestDeliveryDate'),
#             'DeliveryDateRequested': payload.get('DeliveryDateRequested'),
#             'PlannedShipmentDate': payload.get('PlannedShipmentDate'),
#             'Blocked': payload.get('Blocked'),
#             'BlockedMessage': payload.get('BlockedMessage'),
#             'Comment': payload.get('Comment'),
#         }
#         for key in ('EstimatedDeliveryFrom','EstimatedDeliveryTo','DeliveryDate','LatestDeliveryDate','DeliveryDateRequested','PlannedShipmentDate'):
#             cand = _norm_iso_dt(raw.get(key))
#             if cand and str(raw.get(key)).strip().lower() != 'unknown':
#                 return cand, raw, False
#         return self.DUMMY_ETA_STR, raw, True

#     def apply_to_sale_order(self, order, payload: Dict[str, Any]):
#         eta_odoostr, eta_raw, eta_dummy = self._extract_eta_for_commitment(payload)

#         proposed = {
#             'commitment_date': eta_odoostr,
#         }

#         changes = {}
#         for k, v in proposed.items():
#             if k in order._fields and (order[k] or False) != (v or False):
#                 changes[k] = v

#         summary = json.dumps(
#             {
#                 'eta_chosen': eta_odoostr,
#                 'eta_used_dummy': bool(eta_dummy),
#                 'diff_keys': list(changes.keys()),
#             },
#             indent=2,
#             ensure_ascii=False,
#             default=str,
#         )

#         try:
#             order._create_monta_log(
#                 {
#                     'eta': {
#                         'chosen_for_commitment_date': eta_odoostr,
#                         'used_dummy_2099': bool(eta_dummy),
#                         'raw_fields': eta_raw,
#                     }
#                 },
#                 level='info',
#                 tag='Monta ETA',
#                 console_summary='[Monta ETA] decision saved',
#             )
#         except Exception:
#             pass

#         _logger.info("[Monta Pull] %s -> changed keys: %s", order.name, list(changes.keys()))
#         return changes, summary
