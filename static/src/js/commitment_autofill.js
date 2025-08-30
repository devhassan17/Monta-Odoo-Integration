/** Auto-fill Delivery Date input from the stored sale.order.commitment_date
 * Works on pages like /odoo/sales/<id> that already render the input element:
 *   <input id="commitment_date_0" data-field="commitment_date" ...>
 *
 * No template changes needed.
 */
(function () {
  function parseOrderIdFromUrl() {
    try {
      var parts = (window.location.pathname || "").split("/").filter(Boolean);
      // Expect ... /odoo/sales/<id>
      var last = parts[parts.length - 1];
      var id = parseInt(last, 10);
      return Number.isFinite(id) ? id : null;
    } catch (e) {
      return null;
    }
  }

  function formatOdooDatetime(dtStr) {
    // dtStr is UTC "YYYY-MM-DD HH:mm:ss" from Odoo read()
    // Target example: 30/08/2025 16:09:48
    if (!dtStr) return "";
    // Parse as UTC
    var m = dtStr.match(/^(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})$/);
    if (!m) return dtStr;
    var y = parseInt(m[1], 10),
        mo = parseInt(m[2], 10),
        d = parseInt(m[3], 10),
        hh = parseInt(m[4], 10),
        mm = parseInt(m[5], 10),
        ss = parseInt(m[6], 10);
    // Make a Date in UTC, then show as local time:
    var dt = new Date(Date.UTC(y, mo - 1, d, hh, mm, ss));
    var dd = String(dt.getDate()).padStart(2, "0");
    var MM = String(dt.getMonth() + 1).padStart(2, "0");
    var YYYY = dt.getFullYear();
    var HH = String(dt.getHours()).padStart(2, "0");
    var MMm = String(dt.getMinutes()).padStart(2, "0");
    var SS = String(dt.getSeconds()).padStart(2, "0");
    return dd + "/" + MM + "/" + YYYY + " " + HH + ":" + MMm + ":" + SS;
  }

  async function jsonRpc(model, method, args, kwargs) {
    // Minimal JSON-RPC to Odoo /web/dataset/call_kw (works when user is logged in)
    const payload = {
      jsonrpc: "2.0",
      method: "call",
      params: {
        model: model,
        method: method,
        args: args || [],
        kwargs: kwargs || {},
      },
      id: Date.now(),
    };
    const resp = await fetch("/web/dataset/call_kw", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      credentials: "same-origin",
    });
    const data = await resp.json();
    if (data.error) throw new Error(data.error.message || "RPC error");
    return data.result;
  }

  async function fillCommitmentDate() {
    try {
      var input = document.querySelector('input#commitment_date_0[data-field="commitment_date"]');
      if (!input) return; // field not present on this page

      // If it already has a value, do nothing
      if ((input.value || "").trim()) return;

      var orderId = parseOrderIdFromUrl();
      if (!orderId) return;

      // Read commitment_date from server (UTC string)
      const result = await jsonRpc("sale.order", "read", [[orderId], ["commitment_date"]], {});
      const rec = (result && result[0]) || {};
      const dtStr = rec.commitment_date; // e.g. "2099-01-01 00:00:00" or null

      if (!dtStr) return; // nothing to fill

      // Format for your visual requirement
      const pretty = formatOdooDatetime(dtStr);

      // Fill the input
      input.value = pretty;

      // If Odoo widget listens to input events, fire one:
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
    } catch (e) {
      // Silent fail—no UI break if RPC not available on this route
      console && console.debug && console.debug("[Monta ETA autofill] skipped:", e);
    }
  }

  // Run when DOM is ready (works on both backend & portal-like pages)
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", fillCommitmentDate);
  } else {
    fillCommitmentDate();
  }
})();
