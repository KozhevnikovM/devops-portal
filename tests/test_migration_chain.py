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
    assert _script().get_heads() == ["0030"]


def test_startup_script_chain_is_linear():
    # The config columns/tables (0018 startup_script, 0019 config_failed, 0020 roles, 0021
    # config_roles) each arrive in their own revision, linear on top of 0017.
    down = {r.revision: r.down_revision for r in _script().walk_revisions()}
    assert down["0018"] == "0017"
    assert down["0019"] == "0018"
    assert down["0020"] == "0019"
    assert down["0021"] == "0020"
    assert down["0022"] == "0021"
    assert down["0023"] == "0022"
    assert down["0024"] == "0023"
    assert down["0025"] == "0024"
    assert down["0026"] == "0025"
    assert down["0027"] == "0026"
    assert down["0028"] == "0027"
    assert down["0029"] == "0028"
    assert down["0030"] == "0029"


def test_namespace_per_cluster_chain_is_linear():
    # The (name, cluster) uniqueness arrives in its own revision on top of 0016,
    # not by editing the namespaces table migration in place.
    down = {r.revision: r.down_revision for r in _script().walk_revisions()}
    assert down["0017"] == "0016"


def test_static_vm_chain_is_linear():
    script = _script()
    down = {r.revision: r.down_revision for r in script.walk_revisions()}
    # ssh_key arrives in its own revision layered on top of the catalog table,
    # not by editing 0013 in place.
    assert down["0014"] == "0013"
    assert down["0013"] == "0012"
