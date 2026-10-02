"""Python mirror of `selectBatch` in frontend/js/row_reconcile.js (#497 D7).

CI has no node, so the convergence tests run against this oracle; tests/js/select_batch_cases.json
is checked against both this and the JS (`node --test tests/js/*.test.mjs`), so the two can't drift apart.
"""


def _rotate(items: list, count: int, offset: int) -> tuple[list, int]:
    n = len(items)
    if n == 0 or count <= 0:
        return [], 0
    start = offset % n
    take = min(count, n)
    return [items[(start + k) % n] for k in range(take)], (start + take) % n


def select_batch(inflight: list, settled: list, max_ids: int, settled_min: int,
                 offsets: dict) -> tuple[list, dict]:
    reserve = min(len(settled), settled_min)
    live, live_offset = _rotate(inflight, min(len(inflight), max_ids - reserve), offsets.get("inflight", 0))
    calm, calm_offset = _rotate(settled, min(len(settled), max_ids - len(live)), offsets.get("settled", 0))
    return live + calm, {"inflight": live_offset, "settled": calm_offset}
