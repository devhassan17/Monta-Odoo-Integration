# -*- coding: utf-8 -*-
import re

def split_street(street, street2=''):
    """Split street + house number (Dutch style). Returns (street, number, suffix)."""
    full = (street or '') + ' ' + (street2 or '')
    full = full.strip()
    m = re.match(r'^(?P<street>.*?)[\s,]+(?P<number>\d+)(?P<suffix>\s*\w*)$', full)
    if m:
        return m.group('street').strip(), m.group('number').strip(), (m.group('suffix') or '').strip()
    return full, '', ''
