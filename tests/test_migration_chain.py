"""Guard against editing an already-applied migration (regression for #129).

If a schema change is added by mutating an existing revision instead of adding a new
one, environments already stamped at that revision never receive it. These tests assert
the Alembic history is a single linear chain so such mistakes surface in CI.
"""
from alembic.config import Config
from alembic.script import ScriptDirectory


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config("alembic.ini"))


def test_single_head():
    assert _script().get_heads() == ["0014"]


def test_static_vm_chain_is_linear():
    script = _script()
    down = {r.revision: r.down_revision for r in script.walk_revisions()}
    # ssh_key arrives in its own revision layered on top of the catalog table,
    # not by editing 0013 in place.
    assert down["0014"] == "0013"
    assert down["0013"] == "0012"
