"""Settings for page row reconciliation and the environment child limit (#497)."""
import pytest
from pydantic import ValidationError

from app.config import Settings


def test_reconcile_settings_defaults():
    s = Settings()
    assert s.RECONCILE_MAX_IDS == 50
    assert s.RECONCILE_SETTLED_MIN == 10
    assert s.ENVIRONMENT_MAX_CHILDREN == 25


@pytest.mark.parametrize("overrides", [
    {"RECONCILE_MAX_IDS": 0},
    {"RECONCILE_MAX_IDS": 51},  # above the page size (50)
    {"RECONCILE_MAX_IDS": 40, "ENVIRONMENTS_PAGE_SIZE": 30},  # above the smaller page size
    {"RECONCILE_SETTLED_MIN": 0},  # settled rows could be starved
    {"RECONCILE_SETTLED_MIN": 50},  # must leave room below RECONCILE_MAX_IDS
    {"ENVIRONMENT_MAX_CHILDREN": 0},
])
def test_reconcile_settings_reject_out_of_range(overrides):
    with pytest.raises(ValidationError):
        Settings(**overrides)


def test_reconcile_settings_accept_edges():
    # RECONCILE_SETTLED_MIN < RECONCILE_MAX_IDS forces MAX ≥ 2.
    s = Settings(RECONCILE_MAX_IDS=2, RECONCILE_SETTLED_MIN=1)
    assert (s.RECONCILE_MAX_IDS, s.RECONCILE_SETTLED_MIN) == (2, 1)
    assert Settings(RECONCILE_MAX_IDS=50, RECONCILE_SETTLED_MIN=49).RECONCILE_SETTLED_MIN == 49
