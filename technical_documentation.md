# Monta-Odoo WMS Integration Module: Complete Technical Documentation
**Version:** 1.1.29  
**Author:** Managemyweb.co  
**License:** LGPL-3  
**Compatibility:** Odoo 18 (Enterprise & Community editions)

---

## 1. Architectural Overview & Workflow

The **Monta-Odoo Integration** module provides high-performance, real-time, and scheduled two-way synchronization between Odoo 18 and the **Monta WMS (Warehouse Management System)** API (version 6). 

The module has three primary pillars:
1. **Sales Order Fulfillment (Outbound)**: Integrates standard Sales Orders and automated **Subscription Renewals** by pushing stock pickings (deliveries) to Monta.
2. **Vendor Inbound Forecasts (Inbound)**: Pushes Purchase Orders and incoming delivery forecasts to Monta so WMS is prepared to receive supplier shipments.
3. **Status and Expected Delivery Date (EDD/ETA) Updates**: Queries Monta regularly via cron jobs to pull fulfillment progress, track-and-trace links, actual delivery dates, and updates the Odoo picking/sales orders accordingly.

```mermaid
graph TD
    %% Sales Flow
    subgraph Outbound Flow (Sales & Subscriptions)
        A[Odoo Sales Order confirmed] --> B{Subscription renewal?}
        B -- Yes --> C[Subscription Sync Cron checks gaps/invoice counts]
        C --> D[Create Outgoing Stock Picking with SO...-PICK suffix]
        B -- No --> E[Standard Outgoing Stock Picking created]
        D --> F[picking.action_confirm]
        E --> F
        F --> G{Push Eligible? <br> Route Filter <br> Mollie Mandate <br> Ancient order guard}
        G -- Yes --> H[Bypass Lot Tracking]
        H --> I[Expand Pack/BoM variants recursively]
        I --> J[Post Payload to Monta WMS /order]
        J --> K[Odoo Picking auto-validated immediately]
    end

    %% Inbound Flow
    subgraph Inbound Flow (Purchases)
        L[Odoo Purchase Order confirmed] --> M{Inbound enabled in Config?}
        M -- Yes --> N[Query Monta WMS for PO Group /inboundforecast/group/PO_NAME]
        N -- Not Found --> O[POST PO details & component lines as new Inbound Forecast]
        N -- Exists --> P[PUT update existing Inbound Forecast group header]
    end

    %% Status Updates
    subgraph Status & EDD Feedback Sync
        Q[Sync Status Cron runs half-hourly] --> R[Sync non-shipped Sales Orders & pushed Outgoing Pickings]
        R --> S[Query Monta API /order/WEBSHOP_ORDER_ID]
        S --> T[Freshest Wins check: Shipments -> OrderEvents -> Orders Header]
        T --> U{Authoritative overrides? <br> Blocked > Backorder > Others}
        U --> V[Update Odoo Sales Order/Picking fields <br> write monta_status, monta_track_trace]
        V --> W[Upsert monta.order.status history snapshot]
        V --> X[Auto-validate related Odoo Pickings if Shipped]
    end
```

---

## 2. Directory and File Map

Below is a complete description of what every single file in the integration module does:

### 2.1. Root Files

*   **[`__init__.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/__init__.py)**: 
    Root Python initializer that exposes Python sub-packages (`models`, `services`, `utils`) and references installation `hooks`.
*   **[`__manifest__.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/__manifest__.py)**: 
    The Odoo module manifest declaring metadata, version (`1.1.29`), standard dependencies (`sale_management`, `account`, `portal`, `mrp`, `purchase`, `sale_subscription`, `stock`), post-init and uninstall hooks, price/currency, and references all XML files loaded upon installation.
*   **[`hooks.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/hooks.py)**: 
    *   `post_init_hook(env)`: Creates the status-sync cron job programmatically and migrates legacy Odoo system parameters (`ir.config_parameter`) into the new unified `monta.config` singleton model.
    *   `uninstall_hook(env)`: Gracefully uninstalls and deletes registered crons on module removal.

---

### 2.2. Python Models (`models/` directory)

All files under `models/` inherit and extend standard Odoo database tables or register custom tables:

1.  **[`models/__init__.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/models/__init__.py)**: 
    Python loader that registers all custom and inherited models in alphabetical dependency order.
