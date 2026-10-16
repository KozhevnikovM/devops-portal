"""Readiness check: every script/stylesheet the page templates reference is actually served (#542).

Under docker compose the app serves `app/static/` from the `portal_static` volume, which only the
`init` container fills (from its own image), while the dev bind mount makes template edits live
immediately. When the two drift — templates referencing a script the volume never received — the
page silently degrades (the #542 row-action menu opened centred on screen). This check makes that
skew visible: `/health/ready` fails, naming the missing files.

It is evaluated on every call, not cached at startup: Jinja reloads changed templates without a
restart, so a startup-only answer would stay "ok" while the live page requests a missing script.
Each template is re-parsed only when its mtime changes; asset existence is re-checked every time.
The check is existence-only — a changed file under an unchanged name is not detected.
"""

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Same-origin script and stylesheet references; a path containing a Jinja expression (`{`) isn't
# a literal reference and is ignored. Query strings (cache-busters like `?v=2`) are dropped.
_REFERENCE_RE = re.compile(
    r"""(?:src|href)\s*=\s*["'](/static/[^"'?#{\s]+\.(?:js|css))"""
)


class StaticAssetCheck:
    def __init__(
        self, templates_dir: Path, static_dir: Path, url_prefix: str = "/static/"
    ):
        self._templates_dir = templates_dir
        self._static_dir = static_dir
        self._url_prefix = url_prefix
        self._parsed: dict[Path, tuple[int, frozenset[str]]] = {}
        self._last_missing: tuple[str, ...] | None = None
        # Templates (re-)read so far; lets tests prove the mtime cache works.
        self.parse_count = 0

    def references(self) -> set[str]:
        """Every literal `/static/…` script or stylesheet the templates reference."""
        seen: dict[Path, tuple[int, frozenset[str]]] = {}
        for template in sorted(self._templates_dir.rglob("*.html")):
            mtime = template.stat().st_mtime_ns
            cached = self._parsed.get(template)
            if cached is None or cached[0] != mtime:
                self.parse_count += 1
                found = _REFERENCE_RE.findall(template.read_text(encoding="utf-8"))
                cached = (mtime, frozenset(found))
            seen[template] = cached
        self._parsed = seen  # forget templates that were deleted
        return set().union(*(refs for _, refs in seen.values()))

    def missing(self) -> list[str]:
        """The referenced assets that don't exist under the served static directory, sorted."""
        static_root = self._static_dir.resolve()
        absent = []
        for reference in self.references():
            path = (static_root / reference.removeprefix(self._url_prefix)).resolve()
            if not path.is_relative_to(static_root) or not path.is_file():
                absent.append(reference)
        return sorted(absent)

    def check(self) -> list[str]:
        """`missing()`, logging an ERROR whenever the set of missing assets changes."""
        absent = self.missing()
        current = tuple(absent)
        if current != self._last_missing:
            if absent:
                logger.error(
                    "static assets referenced by the page templates are not served: %s — rebuild "
                    "the image that fills the static directory (docker compose up --build)",
                    ", ".join(absent),
                )
            elif self._last_missing:
                logger.info(
                    "static assets: every referenced script and stylesheet is served again"
                )
            self._last_missing = current
        return absent
