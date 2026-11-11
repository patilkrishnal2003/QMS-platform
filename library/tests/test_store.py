import pytest

from app.store import LibraryError, Store, checksum, redline


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "lib.db"))


def payload(text="line one\nline two\n", **kw):
    base = {"source_url": "https://example.test/doc", "title": "Doc", "text": text,
            "retrieved_at": "2026-01-01T00:00:00+00:00", "document_date": "2026-01-01",
            "version_or_docket": "DOC-1", "status": "final", "legal_weight": "regulation",
            "auto_accept": False, "submitted_by": "steward-a"}
    return {**base, **kw}


# ---- checksum ----
def test_checksum_is_sha256_and_ignores_line_endings():
    assert checksum("a\nb") == checksum("a\r\nb")
    assert checksum("a") != checksum("b")
    assert len(checksum("x")) == 64


# ---- versioning ----
def test_new_item_is_queued_not_current(store):
    r = store.ingest(payload())
    assert r["result"] == "queued_new" and r["version_no"] == 1
    assert store.library() == []
    it = store.item(r["item_id"])
    assert it["checksum"] == checksum(it["text"]) and it["queue_state"] == "pending"
    assert it["retrieved_at"] and it["legal_weight"] == "regulation" and it["version_or_docket"] == "DOC-1"


def test_accept_makes_current_and_unchanged_refetch_is_noop(store):
    r = store.ingest(payload())
    store.accept(r["item_id"], "steward-b")
    assert [i["id"] for i in store.library()] == [r["item_id"]]
    again = store.ingest(payload())
    assert again["result"] == "unchanged" and again["item_id"] == r["item_id"]
    assert len(store.versions("https://example.test/doc")) == 1


def test_changed_content_supersedes_old_version_after_accept(store):
    v1 = store.ingest(payload())
    store.accept(v1["item_id"], "steward-b")
    v2 = store.ingest(payload("line one\nline two changed\n"))
    assert v2["result"] == "queued_changed" and v2["version_no"] == 2
    assert store.library()[0]["id"] == v1["item_id"]  # old stays current until accepted
    store.accept(v2["item_id"], "qa-1")
    cur = store.library()
    assert [i["id"] for i in cur] == [v2["item_id"]]
    vs = {v["version_no"]: v for v in store.versions("https://example.test/doc")}
    assert vs[1]["status"] == "superseded" and not vs[1]["is_current"]
    assert vs[2]["status"] == "final" and vs[2]["is_current"]


def test_duplicate_pending_is_not_requeued(store):
    store.ingest(payload())
    assert store.ingest(payload())["result"] == "already_queued"
    assert len(store.queue()) == 1


def test_validation(store):
    with pytest.raises(LibraryError):
        store.ingest(payload(status="bogus"))
    with pytest.raises(LibraryError):
        store.ingest(payload(legal_weight="whatever"))
    with pytest.raises(LibraryError):
        store.ingest(payload(text=""))


# ---- queue + redline ----
def test_redline_shows_added_and_removed_lines(store):
    v1 = store.ingest(payload("a\nb\nc\n"))
    store.accept(v1["item_id"], "steward-a")
    v2 = store.ingest(payload("a\nB changed\nc\nd\n"))
    rl = store.redline_for(v2["item_id"])
    assert rl["base_item_id"] == v1["item_id"] and (rl["added"], rl["removed"]) == (2, 1)
    assert "-b" in rl["unified"] and "+B changed" in rl["unified"] and "+d" in rl["unified"]
    assert {"op": "del", "text": "b"} in rl["lines"] and {"op": "add", "text": "d"} in rl["lines"]


def test_redline_for_first_version_is_all_additions():
    rl = redline(None, "x\ny")
    assert rl["added"] == 2 and rl["removed"] == 0


def test_reject_keeps_history_and_leaves_current_untouched(store):
    v1 = store.ingest(payload())
    store.accept(v1["item_id"], "steward-a")
    v2 = store.ingest(payload("different\n"))
    out = store.reject(v2["item_id"], "steward-b", "not relevant")
    assert out["queue_state"] == "rejected" and out["decision_note"] == "not relevant"
    assert store.library()[0]["id"] == v1["item_id"]
    assert store.queue() == [] and len(store.versions("https://example.test/doc")) == 2
    with pytest.raises(LibraryError):
        store.accept(v2["item_id"], "steward-a")  # already decided


def test_only_reviewers_may_decide(store):
    r = store.ingest(payload())
    with pytest.raises(LibraryError) as e:
        store.accept(r["item_id"], "viewer-1")
    assert e.value.status == 403
    with pytest.raises(LibraryError):
        store.reject(r["item_id"], "")


def test_stale_item_cannot_regress_current(store):
    v1 = store.ingest(payload("one\n"))
    v2 = store.ingest(payload("two\n"))
    store.accept(v2["item_id"], "steward-a")
    with pytest.raises(LibraryError) as e:
        store.accept(v1["item_id"], "steward-a")
    assert e.value.status == 409