2.  **[`models/monta_config.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/models/monta_config.py)**: 
    Defines `monta.config`, a **Singleton** model (only one active configuration row in the database) containing API credentials, base URLs, timeouts, channel fields, global sync toggle, allowed companies, warehouse timezones, and delivery filters.
3.  **[`models/account_move.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/models/account_move.py)**: 
    Contains deprecated fields (`monta_renewal_pushed`, etc.) kept to avoid database migration and view crashes. Outgoing delivery pushing is completely decoupled from Odoo invoice creation, preventing unwanted double-delivery triggers on invoice date updates.
4.  **[`models/monta_order_status.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/models/monta_order_status.py)**: 
    Represents `monta.order.status`, which acts as an audit trail snapshot of order synchronization states. It hashes base url and user credentials into `monta_account_key` to avoid overlaps on credential changes, supports normal sales vs subscription renewals (`order_kind`), and allows manual resends from the dashboard.
5.  **[`models/monta_order_status_upsert.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/models/monta_order_status_upsert.py)**: 
    Extends `monta.order.status` to normalize payload values (such as mapping `status_raw` dictionary structures into standard DB fields) and ensures safe selection constraints.
6.  **[`models/monta_sale_log.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/models/monta_sale_log.py)**: 
    Defines `monta.sale.log` which saves raw JSON formatted request and response payloads, providing a complete debugging journal linked to each Odoo Sales Order.
7.  **[`models/monta_status_sync.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/models/monta_status_sync.py)**: 
    Implements the core order status synchronization scheduled engine:
    *   `cron_monta_sync_status()`: Scans non-delivered Sales Orders and active pushed outgoing Pickings with a **60-day cutoff**.
    *   Invokes `MontaStatusResolver` per company to query Monta APIs, records track-and-trace links and delivery dates, and auto-validates stock pickings in Odoo when WMS reports them as "Shipped".
8.  **[`models/monta_sync.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/models/monta_sync.py)**: 
    Contains historical base synchronization methods and helper algorithms:
    *   `_best_match(target, candidates)`: A soft fuzzy-matching algorithm to map Odoo orders with WMS transaction identifiers.
    *   `_monta_get_order(name)`: A highly resilient order lookup mechanism that queries `/order/{name}` first, followed by fallbacks to general searches on various reference fields.
9.  **[`models/monta_subscription_sync.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/models/monta_subscription_sync.py)**: 
    Houses the hourly cron `_cron_monta_subscription_delivery_sync()` that detects and manages **Subscription Renewals**:
    *   Compiles confirmed subscription orders across allowed companies.
    *   Compares the number of posted Odoo invoices vs Monta-pushed pickings.
    *   Checks guards: ignores historical backlogs (latest invoice must be younger than 7 days, ignores period 1 checkouts, requires a **valid Mollie Mandate** status of `'valid'`).
    *   Generates a fresh outgoing stock picking copying standard SO lines and auto-triggers its push to Monta.
10. **[`models/product_product.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/models/product_product.py)**: 
    Adds `monta_sku` to Odoo variants. If a product's SKU, default reference, barcode, or supplier is edited in Odoo, it automatically flags related confirmed sales orders with `monta_needs_sync = True` to guarantee fresh synchronization.
11. **[`models/product_template.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/models/product_template.py)**: 
    Adds `action_monta_log_pack_variant_skus()`, a helper tool that outputs colorized JSON breakdowns of pack component resolutions and SKUs directly in developer terminals.
12. **[`models/purchase_order.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/models/purchase_order.py)**: 
    Extends Odoo purchases to handle **Inbound Forecasts**:
    *   Pushes inbound forecasts automatically on PO confirmation (`button_confirm()`).
    *   Triggers PO line updates dynamically on confirmed purchase edits (`write()`).
    *   Sends deletion updates to Monta WMS on PO cancel (`button_cancel()`) or complete deletion (`unlink()`).
13. **[`models/purchase_order_line.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/models/purchase_order_line.py)**: 
    Watches purchase order line edits (products, quantities, dates) and forces parents to resynchronize with Monta.
14. **[`models/res_partner_ext.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/models/res_partner_ext.py)**: 
    Adds `x_monta_supplier_code` to vendor contacts to define exact supplier codes expected by Monta.
15. **[`models/sale_order.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/models/sale_order.py)**: 
    *   Defines synchronization flags (`monta_sync_state`, `monta_needs_sync`, etc.).
    *   `_prepare_monta_order_payload()`: Formulates the complete customer delivery address, contact coordinates, invoice lines, and taxes in Monta-compliant formats.
    *   Intercepts order confirmation and edits to mark sync status, and requests order cancellation in Monta WMS if Odoo sales orders are aborted.
    *   `_action_send_to_monta()`: Intercepts the push trigger and delegates it to eligible outgoing pickings.
