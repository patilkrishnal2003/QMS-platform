from app import verifier as V

SRC = {
    "sop:SOP-DEV-001@3.0": {"type": "sop", "source_id": "sop:SOP-DEV-001", "version": "3.0", "status": "effective",
                            "effective": True, "link": "http://k/documents/SOP-DEV-001?version=3.0",
                            "passages": [{"locator": "5.1", "text": "Detection. The originator records the event within 24 hours of detection."},
                                         {"locator": "5.5", "text": "Impact. QA documents whether other lots are not affected."}]},
    "sop:SOP-DEV-001@2.0": {"type": "sop", "source_id": "sop:SOP-DEV-001", "version": "2.0", "status": "superseded",
                            "effective": False, "link": "x", "passages": [{"locator": "5.1", "text": "The originator recorded the event within 48 hours of detection."}]},
    "library:7@1": {"type": "library", "source_id": "library:7", "version": "1", "status": "final", "effective": True,
                    "content_kind": "full_text", "link": "http://l/library/7",
                    "lines": ["Heading", "The quality control unit shall approve or reject all components.", "Third line"], "passages": []},
    "library:8@1": {"type": "library", "source_id": "library:8", "version": "1", "status": "final", "effective": True,
                    "content_kind": "metadata_only", "link": "http://l/library/8", "lines": ["Title: Guidance X"], "passages": []},
    "req:211.192": {"type": "req", "source_id": "req:211.192", "version": None, "status": "map", "effective": None,
                    "link": "http://k/traceability/211.192",
                    "passages": [{"locator": "SOP-DEV-001#5.4", "text": "21 CFR 211.192 is implemented by SOP-DEV-001 section 5.4."}]},
}


def c(sid="sop:SOP-DEV-001", ver="3.0", loc="5.1", quote="within 24 hours of detection"):
    return {"source_id": sid, "version": ver, "locator": loc, "quote": quote}


def stmt(text="The originator must record the event within 24 hours of detection.", cites=None):
    return {"text": text, "kind": "procedure", "citations": [c()] if cites is None else cites}


def v(s, th=None):
    return V.verify_statement(s, SRC, th)


# ---- the three structural checks (never pass)
def test_valid_citation_is_verified_with_a_link_to_the_passage():
    r = v(stmt())
    assert r["verdict"] == "verified" and r["score"] == 1.0
    assert r["citations"][0]["status"] == "ok" and r["citations"][0]["link"] == "http://k/documents/SOP-DEV-001?version=3.0#5.1"


def test_unknown_source_is_invalid():
    r = v(stmt(cites=[c(sid="sop:SOP-NOPE")]))
    assert r["verdict"] == "invalid_citation" and r["reasons"] == ["source_not_found"]


def test_unknown_version_is_invalid():
    assert v(stmt(cites=[c(ver="9.9")]))["reasons"] == ["source_not_found"]


def test_unknown_section_is_invalid():
    assert v(stmt(cites=[c(loc="5.9")]))["reasons"] == ["locator_not_found"]


def test_fabricated_quote_is_invalid():
    assert v(stmt(cites=[c(quote="within 12 hours of detection")]))["reasons"] == ["quote_not_in_passage"]


def test_missing_version_is_invalid_except_for_requirement_links():
    assert v(stmt(cites=[c(ver="")]))["reasons"] == ["version_missing"]
    r = v({"text": "21 CFR 211.192 is implemented by SOP-DEV-001 section 5.4.",
           "citations": [c("req:211.192", None, "SOP-DEV-001#5.4", "is implemented by SOP-DEV-001 section 5.4")]})
    assert r["verdict"] == "verified"


def test_incomplete_citation_is_invalid():
    assert v(stmt(cites=[{"source_id": "sop:SOP-DEV-001", "version": "3.0", "locator": "5.1", "quote": ""}]))["reasons"] == ["incomplete_citation"]
    assert v(stmt(cites=[{"source_id": "", "version": "", "locator": "", "quote": ""}]))["verdict"] == "invalid_citation"


def test_no_citation_is_unsupported_never_dropped_or_passed():
    r = v(stmt(cites=[]))
    assert r["verdict"] == "unsupported" and r["reasons"] == ["no_citation"] and r["text"]
    out = V.verify([stmt(cites=[]), stmt()], SRC)
    assert [x["verdict"] for x in out["results"]] == ["unsupported", "verified"] and out["summary"]["flagged"] == 1


