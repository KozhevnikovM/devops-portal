/* Places the environment row action menu (#538). The menu is a native popover: the browser
 * renders it in the top layer, outside the table's overflow-x-auto clipping, and handles
 * outside-click and Escape dismissal. This controller only anchors the open menu to its
 * trigger, keeps it inside the viewport (scrolling internally when space is short) and owns the
 * scroll/resize/mutation listeners for exactly as long as a menu is open.
 */
(function () {
    "use strict";
    var GAP = 4, MARGIN = 8;
    var menu = null, trigger = null, observer = null;

    function clamp(value, low, high) {
        return Math.max(low, Math.min(value, high));
    }

    function position() {
        if (!menu) return;
        if (!menu.isConnected || !trigger.isConnected) { detach(true); return; }
        var viewportWidth = document.documentElement.clientWidth;
        var viewportHeight = document.documentElement.clientHeight;
        var rect = trigger.getBoundingClientRect();
        // A trigger scrolled out of view leaves nothing to anchor the menu to.
        if (rect.bottom <= 0 || rect.top >= viewportHeight ||
            rect.right <= 0 || rect.left >= viewportWidth) {
            menu.hidePopover();
            return;
        }
        menu.style.maxHeight = "none";
        var natural = menu.offsetHeight;
        var below = viewportHeight - rect.bottom - GAP - MARGIN;
        var above = rect.top - GAP - MARGIN;
        // Open downward when the menu fits there or that side is the roomier one.
        var down = natural <= below || below >= above;
        var room = Math.max(0, down ? below : above);
        var height = Math.min(natural, room);
        menu.style.maxHeight = room + "px";
        var width = menu.offsetWidth;
        menu.style.left = clamp(rect.right - width, MARGIN, viewportWidth - width - MARGIN) + "px";
        menu.style.top = (down ? rect.bottom + GAP : rect.top - GAP - height) + "px";
    }

    function onScroll(event) {
        if (menu && !menu.contains(event.target)) position();
    }

    function attach(opened) {
        if (!opened.matches(":popover-open")) return;
        menu = opened;
        trigger = menu.parentElement.querySelector("[data-environment-menu-trigger]");
        // Replace the UA popover's centred inset/margin with trigger-relative coordinates, and
        // make the computed max-height bound the whole box, borders and padding included.
        menu.style.margin = "0";
        menu.style.boxSizing = "border-box";
        menu.style.right = menu.style.bottom = "auto";
        window.addEventListener("resize", position);
        document.addEventListener("scroll", onScroll, true);
        // An HTMX/SSE swap or reconcile removal of the row or list section hides the popover
        // natively but fires no toggle event we can see, so watch for the menu leaving the page.
        observer = new MutationObserver(function () {
            if (menu && !menu.isConnected) detach(true);
        });
        observer.observe(document.body, { childList: true, subtree: true });
        position();
        menu.style.visibility = "";
    }

    function detach(removed) {
        if (!menu) return;
        var triggerId = trigger && trigger.id;
        window.removeEventListener("resize", position);
        document.removeEventListener("scroll", onScroll, true);
        observer.disconnect();
        menu = trigger = observer = null;
        // A swap out from under a focused menu drops focus to <body>; hand it to the new row's trigger.
        var active = document.activeElement;
        if (removed && triggerId && (!active || active === document.body)) {
            var replacement = document.getElementById(triggerId);
            if (replacement) replacement.focus({ preventScroll: true });
        }
    }

    function isMenu(element) {
        return element instanceof Element && element.hasAttribute("data-environment-menu");
    }

    // Popover toggle events don't bubble, so listen in the capture phase.
    document.addEventListener("beforetoggle", function (event) {
        // Hidden until positioned, so it never flashes at the UA's centred default.
        if (isMenu(event.target) && event.newState === "open") event.target.style.visibility = "hidden";
    }, true);
    document.addEventListener("toggle", function (event) {
        if (!isMenu(event.target)) return;
        if (event.newState === "open") {
            if (menu && menu !== event.target) detach(false);
            attach(event.target);
        } else if (event.target === menu) {
            detach(false);
        }
    }, true);
})();