# ---- auto-accept rule ----
def test_auto_accept_rule_accepts_new_and_changed_and_is_recorded(store):
    a = store.ingest(payload(auto_accept=True))
    assert a["result"] == "auto_accepted_new" and store.queue() == []
    b = store.ingest(payload("changed\n", auto_accept=True))
    assert b["result"] == "auto_accepted_changed"
    cur = store.library()[0]
    assert cur["id"] == b["item_id"] and cur["auto_accepted"] and cur["decided_by"] == "auto-accept-rule"
    assert store.versions("https://example.test/doc")[0]["status"] == "superseded"
    assert store.ingest(payload("changed\n", auto_accept=True))["result"] == "unchanged"


def test_without_auto_accept_changes_wait_in_queue(store):
    store.ingest(payload(auto_accept=False))
    assert len(store.queue()) == 1 and store.library() == []


# ---- content kind (full text vs metadata-only) ----
def test_content_kind_defaults_to_full_text_and_metadata_only_is_stored(store):
    a = store.ingest(payload())
    b = store.ingest(payload(source_url="https://example.test/meta", content_kind="metadata_only"))
    assert store.item(a["item_id"])["content_kind"] == "full_text"
    assert store.item(b["item_id"])["content_kind"] == "metadata_only"
    store.accept(b["item_id"], "steward-a")
    assert store.library()[0]["content_kind"] == "metadata_only"
    with pytest.raises(LibraryError):
        store.ingest(payload(content_kind="partial"))


def test_existing_db_without_content_kind_column_is_migrated(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, source_url TEXT NOT NULL, source_id INTEGER, fetched_url TEXT,"
              " version_no INTEGER NOT NULL, title TEXT, text TEXT NOT NULL, retrieved_at TEXT NOT NULL, document_date TEXT,"
              " version_or_docket TEXT, status TEXT NOT NULL, checksum TEXT NOT NULL, legal_weight TEXT NOT NULL,"
              " queue_state TEXT NOT NULL, is_current INTEGER NOT NULL DEFAULT 0, previous_item_id INTEGER, submitted_by TEXT,"
              " decided_by TEXT, decided_at TEXT, decision_note TEXT, auto_accepted INTEGER NOT NULL DEFAULT 0)")
    c.execute("INSERT INTO items(source_url,version_no,text,retrieved_at,status,checksum,legal_weight,queue_state)"
              " VALUES ('u',1,'t','2026-01-01','final','x','regulation','pending')")
    c.commit()
    c.close()
    st = Store(path)
    assert st.item(1)["content_kind"] == "full_text"


# ---- Phase 3 retrieval support: search, citable item, library version
def test_search_only_covers_current_accepted_items_and_ranks_by_relevance(store):
    a = store.ingest(payload("The quality control unit shall approve or reject all components.\nOther text\n",
                             source_url="https://example.test/a", title="QC unit"))
    b = store.ingest(payload("Unrelated gardening advice about tomatoes.\n", source_url="https://example.test/b", title="Garden"))
    assert store.search("quality control unit") == []                       # nothing accepted yet: pending is not searchable
    store.accept(a["item_id"], "steward-a")
    store.accept(b["item_id"], "steward-a")
    hits = store.search("quality control unit approve")
    assert [h["id"] for h in hits] == [a["item_id"]] and "text" not in hits[0] and hits[0]["snippet"]
    assert "text" in store.search("quality control", include_text=True)[0]
    assert store.search("tomatoes")[0]["id"] == b["item_id"]
    with pytest.raises(LibraryError):
        store.search("   ")
    # a superseded version is no longer found
    v2 = store.ingest(payload("Completely replaced text about audits.\n", source_url="https://example.test/a", title="QC unit"))
    store.accept(v2["item_id"], "steward-a")
    assert store.search("components") == [] and store.search("audits")[0]["id"] == v2["item_id"]


def test_accepted_item_is_citable_but_pending_and_rejected_are_not(store):
    a = store.ingest(payload())
    with pytest.raises(LibraryError) as e:
        store.accepted_item(a["item_id"])
    assert e.value.status == 404
    store.accept(a["item_id"], "steward-a")
    it = store.accepted_item(a["item_id"])
    assert it["text"] == "line one\nline two\n" and it["version_no"] == 1
    r = store.ingest(payload("rejected text\n"))
    store.reject(r["item_id"], "steward-b")
    with pytest.raises(LibraryError):
        store.accepted_item(r["item_id"])
    v2 = store.ingest(payload("newer\n"))
    store.accept(v2["item_id"], "steward-a")
    assert store.accepted_item(a["item_id"])["status"] == "superseded"          # old versions stay citable, flagged


def test_library_version_fingerprint_changes_with_content(store):
    assert store.library_version() == {"library_version": "lib-0-empty", "items": 0}
    a = store.ingest(payload())
    store.accept(a["item_id"], "steward-a")
    v1 = store.library_version()
    assert v1["items"] == 1 and v1["library_version"].startswith("lib-1-")
    assert store.library_version() == v1                                        # stable while nothing changes
    b = store.ingest(payload("changed\n"))
    store.accept(b["item_id"], "steward-a")
    assert store.library_version()["library_version"] != v1["library_version"]


def test_library_search_matches_word_forms(store):
    a = store.ingest(payload("The quality unit approves released components and rejects failed lots.\n",
                             source_url="https://example.test/q", title="QC"))
    store.accept(a["item_id"], "steward-a")
    assert store.search("approving rejected component")[0]["id"] == a["item_id"]