def test_one_bad_citation_next_to_a_good_one_still_flags_the_statement():
    r = v(stmt(cites=[c(), c(sid="sop:SOP-NOPE")]))
    assert r["verdict"] == "invalid_citation"
    assert [x["status"] for x in r["citations"]] == ["ok", "invalid"]


# ---- library line ranges
def test_library_line_ranges():
    ok = c("library:7", "1", "lines:2", "shall approve or reject all components")
    assert v({"text": "The quality control unit must approve or reject all components.", "citations": [ok]})["verdict"] == "verified"
    assert v(stmt(cites=[c("library:7", "1", "lines:9", "x")]))["reasons"] == ["locator_not_found"]
    assert v(stmt(cites=[c("library:7", "1", "lines:3-2", "x")]))["reasons"] == ["locator_not_found"]
    assert v(stmt(cites=[c("library:7", "1", "lines:0", "x")]))["reasons"] == ["locator_not_found"]
    span = c("library:7", "1", "lines:2-3", "Third line")
    assert v({"text": "Third line.", "citations": [span]})["citations"][0]["status"] == "ok"


# ---- plausibility (support score)
def test_unrelated_statement_is_mismatch():
    r = v(stmt(text="Fire extinguishers must be inspected every month."))
    assert r["verdict"] == "mismatch" and "passage_does_not_support_statement" in r["reasons"]


def test_number_not_in_passage_is_mismatch():
    r = v(stmt(text="The originator must record the event within 48 hours of detection."))
    assert r["verdict"] == "mismatch" and any(x.startswith("number_not_in_passage:48") for x in r["reasons"])


def test_number_words_and_digits_are_equivalent():
    assert V.tokenize("within two working days") == V.tokenize("within 2 working days")


def test_low_confidence_band():
    r = v(stmt(text="The originator records the detection and informs the director, plant manager and supervisor."))
    assert r["verdict"] == "low_confidence" and 0.35 <= r["score"] < 0.6


def test_negation_difference_is_flagged_low_confidence():
    s = {"type": "sop", "source_id": "sop:S", "version": "1", "status": "effective", "effective": True, "link": "l",
         "passages": [{"locator": "1", "text": "Operator error alone is not an acceptable root cause."}]}
    r = V.verify_statement({"text": "Operator error alone is an acceptable root cause.",
                            "citations": [c("sop:S", "1", "1", "alone is not an acceptable root cause")]}, {"sop:S@1": s})
    assert r["verdict"] == "low_confidence" and "negation_differs" in r["reasons"]


def test_thresholds_are_configurable_configuration():
    s = stmt(text="The originator records the detection and informs the director, plant manager and supervisor.")
    assert v(s)["verdict"] == "low_confidence"
    assert v(s, {"verified": 0.3, "low": 0.2})["verdict"] == "verified"


# ---- source state
def test_superseded_source_is_never_verified():
    r = v(stmt(text="The originator records the event within 48 hours of detection.",
               cites=[c(ver="2.0", quote="within 48 hours of detection")]))
    assert r["verdict"] == "low_confidence" and "source_not_effective" in r["reasons"]


def test_metadata_only_library_item_is_flagged():
    r = v({"text": "Guidance X.", "citations": [c("library:8", "1", "lines:1", "Guidance X")]})
    assert r["verdict"] == "low_confidence" and "source_is_metadata_only" in r["reasons"]


def test_quote_matching_is_tolerant_of_case_whitespace_and_ellipsis_but_not_of_changes():
    assert V.quote_in_passage("  WITHIN 24   hours of\ndetection ", "... within 24 hours of detection.")
    assert V.quote_in_passage("records the event ... of detection", "The originator records the event within 24 hours of detection.")
    assert not V.quote_in_passage("records the event ... after detection", "The originator records the event within 24 hours of detection.")
    assert not V.quote_in_passage("", "anything")


def test_malformed_statement_entries_are_kept_not_dropped():
    out = V.verify(["just a string", {"text": "x", "citations": "nope"}], SRC)
    assert len(out["results"]) == 2 and all(r["verdict"] == "unsupported" for r in out["results"])


# ---- naming the cited source (id / version) must not be penalised, real quantities still are
def test_correct_statement_naming_its_own_sop_and_version_is_not_downgraded():
    plain = v(stmt())
    named = v(stmt(text="SOP-DEV-001 version 3.0 requires the originator to record the event within 24 hours of detection."))
    assert named["verdict"] == "verified" and named["score"] >= 0.8, named
    assert not any(r.startswith("number_not_in_passage") for r in named["reasons"])
    assert plain["verdict"] == "verified"
    for variant in ("Per SOP-DEV-001 v3.0, the originator records the event within 24 hours of detection.",
                    "SOP DEV 001 (3.0): the originator records the event within 24 hours of detection.",
                    "According to sop-dev-001, version 3.0, the originator records the event within 24 hours of detection."):
        assert v(stmt(text=variant))["verdict"] == "verified", variant


