# -*- coding: utf-8 -*-
import re


class MontaStatusNormalizer:
    """
    Normalize many Monta tenant-specific status strings/codes
    into a compact, predictable set.
    """

    RAW_MAP = {
        'processing': {'processing', 'in progress', 'verified', 'queued', 'open'},
        'received':   {'received', 'inbound received'},
        'picked':     {'picked', 'picking done'},
        'shipped':    {'shipped', 'sent', 'despatched', 'dispatch'},
        'delivered':  {'delivered', 'complete', 'completed'},
        'backorder':  {'backorder', 'bo', 'awaiting stock'},
        'cancelled':  {'cancelled', 'canceled'},
        'error':      {'error', 'failed', 'rejected'},
    }

    # Pre-normalize buckets once (lowercase + stripped + alpha only)
    MAP = {
        key: {re.sub(r'[^a-z]+', ' ', v.lower()).strip() for v in values}
        for key, values in RAW_MAP.items()
    }

    @classmethod
    def _clean(cls, value: str) -> str:
        value = str(value).strip().lower()
        value = re.sub(r'[^a-z]+', ' ', value)
        value = re.sub(r'\s+', ' ', value)
        return value.strip()

    @classmethod
    def normalize(cls, raw: str) -> str:
        if not raw:
            return 'unknown'

        s = cls._clean(raw)

        # 1) Exact match
        for key, bucket in cls.MAP.items():
            if s in bucket:
                return key

        # 2) Fuzzy contains
        for key, bucket in cls.MAP.items():
            if any(token in s for token in bucket):
                return key

        return 'unknown'
