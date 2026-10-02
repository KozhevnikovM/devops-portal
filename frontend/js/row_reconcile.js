/*
 * row_reconcile.js — page row reconciliation for the list sections (#497).
 *
 * Each list section holds one hidden poller (`<div data-reconcile-rows=…>`, see
 * partials/reconcile_poller.html) that htmx fires every 60 s with `hx-sync="this:drop"`, so a
 * section never has two requests in flight. This script decides what each request names and keeps
 * obsolete answers from being applied:
 *
 * - htmx:configRequest — picks a bounded batch of the displayed live rows (`tr[data-live]`):
 *   in-flight rows first, rotating, with `min(settled, settledMin)` slots kept for settled
 *   (READY/FAILED) rows, rotating too; sends each as `r=<id>.<version>`, plus `newest=` from the
 *   first displayed row's list key (any status — a RELEASED row still marks what is displayed).
 * - htmx:beforeSwap — drops the whole response when its poller is no longer in the document (the
 *   section was replaced by a filter change while the request was in flight). htmx fires it on the
 *   poller, and a detached poller's events never reach `document`, so this listener (and the
 *   after-swap one) is bound to each poller itself.
 * - htmx:oobBeforeSwap — skips a returned row whose version changed after the request was sent
 *   (a live update or an action got there first), so older data never overwrites newer.
 *
 * The server still enforces the batch cap and re-authorizes every id; this script only chooses.
 */
(function (root) {
    "use strict";

    // Rotating take of up to `count` items from `items`, starting at `offset`.
    function rotate(items, count, offset) {
        var n = items.length;
        if (n === 0 || count <= 0) return { picked: [], offset: 0 };
        var start = offset % n;
        var take = Math.min(count, n);
        var picked = [];
        for (var k = 0; k < take; k++) picked.push(items[(start + k) % n]);
        return { picked: picked, offset: (start + take) % n };
    }

    /*
     * The batch for one request: in-flight rows first, then settled rows, with
     * min(settled.length, settledMin) slots reserved for settled rows. Pure — the convergence
     * tests mirror it (tests/reconcile_oracle.py).
     */
    function selectBatch(inflight, settled, max, settledMin, offsets) {
        var reserve = Math.min(settled.length, settledMin);
        var live = rotate(inflight, Math.min(inflight.length, max - reserve), offsets.inflight || 0);
        var calm = rotate(settled, Math.min(settled.length, max - live.picked.length), offsets.settled || 0);
        return {
            batch: live.picked.concat(calm.picked),
            offsets: { inflight: live.offset, settled: calm.offset },
        };
    }

    root.rowReconcile = { selectBatch: selectBatch };

    if (typeof document === "undefined") return;   // loaded by the node tests

    var state = new WeakMap();   // poller → { offsets, sent }
    var bound = new WeakSet();   // pollers whose own swap listeners are attached
    var applying = null;         // the sent versions of the response being swapped right now

    function isPoller(elt) {
        return elt && elt.hasAttribute && elt.hasAttribute("data-reconcile-rows");
    }

    function onBeforeSwap(evt) {
        var poller = evt.currentTarget;
        if (!document.body.contains(poller)) {
            evt.detail.shouldSwap = false;   // the section it reconciled is gone
            return;
        }
        applying = (state.get(poller) || {}).sent || {};
    }

    function onDone() {
        applying = null;
    }

    function bind(poller) {
        if (bound.has(poller)) return;
        bound.add(poller);
        poller.addEventListener("htmx:beforeSwap", onBeforeSwap);
        poller.addEventListener("htmx:afterSwap", onDone);
        poller.addEventListener("htmx:afterRequest", onDone);
    }

    document.addEventListener("htmx:configRequest", function (evt) {
        var poller = evt.detail.elt;
        if (!isPoller(poller)) return;
        bind(poller);
        var tbody = document.querySelector(poller.getAttribute("data-reconcile-rows"));
        var inflight = [], settled = [];
        if (tbody) {
            tbody.querySelectorAll("tr[data-live]").forEach(function (tr) {
                (tr.getAttribute("data-live") === "inflight" ? inflight : settled).push(tr);
            });
        }
        var st = state.get(poller) || { offsets: {}, sent: {} };
        var choice = selectBatch(
            inflight, settled,
            parseInt(poller.getAttribute("data-reconcile-max"), 10),
            parseInt(poller.getAttribute("data-reconcile-settled-min"), 10),
            st.offsets
        );
        var sent = {};
        var tokens = choice.batch.map(function (tr) {
            var id = tr.id.slice(tr.id.indexOf("-") + 1);
            sent[tr.id] = tr.getAttribute("data-row-version");
            return id + "." + sent[tr.id];
        });
        state.set(poller, { offsets: choice.offsets, sent: sent });
        if (tokens.length) evt.detail.parameters.r = tokens;
        var first = tbody && tbody.querySelector("tr[data-key]");
        if (first) evt.detail.parameters.newest = first.getAttribute("data-key");
    });

    document.addEventListener("htmx:oobBeforeSwap", function (evt) {
        if (applying === null) return;
        var target = evt.detail.target;
        if (!target || !Object.prototype.hasOwnProperty.call(applying, target.id)) return;
        var incoming = evt.detail.fragment;
        if (incoming && incoming.getAttribute && incoming.getAttribute("hx-swap-oob") === "delete") return;
        if (target.getAttribute("data-row-version") !== applying[target.id]) {
            evt.detail.shouldSwap = false;   // changed since the request was sent — keep the newer row
        }
    });
})(typeof window !== "undefined" ? window : globalThis);
