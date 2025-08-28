# -*- coding: utf-8 -*-
import json, logging
from typing import Dict, Any, List, Tuple
_logger = logging.getLogger(__name__)

class MontaInboundBatches:
    """
    Extract batch/lot + expiry for shipped items from a Monta GET /order payload
    and store them into monta.order.batch.trace records.
    Tries multiple shapes: Shipments[].Lines[], Lines[], OrderLines[], etc.
    """
    def __init__(self, env):
        self.env = env

    def _iter_lines(self, payload: Dict[str, Any]):
        candidates = []
        for key in ('Shipments', 'ShipmentList', 'Lines', 'OrderLines', 'Items'):
            v = payload.get(key)
            if isinstance(v, list):
                candidates.append((key, v))
        # flatten shipments->lines
        for key, arr in candidates:
            for item in arr:
                if isinstance(item, dict) and any(k in item for k in ('Lines','OrderLines','Items')):
                    for subk in ('Lines','OrderLines','Items'):
                        sub = item.get(subk)
                        if isinstance(sub, list):
                            for line in sub:
                                yield line, item  # line, parent shipment
                else:
                    yield item, None

    def _line_fields(self, line: Dict[str, Any]):
        # Try common keys; adjust if your tenant differs
        return {
            'sku': line.get('Sku') or line.get('SKU') or line.get('ProductCode'),
            'qty': line.get('ShippedQuantity') or line.get('Quantity') or line.get('Qty'),
            'batch': line.get('BatchNumber') or line.get('Lot') or line.get('Batch'),
            'expiry': line.get('ExpirationDate') or line.get('ExpiryDate') or line.get('BestBeforeDate'),
        }

    def sync_for_order(self, order, payload: Dict[str, Any]):
        Model = self.env['monta.order.batch.trace']
        created = 0
        # (optional) wipe prior rows to keep latest truth
        old = Model.search([('sale_order_id', '=', order.id)])
        old.unlink()
        for line, parent in self._iter_lines(payload):
            f = self._line_fields(line)
            if not (f['sku'] and f['batch']):
                continue
            sol = order.order_line.filtered(lambda l: (l.product_id.default_code == f['sku']) or
                                                     (getattr(l.product_id, 'monta_sku', False) == f['sku']))
            vals = {
                'sale_order_id': order.id,
                'sale_order_line_id': sol[:1].id if sol else False,
                'product_id': sol[:1].product_id.id if sol else False,
                'sku': f['sku'],
                'batch_number': f['batch'],
                'expiry_date': f['expiry'] and f['expiry'][:10],
                'qty': float(f['qty'] or 0.0),
                'carrier': (parent or {}).get('CarrierName') or (parent or {}).get('ShipperDescription'),
                'tracking_number': (parent or {}).get('TrackingNumber') or (parent or {}).get('TrackAndTraceCode'),
                'raw_payload': json.dumps(line, ensure_ascii=False)[:2000],
            }
            Model.create(vals)
            created += 1
        order.message_post(body=f"Monta batches synced: {created} row(s).")
        return created
