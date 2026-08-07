/*
 * ansible_vars_editor.js — progressive key-value editor for Ansible variable fields.
 *
 * Enhances any `<div data-kv-editor>` that wraps a single `<textarea name="...">`. The textarea
 * stays in the DOM as the real form field (backend contract unchanged: it still submits a YAML/JSON
 * string under its original `name`), but is hidden while the user edits `[Key] [Value]` rows. On
 * every edit the rows are serialized back into the textarea, so a normal (HTMX or native) submit
 * carries the same payload the raw textarea always did.
 *
 * Secret fields (`data-kv-secret="true"`): existing keys render masked (value shown as ●●●). An
 * untouched masked row serializes to the sentinel `●●●`, which the backend merges as "keep this
 * key's stored ciphertext"; a row the admin types into serializes its new plaintext. See
 * openspec/changes/ansible-vars-secrets-form-ui/design.md (Decision 3).
 */
(function () {
    "use strict";

    var SECRET_SENTINEL = "●●●"; // ●●●

    // ---- scalar typing: mirror YAML's common scalar coercions for non-secret values ----
    function coerceScalar(raw) {
        if (raw === "") return "";
        if (raw === "true") return true;
        if (raw === "false") return false;
        if (raw === "null" || raw === "~") return null;
        if (/^-?\d+$/.test(raw)) return parseInt(raw, 10);
        if (/^-?\d*\.\d+$/.test(raw)) return parseFloat(raw);
        return raw;
    }

    // ---- serialize rows -> JSON object string ("" when no keys). JSON is valid YAML, so the
    //      existing yaml.safe_load backend parsers accept it unchanged. ----
    function serialize(rows, isSecret) {
        var obj = {};
        var any = false;
        rows.forEach(function (r) {
            var key = (r.key || "").trim();
            if (!key) return;
            any = true;
            if (isSecret) {
                obj[key] = r.masked ? SECRET_SENTINEL : r.value;
            } else {
                obj[key] = coerceScalar(r.value);
            }
        });
        return any ? JSON.stringify(obj) : "";
    }

    function stripQuotes(s) {
        if (s.length >= 2) {
            var a = s.charAt(0), b = s.charAt(s.length - 1);
            if ((a === '"' && b === '"') || (a === "'" && b === "'")) return s.slice(1, -1);
        }
        return s;
    }

    // ---- parse textarea content into rows. Returns null when the content is too complex
    //      (nested mappings/lists, block scalars) to represent as flat rows — caller stays in Raw. ----
    function parse(text, isSecret) {
        text = (text || "").trim();
        if (!text) return [];

        if (text.charAt(0) === "{") {
            try {
                var obj = JSON.parse(text);
                if (obj && typeof obj === "object" && !Array.isArray(obj)) {
                    var rows = [];
                    var keys = Object.keys(obj);
                    for (var i = 0; i < keys.length; i++) {
                        var v = obj[keys[i]];
                        if (v !== null && typeof v === "object") return null;
                        rows.push(makeRow(keys[i], v === null ? "null" : String(v), isSecret));
                    }
                    return rows;
                }
            } catch (e) { /* not JSON — fall through to null */ }
            return null;
        }
        if (text.charAt(0) === "[") return null;

        var lines = text.split("\n");
        var out = [];
        for (var j = 0; j < lines.length; j++) {
            var line = lines[j];
            if (!line.trim()) continue;
            if (line.trim().charAt(0) === "#") continue;
            if (/^\s/.test(line)) return null; // indentation => nested structure
            var m = line.match(/^([^:]+):\s?(.*)$/);
            if (!m) return null;
            var val = m[2];
            if (val === "|" || val === ">" || val === "|-" || val === ">-") return null;
            out.push(makeRow(m[1].trim(), stripQuotes(val.trim()), isSecret));
        }
        return out;
    }

    function makeRow(key, value, isSecret) {
        // A secret key arriving with no visible value is an existing secret → start masked.
        var masked = !!isSecret && (value === "" || value === SECRET_SENTINEL);
        return { key: key, value: masked ? "" : value, masked: masked };
    }

    function el(tag, cls, attrs) {
        var e = document.createElement(tag);
        if (cls) e.className = cls;
        if (attrs) Object.keys(attrs).forEach(function (k) { e.setAttribute(k, attrs[k]); });
        return e;
    }

    function initOne(container) {
        if (container.__kvInit) return;
        var textarea = container.querySelector("textarea[name]");
        if (!textarea) return;
        container.__kvInit = true;

        var isSecret = container.getAttribute("data-kv-secret") === "true";
        var parsed = parse(textarea.value, isSecret);
        var rows = parsed || [];
        var mode = parsed === null ? "raw" : "kv"; // complex content opens in Raw

        var ui = el("div", "kv-editor");
        var tabs = el("div", "kv-tabs");
        var tabKv = el("button", "kv-tab", { type: "button" });
        tabKv.textContent = "Key-Value";
        var tabRaw = el("button", "kv-tab", { type: "button" });
        tabRaw.textContent = "Raw";
        tabs.appendChild(tabKv);
        tabs.appendChild(tabRaw);
        var rowsWrap = el("div", "kv-rows");
        var addBtn = el("button", "kv-add", { type: "button" });
        addBtn.textContent = "+ Add variable";
        var notice = el("div", "kv-notice");
        notice.textContent = "Nested/complex value — edit in Raw mode.";
        notice.style.display = "none";

        // Move the textarea inside our UI so tabs/rows sit directly above it.
        container.insertBefore(ui, textarea);
        ui.appendChild(tabs);
        ui.appendChild(rowsWrap);
        ui.appendChild(addBtn);
        ui.appendChild(notice);
        ui.appendChild(textarea);
        textarea.classList.add("kv-raw");

        function sync() {
            // Only the KV view drives the textarea; in Raw mode the textarea is source of truth.
            if (mode === "kv") textarea.value = serialize(rows, isSecret);
        }

        function renderRows() {
            rowsWrap.innerHTML = "";
            rows.forEach(function (row, idx) {
                var r = el("div", "kv-row");
                var key = el("input", "kv-key", { type: "text", placeholder: "key", autocomplete: "off" });
                key.value = row.key;
                var val = el("input", "kv-val", {
                    type: isSecret ? "text" : "text",
                    placeholder: row.masked ? SECRET_SENTINEL : "value",
                    autocomplete: "off",
                });
                val.value = row.value;
                var rm = el("button", "kv-remove", { type: "button", "aria-label": "Remove" });
                rm.textContent = "×"; // ×

                key.addEventListener("input", function () { rows[idx].key = key.value; sync(); });
                val.addEventListener("input", function () {
                    rows[idx].value = val.value;
                    if (rows[idx].masked) { rows[idx].masked = false; val.placeholder = "value"; }
                    sync();
                });
                rm.addEventListener("click", function () { rows.splice(idx, 1); renderRows(); sync(); });

                r.appendChild(key);
                r.appendChild(val);
                r.appendChild(rm);
                rowsWrap.appendChild(r);
            });
        }

        function setMode(next) {
            if (next === "kv") {
                var p = parse(textarea.value, isSecret);
                if (p === null) { notice.style.display = ""; return; } // stay in Raw
                rows = p;
                notice.style.display = "none";
                mode = "kv";
                renderRows();
                sync();
            } else {
                // Entering Raw: freeze the current rows into the textarea as its starting text.
                if (mode === "kv") textarea.value = serialize(rows, isSecret);
                mode = "raw";
            }
            applyMode();
        }

        function applyMode() {
            var kv = mode === "kv";
            tabKv.classList.toggle("kv-active", kv);
            tabRaw.classList.toggle("kv-active", !kv);
            rowsWrap.style.display = kv ? "" : "none";
            addBtn.style.display = kv ? "" : "none";
            textarea.hidden = kv;
        }

        tabKv.addEventListener("click", function () { setMode("kv"); });
        tabRaw.addEventListener("click", function () { setMode("raw"); });
        addBtn.addEventListener("click", function () {
            rows.push({ key: "", value: "", masked: false });
            renderRows();
        });

        renderRows();
        applyMode();
        sync(); // seed the textarea (incl. secret sentinels) before any interaction
    }

    function initAll(root) {
        var scope = root && root.querySelectorAll ? root : document;
        scope.querySelectorAll("[data-kv-editor]").forEach(initOne);
        // htmx.onLoad passes the swapped element itself, which may *be* the editor.
        if (root && root.matches && root.matches("[data-kv-editor]")) initOne(root);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", function () { initAll(document); });
    } else {
        initAll(document);
    }
    if (window.htmx && typeof window.htmx.onLoad === "function") {
        window.htmx.onLoad(function (elt) { initAll(elt); });
    }
})();