16. **[`models/sale_order_inbound.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/models/sale_order_inbound.py)**: 
    Implements `action_monta_pull_now()` which calls the Monta GET API, extracts Estimated Delivery Dates (EDD/ETA) using custom prioritizations, updates Odoo's `commitment_date` field, and logs step progressions for user feedback.
17. **[`models/sale_order_line.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/models/sale_order_line.py)**: 
    Watches changes on sales lines (product, quantity, taxes, price) and marks the parent order for update.
18. **[`models/sale_order_monta_actions.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/models/sale_order_monta_actions.py)**: 
    Adds Odoo Form helper to navigate directly from standard Sales Orders to the filtered Monta Status logs.
19. **[`models/sale_order_monta_fields.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/models/sale_order_monta_fields.py)**: 
    Declares mirror fields (delivery dates, status messages, raw JSON trackers) directly visible on Odoo sales forms.
20. **[`models/sku_test_log.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/models/sku_test_log.py)**: 
    Declares `sku_test.log` table to help audit extracted variant SKUs at order confirmation.
21. **[`models/stock_picking.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/models/stock_picking.py)**: 
    Coordinates exact stock actions:
    *   `_is_monta_push_eligible()`: Validates if picking is outgoing, confirmed, within allowed routes, matches subscription Mollie mandates, and is not an ancient subscription backlog.
    *   `_monta_make_webshop_order_id()`: Generates unique transaction identifiers for WMS. First delivery uses the original `SO.name`; subsequent subscription renewals use `SO_NAME-PICK{picking_id}`.
    *   `_monta_ensure_untracked_products()`: Automatically bypasses serial/lot tracking by setting product tracking to `'none'` dynamically, ensuring automated fulfillment never gets stuck in Odoo.
    *   `action_push_to_monta()`: Compiles lines, posts payload to `/order`, sets logs, and immediately triggers Odoo delivery validation.
    *   `action_cancel()`: Deletes/cancels active deliveries directly in Monta WMS.
22. **[`models/stock_warehouse_ext.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/models/stock_warehouse_ext.py)**: 
    Adds `x_monta_inbound_warehouse_name` to stock warehouses to define separate targets on Monta's side.

---

### 2.3. Services (`services/` directory)

Services act as independent business logic layers separated from Odoo model overrides:

1.  **[`services/__init__.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/services/__init__.py)**: 
    Registers service package classes.
2.  **[`services/monta_client.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/services/monta_client.py)**: 
    Low-level client wrapping connection protocols. Handles authorization, dynamic timeouts, logs raw payloads in standard logger, and registers chronological logs under `monta.sale.log`.
3.  **[`services/monta_http.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/services/monta_http.py)**: 
    An `AbstractModel` (`monta.http`) used for read-only GET queries on status trackers and fuzzy checkups.
4.  **[`services/monta_inbound_forecast.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/services/monta_inbound_forecast.py)**: 
    Orchestrates Purchase Order Inbound Forecast logic (`monta.inbound.forecast.service`):
    *   Resolves target supplier codes and target warehouses dynamically.
    *   Fuzzy checks existing forecasts via `GET /inboundforecast/group/{po.name}`.
    *   Recursively explodes pack lines to prepare individual component lists.
    *   Initiates `POST` requests for new forecasts or `PUT` requests for edits.
5.  **[`services/monta_status_normalizer.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/services/monta_status_normalizer.py)**: 
    Pre-compiles complex raw WMS statuses into predictable, standardized buckets: `processing`, `received`, `picked`, `shipped`, `delivered`, `backorder`, `cancelled`, `error`.
6.  **[`services/monta_status_resolver.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/services/monta_status_resolver.py)**: 
    Processes chronological feedback loops. Executes queries across three tiers—**Shipments** (1st priority), **Order Events** (2nd priority), and **Order Header** (fallback). Combines the results and enforces authoritative overrides: **Blocked** status blocks everything; **Backorder** status suspends intermediate processing.

---

### 2.4. Utilities (`utils/` directory)

Static helper modules that handle data parsing:

1.  **[`utils/__init__.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/utils/__init__.py)**: 
    Exposes utility functions.
