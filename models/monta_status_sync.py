# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    _inherit = "sale.order"

    monta_status = fields.Char(string="Monta Status", copy=False, index=True)
    monta_status_code = fields.Char(string="Monta Status Code", copy=False)
    monta_status_source = fields.Selection(
        selection=[("shipments", "Shipments"), ("orderevents", "Order Events"), ("orders", "Orders Header")],
        string="Monta Status Source",
        copy=False,
    )
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
        domain = [("state", "in", ["sale", "done"]), ("monta_status", "!=", "Shipped")]
        orders = self.search(domain, limit=batch_limit, order="write_date desc")
        _logger.info("[Monta] Cron sync starting for %d orders", len(orders))
        orders._monta_sync_batch()
        _logger.info("[Monta] Cron sync finished")
        return True

    def _monta_sync_batch(self):
        from ..services.monta_status_resolver import MontaStatusResolver

        Snapshot = self.env["monta.order.status"].sudo()

        # Cache resolvers per company (so we don't init one per order)
        resolver_by_company = {}

        for so in self:
            ref = so._monta_candidate_reference()
            if not ref:
                _logger.warning("[Monta] %s has no reference; skipping", so.display_name)
                continue

            company = so.company_id or self.env.company
            resolver = resolver_by_company.get(company.id)
            if not resolver:
                try:
                    resolver = MontaStatusResolver(self.env, company=company)
                    resolver_by_company[company.id] = resolver
                except Exception as e:
                    _logger.exception(
                        "[Monta] Resolver init failed for company %s (%s): %s",
                        company.display_name,
                        company.id,
                        e,
                    )
                    try:
                        if "monta_on_monta" in so._fields:
                            so.write({"monta_on_monta": False})
                    except Exception:
                        pass
                    continue

            try:
                status, meta = resolver.resolve(ref)
            except Exception as e:
                _logger.exception("[Monta] %s (%s) -> resolve() failed: %s", so.name, ref, e)
                continue

            meta = meta or {}
            now = fields.Datetime.now()

            # Not found -> mark as not available on Monta, upsert snapshot with reason
            if not status:
                try:
                    if "monta_on_monta" in so._fields:
                        so.write({"monta_on_monta": False})
                    Snapshot.upsert_for_order(
                        so,
                        order_status=False,
                        delivery_message=meta.get("reason"),
                        status_raw=meta.get("status_raw"),
                        last_sync=now,
                    )
                except Exception:
                    _logger.exception("[Monta] Snapshot upsert failed for %s on not-found", so.name)

                _logger.warning("[Monta] %s (%s) -> no status returned (%s)", so.name, ref, meta)
                continue

            vals_so = {
                "monta_status": status,
                "monta_status_code": meta.get("status_code"),
                "monta_status_source": meta.get("source") or "orders",
                "monta_track_trace": meta.get("track_trace"),
                "monta_last_sync": now,
            }

            # Optional mirrors if present
            if "monta_order_ref" in so._fields:
                vals_so["monta_order_ref"] = meta.get("monta_order_ref")
            if "monta_delivery_message" in so._fields:
                vals_so["monta_delivery_message"] = meta.get("delivery_message")
            if "monta_delivery_date" in so._fields:
                vals_so["monta_delivery_date"] = meta.get("delivery_date")
            if "monta_status_raw" in so._fields:
                vals_so["monta_status_raw"] = meta.get("status_raw")

            # Mirror Available on Monta (true if we have a stable Monta ref)
            if "monta_on_monta" in so._fields:
                vals_so["monta_on_monta"] = bool(meta.get("monta_order_ref"))

            try:
                so.write(vals_so)
            except Exception as e:
                _logger.exception("[Monta] %s (%s) -> write failed: %s", so.name, ref, e)

            # Snapshot for history/audit
            try:
                Snapshot.upsert_for_order(
                    so,
                    monta_order_ref=meta.get("monta_order_ref") or so.name,
                    order_status=status,
                    delivery_message=meta.get("delivery_message"),
                    track_trace_url=meta.get("track_trace"),
                    delivery_date=meta.get("delivery_date"),
                    status_raw=meta.get("status_raw"),
                    last_sync=now,
                )
            except Exception as e:
                _logger.exception("[Monta] Snapshot upsert failed for %s: %s", so.name, e)
