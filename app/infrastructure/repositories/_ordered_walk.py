"""The ordered-walk plan pin shared by the bookings and environments page-key queries
(#479, #485, #495, #496)."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Pin for the page key query (#479, design.md Decision 10; #485). A branch's page key leaves its
# own index as the only one with usable conditions, and the only one read in page order, so that
# walk needs no sort. Every other path for a branch sorts: a bitmap or sequential scan, or a full
# scan of another page index whose partial predicate the branch implies (e.g. a creator branch's
# `created_by IS NOT NULL` makes ix_bookings_creator_page a candidate). Such a full scan is cheap
# when that index is empty or its statistics are stale, and then reads the index whole with the
# page key as a mere filter (review of #488). With bitmap scans, sequential scans and sorts off
# and index scans on, the sort-free page-key walk is the one plan left that no disabled step
# penalises, whatever the statistics. The label window's own sorts, of at most S + 1 rows, still
# run: a disabled step is only penalised, and those sorts have no alternative. If an operator
# had turned index scans off, disabling the rest would leave only a sequential scan, hence "on".
# Those penalised label-window sorts inflate the plan's estimated cost past jit_above_cost, which
# would JIT-compile a millisecond query for most of a second — so JIT is off here too.
# Transaction-local, and restored to the exact previous values right after the key query, so
# nothing else in the request plans differently.
_ORDERED_WALK_SETTINGS = {
    "enable_bitmapscan": "off",
    "enable_seqscan": "off",
    "enable_sort": "off",
    "enable_indexscan": "on",
    "jit": "off",
}
_PIN_ORDERED_WALK = text(
    "WITH prev AS MATERIALIZED (SELECT "
    + ", ".join(f"current_setting('{name}') AS {name}" for name in _ORDERED_WALK_SETTINGS)
    + ") SELECT "
    + ", ".join(_ORDERED_WALK_SETTINGS)
    + ", "
    + ", ".join(f"set_config('{name}', '{value}', true)" for name, value in _ORDERED_WALK_SETTINGS.items())
    + " FROM prev"
)
_UNPIN_ORDERED_WALK = text(
    "SELECT " + ", ".join(f"set_config('{name}', :{name}, true)" for name in _ORDERED_WALK_SETTINGS)
)


class _OrderedWalk:
    """`async with _OrderedWalk(session):` runs its body under the ordered-walk plan pin (the page
    key query, #479; the queue-position read, #495)."""

    def __init__(self, session: AsyncSession):
        self._session = session
        self._prev: dict | None = None

    async def __aenter__(self):
        row = (await self._session.execute(_PIN_ORDERED_WALK)).one()
        self._prev = {name: row._mapping[name] for name in _ORDERED_WALK_SETTINGS}
        return self

    async def __aexit__(self, exc_type, exc, tb):
        # On an error the transaction is aborted and the local settings go with it; restoring
        # would only fail on the aborted transaction and mask the original error.
        if exc_type is None:
            await self._session.execute(_UNPIN_ORDERED_WALK, self._prev)
        return False
