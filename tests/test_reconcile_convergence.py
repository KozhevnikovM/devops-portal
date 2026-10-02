"""Worst-case convergence of page reconciliation's batch rotation (#497 D7, D9).

The spec bound, for interval T, batch size B, settled reserve R, I in-flight and S settled rows: a
change is reflected within ⌈I / (B − min(S, R))⌉ × T for an in-flight row and
⌈S / max(min(S, R), B − I)⌉ × T for a settled one. Simulated here tick by tick (one request per
tick) on the Python oracle of the client's `selectBatch`, which tests/js/select_batch_cases.json
pins to the JS.
"""
import json
import math
from pathlib import Path

import pytest

from tests.reconcile_oracle import select_batch

_CASES = json.loads((Path(__file__).parent / "js" / "select_batch_cases.json").read_text())


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_oracle_matches_the_fixture_table(case):
    batch, offsets = select_batch(case["inflight"], case["settled"], case["max"],
                                  case["settledMin"], case["offsets"])
    assert batch == case["batch"]
    assert offsets == case["nextOffsets"]


def _bounds(i, s, b, r):
    inflight = math.ceil(i / (b - min(s, r))) if i else 0
    settled = math.ceil(s / max(min(s, r), b - i)) if s else 0
    return inflight, settled


def _gaps(inflight, settled, b, r, ticks):
    """Per row: the most ticks between two of its sends (counting from the start)."""
    offsets, last, gap = {}, {}, {}
    for tick in range(1, ticks + 1):
        batch, offsets = select_batch(inflight, settled, b, r, offsets)
        assert len(batch) <= b                       # never more than the cap
        assert len(batch) == len(set(batch))         # no duplicates
        for row in batch:
            gap[row] = max(gap.get(row, 0), tick - last.get(row, 0))
            last[row] = tick
    for row in inflight + settled:                   # a row never sent has an unbounded gap
        gap[row] = max(gap.get(row, ticks + 1), ticks + 1 - last.get(row, 0))
    return gap


@pytest.mark.parametrize("i, s, b, r", [
    (1, 0, 50, 10),       # 1 row
    (0, 1, 50, 10),
    (10, 40, 50, 10),     # one page of 50
    (50, 0, 50, 10),
    (5, 145, 50, 10),     # three loaded pages, mostly settled — spec example: 1 and 4 intervals
    (60, 30, 50, 10),     # in-flight overflow — spec example: settled within 3, never starved
    (150, 0, 50, 10),
    (0, 150, 50, 10),
    (40, 110, 50, 1),     # smallest reserve still serves settled rows
])
def test_every_row_is_sent_within_the_spec_bound(i, s, b, r):
    inflight = [f"i{k}" for k in range(i)]
    settled = [f"s{k}" for k in range(s)]
    inflight_bound, settled_bound = _bounds(i, s, b, r)
    gaps = _gaps(inflight, settled, b, r, ticks=40)
    assert all(gaps[row] <= inflight_bound for row in inflight), (inflight_bound, max(gaps[x] for x in inflight))
    assert all(gaps[row] <= settled_bound for row in settled), (settled_bound, max(gaps[x] for x in settled))


def test_spec_examples():
    assert _bounds(5, 145, 50, 10) == (1, 4)
    assert _bounds(60, 30, 50, 10)[1] == 3


def test_in_flight_rows_go_first():
    batch, _ = select_batch([f"i{k}" for k in range(30)], [f"s{k}" for k in range(100)], 50, 10, {})
    assert batch[:30] == [f"i{k}" for k in range(30)]   # every in-flight row, every tick
    assert len(batch) == 50


def test_settled_reserve_is_honoured_under_in_flight_overflow():
    batch, _ = select_batch([f"i{k}" for k in range(60)], [f"s{k}" for k in range(30)], 50, 10, {})
    assert sum(row.startswith("s") for row in batch) == 10


def test_prepended_row_is_sent_within_the_bound_for_the_new_counts():
    inflight, settled = [f"i{k}" for k in range(45)], [f"s{k}" for k in range(60)]
    offsets = {}
    for _ in range(3):
        _, offsets = select_batch(inflight, settled, 50, 10, offsets)
    inflight = ["new"] + inflight               # an order prepends a row mid-rotation
    bound, _ = _bounds(len(inflight), len(settled), 50, 10)
    for tick in range(1, bound + 1):
        batch, offsets = select_batch(inflight, settled, 50, 10, offsets)
        if "new" in batch:
            break
    else:
        pytest.fail("prepended row not sent within the bound")
    assert tick <= bound
