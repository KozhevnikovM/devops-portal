"""Replays a list section's reconcile poller the way the browser does (#505 D7, #497).

Before each 60 s poll, frontend/js/row_reconcile.js picks a bounded batch of the displayed live rows
(`selectBatch`), sends `r=<id>.<version>` for each, and `newest=<data-key>` of the first displayed
row; after the response it applies the out-of-band rows. A simulated tab has to do the same, or the
server's batch read is never exercised.

The batch rule is not re-implemented here: `select_batch` comes from tests/reconcile_oracle.py, the
Python mirror the fast suite already checks against the JS cases, so the two can't drift apart.

Standard library only (no Locust), so tests/test_loadtest_reconcile.py runs in the fast suite.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode

_ORACLE_PATH = Path(__file__).resolve().parent.parent / "tests" / "reconcile_oracle.py"
_spec = importlib.util.spec_from_file_location(
    "loadtest_reconcile_oracle", _ORACLE_PATH
)
if _spec is None or _spec.loader is None:
    raise ImportError(f"cannot load {_ORACLE_PATH}")
_oracle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_oracle)
select_batch = _oracle.select_batch

POLLER_ATTRS = (
    "hx-get",
    "data-reconcile-rows",
    "data-reconcile-max",
    "data-reconcile-settled-min",
)


class ReconcileMarkupError(ValueError):
    """The page has no reconcile poller, or it lacks an attribute the browser relies on."""


@dataclass
class Row:
    """One displayed `<tr>` of a list section's tbody, as the browser sees it."""

    dom_id: str
    version: str | None
    live: str | None  # "inflight", "settled", or None for a non-live (RELEASED) row
    # The row's list key (data-key); rows like the empty-state row have none.
    key: str | None

    @property
    def row_id(self) -> str:
        return self.dom_id[self.dom_id.index("-") + 1 :]


class _PageParser(HTMLParser):
    """Collects the reconcile poller's attributes and every `<tr>` per tbody id, in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.poller: dict[str, str | None] | None = None
        self.tbodies: dict[str, list[Row]] = {}
        self._tbody: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "tbody":
            self._tbody.append(a.get("id"))
            if a.get("id"):
                self.tbodies.setdefault(a["id"], [])
        elif tag == "tr" and self._tbody and self._tbody[-1] and a.get("id"):
            self.tbodies[self._tbody[-1]].append(
                Row(
                    a["id"],
                    a.get("data-row-version"),
                    a.get("data-live"),
                    a.get("data-key"),
                ),
            )
        elif "data-reconcile-rows" in a and self.poller is None:
            self.poller = a

    def handle_endtag(self, tag: str) -> None:
        if tag == "tbody" and self._tbody:
            self._tbody.pop()


@dataclass
class Section:
    """A loaded list section: its poller and the rows it displays. Rotation offsets live here, so a
    page reload (a new Section) resets them, as replacing the section does in the browser."""

    url: str
    max_ids: int
    settled_min: int
    rows: list[Row]
    offsets: dict = field(default_factory=dict)

    @property
    def stats_name(self) -> str:
        return self.url.split("?", 1)[0]

    def next_params(self) -> list[tuple[str, str]]:
        """The batch for the next poll — `r` per chosen row, then `newest` — advancing rotation."""
        inflight = [row for row in self.rows if row.live == "inflight"]
        settled = [
            row for row in self.rows if row.live is not None and row.live != "inflight"
        ]
        batch, self.offsets = select_batch(
            inflight, settled, self.max_ids, self.settled_min, self.offsets
        )
        params = [("r", f"{row.row_id}.{row.version}") for row in batch]
        first = next((row for row in self.rows if row.key), None)
        if first is not None:
            params.append(("newest", first.key))
        return params

    def next_url(self) -> str:
        params = self.next_params()
        if not params:
            return self.url
        return self.url + ("&" if "?" in self.url else "?") + urlencode(params)

    def apply_response(self, html: str) -> None:
        """Apply the response's out-of-band rows: deletions drop the row, others replace its state.
        Rows the section doesn't display (the newer-rows indicator, a row from another page) are
        ignored, as htmx finds no target for them."""
        parser = _OobParser()
        parser.feed(html)
        parser.close()
        by_id = {row.dom_id: i for i, row in enumerate(self.rows)}
        deleted = set()
        for dom_id, swap, row in parser.rows:
            if dom_id not in by_id:
                continue
            if swap == "delete":
                deleted.add(dom_id)
            else:
                self.rows[by_id[dom_id]] = row
        self.rows = [row for row in self.rows if row.dom_id not in deleted]


class _OobParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[str, str, Row]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "tr" and a.get("id") and a.get("hx-swap-oob"):
            row = Row(
                a["id"],
                a.get("data-row-version"),
                a.get("data-live"),
                a.get("data-key"),
            )
            self.rows.append((a["id"], a["hx-swap-oob"] or "", row))


def parse_section(html: str) -> Section:
    """The list section of a rendered page. Raises ReconcileMarkupError when the poller or one of
    its attributes is missing, so a template change breaks the load test loudly."""
    parser = _PageParser()
    parser.feed(html)
    parser.close()
    poller = parser.poller
    if poller is None:
        raise ReconcileMarkupError("page has no reconcile poller (data-reconcile-rows)")
    missing = [name for name in POLLER_ATTRS if not poller.get(name)]
    if missing:
        raise ReconcileMarkupError(f"reconcile poller lacks {', '.join(missing)}")
    selector = poller["data-reconcile-rows"] or ""
    if not selector.startswith("#"):
        raise ReconcileMarkupError(
            f"unsupported data-reconcile-rows selector {selector!r}"
        )
    try:
        max_ids = int(poller["data-reconcile-max"] or "")
        settled_min = int(poller["data-reconcile-settled-min"] or "")
    except ValueError as exc:
        raise ReconcileMarkupError(
            f"reconcile poller has a non-integer limit: {exc}"
        ) from exc
    return Section(
        url=poller["hx-get"] or "",
        max_ids=max_ids,
        settled_min=settled_min,
        # The browser queries the tbody; a page without it is an empty list, not an error.
        rows=list(parser.tbodies.get(selector[1:], [])),
    )
