// /** Auto-fill Delivery Date input from the stored sale.order.commitment_date
//  * Works on pages like /odoo/sales/<id>.
//  * Adds clear console logs: Request, Date Get, Date is that, Added, Showing.
//  */
// (function () {
//   function parseOrderIdFromUrl() {
//     try {
//       var parts = (window.location.pathname || "").split("/").filter(Boolean);
//       var last = parts[parts.length - 1];
//       var id = parseInt(last, 10);
//       return Number.isFinite(id) ? id : null;
//     } catch (e) {
//       return null;
//     }
//   }

//   function formatOdooDatetime(dtStr) {
//     if (!dtStr) return "";
//     var m = dtStr.match(/^(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})$/);
//     if (!m) return dtStr;
//     var y = parseInt(m[1], 10),
//         mo = parseInt(m[2], 10),
//         d = parseInt(m[3], 10),
//         hh = parseInt(m[4], 10),
//         mm = parseInt(m[5], 10),
//         ss = parseInt(m[6], 10);
//     var dt = new Date(Date.UTC(y, mo - 1, d, hh, mm, ss));
//     var dd = String(dt.getDate()).padStart(2, "0");
//     var MM = String(dt.getMonth() + 1).padStart(2, "0");
//     var YYYY = dt.getFullYear();
//     var HH = String(dt.getHours()).padStart(2, "0");
//     var MMm = String(dt.getMinutes()).padStart(2, "0");
//     var SS = String(dt.getSeconds()).padStart(2, "0");
//     return dd + "/" + MM + "/" + YYYY + " " + HH + ":" + MMm + ":" + SS;
//   }

//   async function jsonRpc(model, method, args, kwargs) {
//     const payload = {
//       jsonrpc: "2.0",
//       method: "call",
//       params: { model, method, args: args || [], kwargs: kwargs || {} },
//       id: Date.now(),
//     };
//     console.info("[EDD UI] Request Sent To Monta/Server → read commitment_date");
//     const resp = await fetch("/web/dataset/call_kw", {
//       method: "POST",
//       headers: { "Content-Type": "application/json" },
//       body: JSON.stringify(payload),
//       credentials: "same-origin",
//     });
//     const data = await resp.json();
//     if (data.error) throw new Error(data.error.message || "RPC error");
//     return data.result;
//   }

//   async function fillCommitmentDate() {
//     try {
//       var input = document.querySelector('input#commitment_date_0[data-field="commitment_date"]');
//       if (!input) {
//         console.info("[EDD UI] Input not found on this page; nothing to show.");
//         return;
//       }
//       if ((input.value || "").trim()) {
//         console.info("[EDD UI] Input already filled by server; showing:", input.value);
//         return;
//       }

//       var orderId = parseOrderIdFromUrl();
//       if (!orderId) {
//         console.info("[EDD UI] Cannot parse order id from URL.");
//         return;
//       }

//       const result = await jsonRpc("sale.order", "read", [[orderId], ["commitment_date"]], {});
//       const rec = (result && result[0]) || {};
//       const dtStr = rec.commitment_date;
//       console.info("[EDD UI] Date Get:", dtStr || "(none)");
//       if (!dtStr) return;

//       const pretty = formatOdooDatetime(dtStr);
//       console.info("[EDD UI] Date is that:", dtStr, "| pretty:", pretty);

//       input.value = pretty;
//       input.dispatchEvent(new Event("input", { bubbles: true }));
//       input.dispatchEvent(new Event("change", { bubbles: true }));
//       console.info("[EDD UI] Date is added to Commitment date (input).");
//       console.info("[EDD UI] Date is showing.");
//     } catch (e) {
//       console.info("[EDD UI] Skipped:", e && e.message ? e.message : e);
//     }
//   }

//   if (document.readyState === "loading") {
//     document.addEventListener("DOMContentLoaded", fillCommitmentDate);
//   } else {
//     fillCommitmentDate();
//   }
// })();