2.  **[`utils/address.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/utils/address.py)**: 
    Regular expression module that splits address lines (like "Mainstreet 42-A") into `street`, `HouseNumber` ("42"), and `HouseNumberAddition` ("A") for Monta compatibility.
3.  **[`utils/eta.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/utils/eta.py)**: 
    Scrapes Estimated Delivery Dates (EDD) from raw payloads by parsing standard date-time patterns.
4.  **[`utils/pack.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/utils/pack.py)**: 
    An elegant, robust recursive kit expansion system:
    *   Supports native Odoo **Phantom Bill of Materials (BoM)**.
    *   Supports OCA open-source `product_pack` schemas.
    *   Recursively flattens nested packs down to leaf components up to **8 levels deep** to protect against infinite circular loops.
5.  **[`utils/sku.py`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/utils/sku.py)**: 
    Strict SKU resolver. Pulls identifier in prioritized sequence: 
    `monta_sku` $\rightarrow$ `default_code` $\rightarrow$ Supplier Code $\rightarrow$ `barcode` $\rightarrow$ Template `default_code`. Raises a validation warning if blank, protecting against payload errors.

---

### 2.5. Views (`views/` & `data/` directories)

1.  **[`views/monta_config_views.xml`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/views/monta_config_views.xml)**: 
    Declares the Singleton Form View and the Server Action required to open it.
2.  **[`views/monta_menu.xml`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/views/monta_menu.xml)**: 
    Defines the root navigation menu labeled **"Monta"** on Odoo's top application bar.
3.  **[`views/monta_order_status_views.xml`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/views/monta_order_status_views.xml)**: 
    Declares form, list (tree), and search dashboards for the `monta.order.status` history snapshots, enabling filters by status, kinds (renewal vs sale), and manual pushing.
4.  **[`views/portal_my_orders_tnt.xml`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/views/portal_my_orders_tnt.xml)**: 
    An empty placeholder XML file.
5.  **[`views/sale_order_monta_sync_button.xml`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/views/sale_order_monta_sync_button.xml)**: 
    Inherits standard Sales Order form views to add a manual **"Sync Monta"** header button and a stat-button displaying WMS shipment details.
6.  **[`views/stock_picking_views.xml`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/views/stock_picking_views.xml)**: 
    Adds a **"Push to Monta"** action button in standard outgoing pickings, alongside a dedicated **"Monta Integration"** information block.
7.  **[`views/subscription_monta_status.xml`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/views/subscription_monta_status.xml)**: 
    Safely overrides standard Odoo subscription views to show plain-text status mirrors instead of restrictive selection fields, avoiding UI crashes.
8.  **[`data/monta_subscription_sync_cron.xml`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/data/monta_subscription_sync_cron.xml)**: 
    Registers the scheduled cron `ir_cron_monta_subscription_delivery_sync` to automatically scan and synchronize subscription deliveries hourly.

---

### 2.6. Security (`security/` directory)

1.  **[`security/ir.model.access.csv`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/security/ir.model.access.csv)**: 
    Defines model permissions: System Admins get full CRUD privileges across configs, order statuses, and logs, whereas standard Sales Users (`group_user`) get read-only rights.
2.  **[`security/monta_order_status_rules.xml`](file:///Users/alihassan/Documents/Github/Monta-Odoo-Integration/security/monta_order_status_rules.xml)**: 
    Adds global record rules that permit all Odoo users to read the synced order status logs.

---

## 3. Notable Guards, Safety Filters & Edge Cases

The module includes several enterprise guards that make it highly reliable in both staging and production environments:

*   **Allowed Odoo Base URLs Guard**: 
    Prevents staging or testing Odoo servers from pushing test orders to your production Monta WMS. If `allowed_base_urls` is set in configuration, Odoo checks it against Odoo's internal base parameter and silently blocks any outbound request if they don't match.
*   **Ancient Subscription Base Delivery Guard**: 
    In testing/staging environments, old subscription orders can pile up. The module ensures that if a subscription is older than **30 days**, Odoo blocks WMS from attempting to push its base delivery, preventing unwanted historical fulfillment storms.
*   **Duplicate Sent Guard**: 
    If Monta reports that a delivery ID "already exists", the module gracefully captures it and marks it as "Sent" in Odoo instead of crashing or retrying, ensuring the pipeline never gets blocked by duplicates.
*   **Automatic Untracking (No Lot Blocks)**: 
    If a product in a delivery has Serial/Lot tracking enabled in Odoo, Odoo will block automated validation. The connector automatically turns tracking off (`tracking = 'none'`) during the push process, ensuring that fulfillment processes run seamlessly in Odoo.
