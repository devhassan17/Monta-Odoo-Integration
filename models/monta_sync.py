# -*- coding: utf-8 -*-
import logging
from odoo import api, fields, models
from odoo.tools import float_utils

_logger = logging.getLogger(__name__)

class MontaOrderStatus(models.Model):
    _inherit = "monta.order.status"

    def _lower(self, s):
        return str(s or "").strip().lower()

    def _best_match(self, target, candidates):
        t = self._lower(target)
        vals = candidates if isinstance(candidates, list) else [candidates]
        best, best_sc = None, 0
        for r in vals:
            sc = 0
            for k in ("OrderNumber","Reference","ClientReference","WebshopOrderId",
                      "InternalWebshopOrderId","EorderGUID","EorderGuid"):
                v = self._lower((r or {}).get(k))
                if not v:
                    continue
                if v == t:
                    sc = 100
                elif v.startswith(t):
                    sc = max(sc, 85)
                elif t in v:
                    sc = max(sc, 70)
            if sc > best_sc:
                best_sc, best = sc, r
                if sc >= 100:
                    break
        return best if best_sc >= 60 else None

    def _monta_get_order(self, name: str):
        http = self.env["monta.http"].sudo()

        def _as_list(payload):
            if payload is None:
                return []
            if isinstance(payload, list):
                return payload
            if isinstance(payload, dict):
                for k in ("Items","items","Data","data","results","Results","value"):
                    if isinstance(payload.get(k), list):
                        return payload[k]
                return [payload]
            return []

        # NEW: direct exact endpoint
        direct = http.get_json(f"order/{name}")
        if isinstance(direct, dict) and direct:
            lst = _as_list(direct)
            if lst:
                match = self._best_match(name, lst)
                if match:
                    return match
            return direct

        # fallback queries
        for params in ({"orderNumber":name},{"reference":name},{"clientReference":name},
                       {"webshopOrderId":name},{"internalWebshopOrderId":name},
                       {"eorderGuid":name},{"search":name}):
            data = http.get_json("orders", params=params)
            lst = _as_list(data)
            if not lst:
                continue
            match = self._best_match(name, lst)
            if match:
                return match

        recent = http.get_json("orders", params={"limit":250,"sort":"desc"})
        match = self._best_match(name, _as_list(recent))
        return match or {}

    @api.model
    def _resolve_and_upsert(self, so):
        if not so or not so.name:
            return False
        data = self._monta_get_order(so.name)
        if not data:
            return False
        vals = {"order_name": so.name, "sale_order_id": so.id,
                "status": data.get("Status"), "monta_order_ref": data.get("OrderNumber")}
        rec = self.search([("order_name","=",so.name)], limit=1)
        if rec:
            rec.sudo().write(vals)
        else:
            rec = self.sudo().create(vals)
        return rec