def test_self_reference_exemption_does_not_hide_a_wrong_quantity():
    r = v(stmt(text="SOP-DEV-001 version 3.0 requires the originator to record the event within 48 hours of detection."))
    assert r["verdict"] == "mismatch" and any(x.startswith("number_not_in_passage:48") for x in r["reasons"])
    # a number equal to the version digits used as a quantity is still checked when it is not a version reference
    r = v(stmt(text="The originator records the event within 3 hours of detection."))
    assert r["verdict"] == "mismatch" and any(x.startswith("number_not_in_passage:3") for x in r["reasons"])


def test_the_live_run_statement_still_fails_on_meaning_but_not_on_its_own_ids():
    src = {"sop:SOP-DEV-001@3.0": {"type": "sop", "source_id": "sop:SOP-DEV-001", "version": "3.0", "status": "effective",
                                   "effective": True, "link": "L",
                                   "passages": [{"locator": "1", "text": "Purpose. This procedure defines how unplanned departures "
                                                 "from approved procedures (deviations) are reported, contained, classified, "
                                                 "investigated and closed at all Examplon sites."}]}}
    r = V.verify_statement({"text": "The SOP-DEV-001 version 3.0 requires deviations to be reported immediately upon discovery.",
                            "citations": [c(loc="1", quote="deviations) are reported, contained, classified")]}, src)
    assert r["verdict"] == "mismatch"
    assert not any(x.startswith("number_not_in_passage") for x in r["reasons"])      # the 001 / 3.0 reason is gone
    assert "passage_does_not_support_statement" in r["reasons"]


def test_statement_that_only_names_its_source_is_not_verified():
    r = v(stmt(text="SOP-DEV-001 version 3.0."))
    assert r["verdict"] == "mismatch" and "statement_only_names_its_source" in r["reasons"]


def test_strip_self_references_unit():
    cites = [{"source_id": "sop:SOP-DEV-001", "version": "3.0"}, {"source_id": "library:7", "version": "1"}]
    assert V.strip_self_references("SOP-DEV-001 v3.0 says 24 hours", cites) == "says 24 hours"
    assert V.strip_self_references("library 7 item, version 1, requires 7 days", cites).endswith("requires 7 days")
    assert "24" in V.strip_self_references("within 24 hours (v3.0)", cites)


# ---- a statement naming the very section it cites is not penalised for that number (found by the Phase 4 walkthrough)
def test_statement_naming_the_cited_section_is_not_downgraded_for_the_section_number():
    r = v(stmt(text="SOP-DEV-001 section 5.1 requires the originator to record the event within 24 hours of detection."))
    assert r["verdict"] == "verified" and not any(x.startswith("number_not_in_passage") for x in r["reasons"])
    assert v(stmt(text="Per section 5.1 the originator must record the event within 24 hours of detection."))["verdict"] == "verified"
    assert v(stmt(text="\u00a75.1: the originator records the event within 24 hours of detection."))["verdict"] == "verified"


def test_citing_a_different_section_number_or_a_wrong_quantity_is_still_caught():
    wrong_section = v(stmt(text="Section 6 requires the originator to record the event within 24 hours of detection."))
    assert wrong_section["verdict"] == "mismatch" and any(x.startswith("number_not_in_passage:6") for x in wrong_section["reasons"])
    wrong_qty = v(stmt(text="Section 5.1 requires the originator to record the event within 48 hours of detection."))
    assert wrong_qty["verdict"] == "mismatch" and any(x.startswith("number_not_in_passage:48") for x in wrong_qty["reasons"])


def test_requirement_link_locators_with_a_hash_are_handled():
    c = SRC["req:211.192"]["passages"][0]
    r = V.verify_statement({"text": "21 CFR 211.192 is implemented by SOP-DEV-001 section 5.4.",
                            "citations": [{"source_id": "req:211.192", "version": None, "locator": "SOP-DEV-001#5.4",
                                           "quote": "is implemented by SOP-DEV-001 section 5.4"}]}, SRC)
    assert r["verdict"] == "verified"
    assert V.strip_self_references("see section 5.4 and 5.45", [{"source_id": "x:y", "locator": "SOP-DEV-001#5.4"}]).endswith("and 5.45")
