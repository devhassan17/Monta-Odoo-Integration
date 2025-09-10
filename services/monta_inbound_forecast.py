# -*- coding: utf-8 -*-
import json, pytz, requests, logging
from datetime import datetime, timedelta
from requests.auth import HTTPBasicAuth
from odoo import models, fields

_logger = logging.getLogger(__name__)

class MontaInboundForecastService(models.AbstractModel):
    _name = 'monta.inbound.forecast.service'
    _description = 'Create/Update Inbound Forecast in Monta (idempotent + line upserts)'

    # ---------- feature toggle ----------
    def _is_enabled(self) -> bool:
        """Global on/off switch via ICP `monta.inbound_enable` (default False)."""
        try:
            ICP = self.env['ir.config_parameter'].sudo()
            val = (ICP.get_param('monta.inbound_enable') or '').strip().lower()
            return val in ('1', 'true', 'yes', 'on')
        except Exception:
            return False

    # ---------- config ----------
    def _conf(self):
        ICP = self.env['ir.config_parameter'].sudo()
        base = (ICP.get_param('monta.base_url') or 'https://api-v6.monta.nl').rstrip('/')
        user = ICP.get_param('monta.username') or ''
        pwd  = ICP.get_param('monta.password') or ''
        tz   = ICP.get_param('monta.warehouse_tz') or 'Europe/Amsterdam'
        wh_display = (ICP.get_param('monta.inbound_warehouse_display_name') or '').strip()
        return base, user, pwd, tz, wh_display

    def _supplier_code_for(self, partner):
        ICP = self.env['ir.config_parameter'].sudo()
        override = (ICP.get_param('monta.supplier_code_override') or '').strip()
        if override:
            return override
        x = (getattr(partner, 'x_monta_supplier_code', '') or '').strip()
        if x:
            return x
        try:
            mp = { (k or '').strip().upper(): (v or '').strip()
                   for k, v in json.loads(ICP.get_param('monta.supplier_code_map') or "{}").items() }
        except Exception:
            mp = {}
        name_u = (partner.display_name or partner.name or '').strip().upper()
        ref_u  = (partner.ref or '').strip().upper()
        if name_u in mp and mp[name_u]:
            return mp[name_u]
        if ref_u and ref_u in mp and mp[ref_u]:
            return mp[ref_u]
        for attr in ('ref', 'vat'):
            v = (getattr(partner, attr, '') or '').strip()
            if v:
                return v
        return (ICP.get_param('monta.default_supplier_code') or '').strip()

    def _warehouse_display_name_for(self, po):
        wh_name = (getattr(po.picking_type_id.warehouse_id, 'x_monta_inbound_warehouse_name', '') or '').strip()
        if wh_name:
            return wh_name
        _base, _u, _p, _tz, wh_icp = self._conf()
        return wh_icp or None

    def _iso_with_tz(self, dt, tzname):
        tz = pytz.timezone(tzname)
        if not dt:
            dt = fields.Datetime.now()
        if dt.tzinfo is None:
            dt = tz.localize(dt)
        else:
            dt = dt.astimezone(tz)
        s = dt.strftime('%Y-%m-%dT%H:%M:%S%z')
        return s[:-2] + ':' + s[-2:]

    # ---------- HTTP + logging ----------
    def _http(self, po, method, url, payload=None, auth=None, headers=None, timeout=30):
        headers = headers or {"Accept": "application/json", "Content-Type": "application/json"}
        try:
            po._create_monta_log({'IF': {'step': f'HTTP {method}', 'url': url, 'payload': payload or {}}},
                                 'info', 'Monta IF', console_summary=f"[Monta IF] {method} {url}")
        except Exception:
            pass

        _logger.info("[Monta IF] %s %s", method, url)
        if payload is not None:
            try:
                _logger.info("[Monta IF] payload: %s", json.dumps(payload)[:1500])
            except Exception:
                pass

        r = requests.request(method=method, url=url, json=payload, auth=auth, headers=headers, timeout=timeout)
        try:
            body = r.json()
        except Exception:
            body = {'raw': (r.text or '')[:2000]}

        (_logger.info if r.ok else _logger.error)("[Monta IF] ← %s %s (%s)", method, url, r.status_code)
        try:
            po._create_monta_log({'IF': {'step': 'HTTP RESP', 'status': r.status_code, 'body': body}},
                                 'info' if r.ok else 'error', 'Monta IF', console_summary=f"[Monta IF] {r.status_code}")
        except Exception:
            pass
        return r.status_code, body

    # ---------- builders ----------
    def _collect_lines(self, po, line_dt_iso):
        rows = []
        for l in po.order_line:
            sku = (l.product_id.monta_sku or l.product_id.default_code or '').strip()
            if not sku:
                raise ValueError(f"Line '{l.display_name}' has no SKU (monta_sku/default_code).")
            q = int(l.product_qty or 0)
            if q > 0:
                rows.append({"Sku": sku, "Quantity": q, "DeliveryDate": line_dt_iso})
        if not rows:
            raise ValueError("PO has no positive-quantity lines.")
        return rows

    def _group_payload(self, po, tz):
        planned = po.date_planned or fields.Datetime.now()
        if planned < fields.Datetime.now() - timedelta(minutes=1):
            planned = fields.Datetime.now() + timedelta(days=1, hours=1)
        edd   = self._iso_with_tz(planned, tz)
        wh_dn = self._warehouse_display_name_for(po)
        payload = {
            "Reference": po.name,
            "SupplierCode": self._supplier_code_for(po.partner_id),
            "ExpectedDeliveryDate": edd,
            "AllocateStockOnDelivery": True,
            "WarehouseDisplayName": wh_dn,
            "Comment": (po.origin or "")[:200],
        }
        return payload, edd

    # ---------- helpers for idempotency ----------
    def _get_group(self, base, auth, po):
        url = f"{base}/inboundforecast/group/{po.name}"
        st, body = self._http(po, "GET", url, None, auth=auth)
        return st, body

    def _create_group_with_lines(self, base, auth, po, header, lines, edd):
        payload = header.copy()
        payload["InboundForecasts"] = lines
        url_post = f"{base}/inboundforecast/group"
        st, body = self._http(po, "POST", url_post, payload, auth=auth)
        return st, body

    def _put_header(self, base, auth, po, header):
        url_put = f"{base}/inboundforecast/group/{po.name}"
        st, body = self._http(po, "PUT", url_put, header, auth=auth)
        return st, body

    def _get_existing_skus(self, group_body):
        existing = set()
        try:
            for f in (group_body.get("InboundForecasts") or []):
                s = (f.get("Sku") or "").strip()
                if s:
                    existing.add(s)
                    if s.startswith('[') and s.endswith(']'):
                        existing.add(s[1:-1])
        except Exception:
            pass
        return existing

    def _upsert_lines(self, base, auth, po, edd, existing_skus):
        url_group = f"{base}/inboundforecast/group/{po.name}"
        for l in po.order_line:
            sku = (l.product_id.monta_sku or l.product_id.default_code or '').strip()
            if not sku:
                raise ValueError(f"Line '{l.display_name}' has no SKU.")
            qty = int(l.product_qty or 0)
            if qty <= 0:
                continue
            line_payload = {
                "Sku": sku,
                "Quantity": qty,
                "DeliveryDate": edd,
                "Reference": po.name,
                "Approved": False,
                "Comment": "",
            }
            if sku in existing_skus:
                self._http(po, "PUT", f"{url_group}/{sku}", line_payload, auth=auth)
            else:
                st, body = self._http(po, "POST", url_group, line_payload, auth=auth)
                if not (200 <= (st or 0) < 300):
                    txt = json.dumps(body).lower()
                    if "already" in txt or "exist" in txt:
                        self._http(po, "PUT", f"{url_group}/{sku}", line_payload, auth=auth)

    # ---------- public entry ----------
    def send_for_po(self, po):
        # FEATURE FLAG — hard stop when disabled
        if not self._is_enabled():
            _logger.info("[Monta IF] Disabled by ICP 'monta.inbound_enable' — skipping PO %s", po.name)
            try:
                po._create_monta_log({'IF': {'step': 'SKIP', 'reason': "monta.inbound_enable is false/empty"}},
                                     'info', 'Monta IF', console_summary='[Monta IF] disabled')
            except Exception:
                pass
            return False

        if po.state not in ('purchase', 'done'):
            _logger.info("[Monta IF] Skip PO %s (state=%s)", po.name, po.state)
            return False

        base, user, pwd, tz, _wh = self._conf()
        auth = HTTPBasicAuth(user, pwd)
        header, edd = self._group_payload(po, tz)

        sc = (header.get('SupplierCode') or '').strip()
        if not sc or len(sc) < 3:
            raise ValueError("SupplierCode is missing/invalid. Set vendor.x_monta_supplier_code "
                             "or configure ICP 'monta.supplier_code_map' / 'monta.supplier_code_override'.")

        st, body = self._get_group(base, auth, po)
        if st == 404:
            st2, body2 = self._create_group_with_lines(base, auth, po, header, self._collect_lines(po, edd), edd)
            if 200 <= (st2 or 0) < 300:
                uid = body2.get("UniqueId")
                if uid:
                    try:
                        po.write({'x_monta_inboundforecast_uid': uid})
                    except Exception:
                        pass
                _logger.info("[Monta IF] ✅ Created group for %s", po.name)
                return True
            body = body2
        elif 200 <= (st or 0) < 300:
            pass
        else:
            raise RuntimeError(f"GET group failed for {po.name}: HTTP {st} {body}")

        st3, body3 = self._put_header(base, auth, po, header)
        if not (200 <= (st3 or 0) < 300):
            raise RuntimeError(f"PUT header failed for {po.name}: HTTP {st3} {body3}")

        existing = self._get_existing_skus(body if st == 200 else body3 if isinstance(body3, dict) else {})
        self._upsert_lines(base, auth, po, edd, existing)

        _logger.info("[Monta IF] ✅ Header synced and lines upserted for %s", po.name)
        return True
