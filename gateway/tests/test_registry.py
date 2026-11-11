import sqlite3

import pytest

from app import audit
from app.auth import User
from app.registry import Registry, RegistryError

A, B, QA, V = User("steward-a", "steward"), User("steward-b", "steward"), User("qa-1", "qa"), User("viewer-1", "viewer")

SRC = {"name": "21 CFR 211", "url": "https://www.ecfr.gov/api/versioner/v1/full/{date}/title-21.xml?part=211",
       "jurisdiction": "US-FDA", "product_types": ["drug"], "document_types": ["regulation"],
       "check_frequency": "manual", "legal_weight": "regulation", "auto_accept": False}


@pytest.fixture
def reg(tmp_path):
    return Registry(str(tmp_path / "g.db"))


def active_source(reg):
    s = reg.propose_add(SRC, A)
    return reg.decide(s["pending_change"]["id"], True, B)


def test_propose_creates_pending_not_active(reg):
    s = reg.propose_add(SRC, A)
    assert s["status"] == "pending" and s["pending_change"]["proposed_by"] == "steward-a"
    assert reg.list_sources("active") == [] and len(reg.list_sources()) == 1


def test_proposer_cannot_approve_own_change(reg):
    s = reg.propose_add(SRC, A)
    with pytest.raises(RegistryError) as e:
        reg.decide(s["pending_change"]["id"], True, A)
    assert e.value.status == 403
    assert reg.get_source(s["id"])["status"] == "pending"


def test_second_steward_or_qa_activates(reg):
    s = reg.propose_add(SRC, A)
    assert reg.decide(s["pending_change"]["id"], True, QA)["status"] == "active"


def test_viewer_cannot_propose_or_approve(reg):
    with pytest.raises(RegistryError) as e:
        reg.propose_add(SRC, V)
    assert e.value.status == 403
    s = reg.propose_add(SRC, A)
    with pytest.raises(RegistryError):
        reg.decide(s["pending_change"]["id"], True, V)


def test_reject_add_leaves_source_inactive(reg):
    s = reg.propose_add(SRC, A)
    out = reg.decide(s["pending_change"]["id"], False, B, "no")
    assert out["status"] == "rejected"
    reg.propose_add(SRC, A)  # same url may be proposed again after a rejection


def test_duplicate_url_conflicts(reg):
    reg.propose_add(SRC, A)
    with pytest.raises(RegistryError) as e:
        reg.propose_add(SRC, B)
    assert e.value.status == 409


def test_edit_is_not_applied_until_approved(reg):
    s = active_source(reg)
    p = reg.propose_edit(s["id"], {"check_frequency": "weekly", "auto_accept": True}, B)
    assert p["check_frequency"] == "manual" and p["auto_accept"] is False  # active values unchanged
    assert p["pending_change"]["proposed"]["check_frequency"] == "weekly"
    with pytest.raises(RegistryError):
        reg.decide(p["pending_change"]["id"], True, B)  # own proposal
    done = reg.decide(p["pending_change"]["id"], True, A)
    assert done["check_frequency"] == "weekly" and done["auto_accept"] is True and done["pending_change"] is None


def test_rejected_edit_keeps_old_values(reg):
    s = active_source(reg)
    p = reg.propose_edit(s["id"], {"jurisdiction": "EU"}, B)
    out = reg.decide(p["pending_change"]["id"], False, A)
    assert out["jurisdiction"] == "US-FDA" and out["status"] == "active"


def test_only_one_pending_change_and_noop_edit_refused(reg):
    s = active_source(reg)
    reg.propose_edit(s["id"], {"check_frequency": "daily"}, B)
    with pytest.raises(RegistryError) as e:
        reg.propose_edit(s["id"], {"check_frequency": "weekly"}, B)
    assert e.value.status == 409


def test_retire_needs_second_person(reg):
    s = active_source(reg)
    p = reg.propose_retire(s["id"], A)
    assert p["status"] == "active"
    assert reg.decide(p["pending_change"]["id"], True, QA)["status"] == "retired"


def test_validation_and_allowlist_on_propose(reg):
    with pytest.raises(RegistryError):
        reg.propose_add({**SRC, "url": "https://evil.example.com/x"}, A)
    with pytest.raises(RegistryError):
        reg.propose_add({**SRC, "check_frequency": "hourly"}, A)
    with pytest.raises(RegistryError):
        reg.propose_add({**SRC, "legal_weight": "opinion"}, A)
    with pytest.raises(RegistryError):
        reg.propose_add({k: v for k, v in SRC.items() if k != "jurisdiction"}, A)
    assert reg.list_sources() == []


def test_get_missing_source_is_404(reg):
    with pytest.raises(RegistryError) as e:
        reg.get_source(99)
    assert e.value.status == 404


# ---- audit log ----
def test_every_change_is_audited_in_order(reg):
    s = reg.propose_add(SRC, A)
    reg.decide(s["pending_change"]["id"], True, B, "looks right")
    p = reg.propose_edit(s["id"], {"auto_accept": True}, B)
    reg.decide(p["pending_change"]["id"], False, A)
    acts = [(e["actor"], e["action"]) for e in reg.audit_entries()]
    assert acts == [("steward-a", "source.propose_add"), ("steward-b", "source.approve"),
                    ("steward-b", "source.propose_edit"), ("steward-a", "source.reject")]
    assert reg.verify_audit()


def test_audit_log_is_append_only(reg):
    reg.propose_add(SRC, A)
    c = reg.conn()
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("UPDATE audit SET actor='mallory'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("DELETE FROM audit")
    c.close()
    assert len(reg.audit_entries()) == 1


def test_audit_chain_detects_tampering(reg):
    s = reg.propose_add(SRC, A)
    reg.decide(s["pending_change"]["id"], True, B)
    assert reg.verify_audit()
    c = reg.conn()
    c.execute("DROP TRIGGER audit_no_update")  # attacker with raw DB access
    c.execute("UPDATE audit SET actor='mallory' WHERE seq=1")
    c.commit()
    assert audit.verify_chain(c) is False
    c.close()
    assert reg.verify_audit() is False


def test_failed_change_writes_no_audit_row(reg):
    s = reg.propose_add(SRC, A)
    with pytest.raises(RegistryError):
        reg.decide(s["pending_change"]["id"], True, A)
    assert len(reg.audit_entries()) == 1