# ---------------------------------------------------------------------------
# SaleOrder sync & picking auto-validate on Delivered
# ---------------------------------------------------------------------------
class SaleOrder(models.Model):
    _inherit = "sale.order"

    monta_status = fields.Char(string="Monta Status", copy=False, index=True)
    monta_status_code = fields.Char(string="Monta Status Code", copy=False)
    monta_status_source = fields.Selection(
        selection=[("shipments","Shipments"),("orderevents","Order Events"),("orders","Orders Header")],
        string="Monta Status Source", copy=False)
    monta_track_trace = fields.Char(string="Monta Track & Trace", copy=False)
    monta_last_sync = fields.Datetime(string="Monta Last Sync", copy=False)

    def _monta_candidate_reference(self):
        self.ensure_one()
        return self.name

    def action_monta_sync_status(self):
        _logger.info("[Monta] Manual sync for %d sales orders", len(self))
        self._monta_sync_batch()
        return True

    @api.model
    def cron_monta_sync_status(self, batch_limit=200):
        domain = [("state","in",["sale","done"]), ("monta_status","!=", "Shipped")]
        orders = self.search(domain, limit=batch_limit, order="write_date desc")
        _logger.info("[Monta] Cron sync starting for %d orders", len(orders))
        orders._monta_sync_batch()
        _logger.info("[Monta] Cron sync finished")
        return True

    def _monta_sync_batch(self):
        from ..services.monta_status_resolver import MontaStatusResolver
        Snapshot = self.env["monta.order.status"].sudo()

        try:
            resolver = MontaStatusResolver(self.env)
        except Exception as e:
            _logger.exception("[Monta] Resolver init failed (check System Parameters): %s", e)
            return

        # statuses/codes considered final-delivery states (adjust as needed)
        DELIVERED_NORMALIZED = ("delivered", "completed", "done", "delivered_to_customer", "delivered_ok")
        DELIVERED_CODES = ("DELIVERED", "DELIVERED_OK", "DELIVERED_CONFIRMED")

        for so in self:
            ref = so._monta_candidate_reference()
            if not ref:
                _logger.warning("[Monta] %s has no reference; skipping", so.display_name)
                continue

            try:
                status, meta = resolver.resolve(ref)
            except Exception as e:
                _logger.exception("[Monta] %s (%s) -> resolve() failed: %s", so.name, ref, e)
                continue

            # Not found -> mark as not available on Monta, upsert snapshot with reason
            if not status:
                try:
                    if "monta_on_monta" in so._fields:
                        so.write({"monta_on_monta": False})
                    Snapshot.upsert_for_order(
                        so,
                        order_status=False,
                        delivery_message=(meta or {}).get("reason"),
                        status_raw=(meta or {}).get("status_raw"),
                        last_sync=fields.Datetime.now(),
                    )
                except Exception:
                    _logger.exception("[Monta] Snapshot upsert failed for %s on not-found", so.name)
                _logger.warning("[Monta] %s (%s) -> no status returned (%s)", so.name, ref, meta)
                continue

            # Found -> write mirrors on sale.order (including Available on Monta)
            vals_so = {
                "monta_status": status,
                "monta_status_code": (meta or {}).get("status_code"),
                "monta_status_source": (meta or {}).get("source") or "orders",
                "monta_track_trace": (meta or {}).get("track_trace"),
                "monta_last_sync": fields.Datetime.now(),
            }

            # Optional mirrors if you’ve added them (safe checks)
            if "monta_order_ref" in so._fields:
                vals_so["monta_order_ref"] = (meta or {}).get("monta_order_ref")
            if "monta_delivery_message" in so._fields:
                vals_so["monta_delivery_message"] = (meta or {}).get("delivery_message")
            if "monta_delivery_date" in so._fields:
                vals_so["monta_delivery_date"] = (meta or {}).get("delivery_date")
            if "monta_status_raw" in so._fields:
                vals_so["monta_status_raw"] = (meta or {}).get("status_raw")

            # NEW: mirror Available on Monta (true if we have a stable Monta ref)
            if "monta_on_monta" in so._fields:
                vals_so["monta_on_monta"] = bool((meta or {}).get("monta_order_ref"))

            # Write to SO
            try:
                so.write(vals_so)
            except Exception as e:
                _logger.exception("[Monta] %s (%s) -> write failed: %s", so.name, ref, e)

            # Snapshot for history/audit
            try:
                Snapshot.upsert_for_order(
                    so,
                    monta_order_ref=(meta or {}).get("monta_order_ref") or so.name,
                    order_status=status,
                    delivery_message=(meta or {}).get("delivery_message"),
                    track_trace_url=(meta or {}).get("track_trace"),
                    delivery_date=(meta or {}).get("delivery_date"),
                    status_raw=(meta or {}).get("status_raw"),
                    last_sync=fields.Datetime.now(),
                )
            except Exception as e:
                _logger.exception("[Monta] Snapshot upsert failed for %s: %s", so.name, e)

            # ---------------------------
            # Auto-validate pickings on delivered
            # ---------------------------
            try:
                normalized_status = str(status or "").strip().lower()
                status_code = str((meta or {}).get("status_code") or "").strip()
                is_delivered = False

                if normalized_status in DELIVERED_NORMALIZED:
                    is_delivered = True
                elif status_code.upper() in DELIVERED_CODES:
                    is_delivered = True
                # allow explicit meta flag if resolver provides one
                elif (meta or {}).get("is_delivered") in (True, "true", "True", "1", 1):
                    is_delivered = True

                if not is_delivered:
                    # nothing to do for pickings
                    continue

                # find pickings linked to the sale.order
                Picking = self.env["stock.picking"].sudo()
                # search by sale_id or origin
                domain = ["|", ("sale_id", "=", so.id), ("origin", "=", so.name)]
                pickings = Picking.search(domain, order="id asc")

                if not pickings:
                    _logger.warning("[Monta] No pickings found for %s to mark delivered", so.name)
                    continue

                for pick in pickings:
                    try:
                        # Skip if already done or cancelled
                        if pick.state in ("done", "cancel"):
                            _logger.debug("[Monta] Skipping picking %s - state %s", pick.name, pick.state)
                            continue

                        # If there are move lines, ensure qty_done is populated
                        # For Odoo >= 14/15/16/18 the field is move_line_ids (stock.move.line)
                        if pick.move_line_ids:
                            for ml in pick.move_line_ids:
                                # set qty_done to product_uom_qty if it's zero or less than required
                                wanted = ml.product_uom_qty or 0.0
                                # rounding from the move line uom
                                rounding = ml.product_uom_id.rounding if ml.product_uom_id else ml.product_uom.rounding
                                if float_utils.float_compare(ml.qty_done or 0.0, wanted, precision_rounding=rounding) < 0:
                                    ml.qty_done = wanted
                        else:
                            # older style: no move_line_ids (rare in v18), fill move_lines.quantity_done
                            for mv in pick.move_lines:
                                wanted = mv.product_uom_qty or 0.0
                                rounding = mv.product_uom.rounding
                                if float_utils.float_compare(getattr(mv, "quantity_done", 0.0), wanted, precision_rounding=rounding) < 0:
                                    # try to set attribute if present
                                    try:
                                        mv.quantity_done = wanted
                                    except Exception:
                                        # fallback: create move_line entries is complex; warn and skip
                                        _logger.warning("[Monta] Unable to set quantity_done on move %s (picking %s)", mv.id, pick.name)

                        # Try to validate the picking. Use force_validate context to avoid blocking on small differences.
                        try:
                            pick.with_context(force_validate=True).button_validate()
                            _logger.info("[Monta] Auto-validated picking %s for sale %s", pick.name, so.name)
                        except Exception as e_val:
                            # Fallback: sometimes action_done() works for custom modules
                            try:
                                pick.action_done()
                                _logger.info("[Monta] action_done succeeded for picking %s (fallback)", pick.name)
                            except Exception as e2:
                                _logger.exception("[Monta] Failed to validate picking %s for sale %s: %s / %s", pick.name, so.name, e_val, e2)
                    except Exception as e_pick:
                        _logger.exception("[Monta] Exception while processing picking %s for sale %s: %s", pick.name if pick else "n/a", so.name, e_pick)

            except Exception as e:
                _logger.exception("[Monta] Exception while attempting to auto-validate pickings for %s: %s", so.name, e)

        # end for so
        return True


#