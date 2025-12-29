# -*- coding: utf-8 -*-
import re

class MontaStatusNormalizer:
    """
    Map many Monta tenant-specific status strings/codes to a compact selection.
    """
    MAP = {
        'processing': {'processing','in progress','verified','queued','open'},
        'received':   {'received','inbound received'},
        'picked':     {'picked','picking done'},
        'shipped':    {'shipped','sent','despatched','dispatch'},
        'delivered':  {'delivered','complete','completed'},
        'backorder':  {'backorder','bo','awaiting stock'},
        'cancelled':  {'cancelled','canceled'},
        'error':      {'error','failed','rejected'},
    }

    @classmethod
    def normalize(cls, raw: str):
        if not raw:
            return 'unknown'
        s = str(raw).strip().lower()
        s = re.sub(r'[^a-z]+', ' ', s).strip()
        for key, bucket in cls.MAP.items():
            if s in bucket:
                return key
        # fuzzy contains
        for key, bucket in cls.MAP.items():
            if any(tok in s for tok in bucket):
                return key
        return 'unknown'
