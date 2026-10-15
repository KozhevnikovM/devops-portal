/* One delegated controller for every initial/appended/SSE/reconciled deployment cell.
 * Report markup comes from autoescaped Jinja templates, never executable data strings.
 * Panels live under body, outside the horizontally scrolling environment table.
 */
(function () {
    "use strict";
    var trigger = null, panel = null, host = null, pendingFocus = null;

    function restoreFocus() {
        if (!pendingFocus) return;
        var replacement = document.getElementById(pendingFocus) ||
            document.getElementById("environments-label-filter") ||
            document.querySelector('a[href="/environments"]');
        pendingFocus = null;
        if (replacement) replacement.focus({ preventScroll: true });
    }

    function close(restore, replacing) {
        if (!trigger) return;
        var old = trigger;
        old.setAttribute("aria-expanded", "false");
        panel.remove();
        host.remove();
        trigger = panel = host = null;
        if (replacing) pendingFocus = old.id;
        else if (restore && old.isConnected) old.focus({ preventScroll: true });
    }

    function position() {
        if (!trigger) return;
        if (!trigger.isConnected) { close(false, true); return; }
        var gap = 8, viewportWidth = window.innerWidth, viewportHeight = window.innerHeight;
        var rect = trigger.getBoundingClientRect();
        panel.style.maxWidth = Math.max(0, viewportWidth - gap * 2) + "px";
        panel.style.maxHeight = Math.max(0, viewportHeight - gap * 2) + "px";
        var width = panel.getBoundingClientRect().width;
        var height = panel.getBoundingClientRect().height;
        var top = rect.bottom + gap;
        if (top + height > viewportHeight - gap) top = rect.top - height - gap;
        panel.style.left = Math.max(gap, Math.min(rect.left, viewportWidth - width - gap)) + "px";
        panel.style.top = Math.max(gap, Math.min(top, viewportHeight - height - gap)) + "px";
    }

    document.addEventListener("click", function (event) {
        var button = event.target.closest("[data-deployment-trigger]");
        if (button) {
            if (button === trigger) { close(true, false); return; }
            close(false, false);
            var template = button.parentElement.querySelector("[data-deployment-template]");
            if (!template) return;
            trigger = button;
            host = document.createElement("div");
            host.setAttribute("data-deployment-overlay-host", "");
            panel = template.content.firstElementChild.cloneNode(true);
            host.appendChild(panel);
            document.body.appendChild(host);
            trigger.setAttribute("aria-expanded", "true");
            position();
            panel.focus({ preventScroll: true });
        } else if (panel && !panel.contains(event.target) &&
                   !trigger.parentElement.contains(event.target)) close(true, false);
    });
    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && trigger) {
            event.preventDefault();
            close(true, false);
        }
    });
    // The cleanup event fires only on elements actually being removed, so a reconciliation
    // response that keeps a row unchanged (or skips an obsolete swap) preserves its panel.
    document.addEventListener("htmx:beforeCleanupElement", function (event) {
        var element = event.detail && event.detail.elt;
        if (trigger && element && (element === trigger || element.contains(trigger))) close(false, true);
    });
    document.addEventListener("htmx:afterSwap", restoreFocus);
    window.addEventListener("resize", position);
    document.addEventListener("scroll", function (event) {
        if (panel && !panel.contains(event.target)) position();
    }, true);
    // Covers native removals as well as SSE/HTMX implementations with no cleanup event.
    // A single observer/listener set lives for the page; none is allocated per row or opening.
    function observe() {
        new MutationObserver(function () {
            if (trigger && !trigger.isConnected) close(false, true);
            restoreFocus();
        }).observe(document.body, { childList: true, subtree: true });
    }
    if (document.body) observe();
    else document.addEventListener("DOMContentLoaded", observe, { once: true });
})();
