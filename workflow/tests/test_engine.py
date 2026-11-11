"""Unit tests for the deviation drafting loop: every step's logic and every refusal. No network, no model."""
import sqlite3

import pytest

from app import rules, style
from app.engine import AI_SECTIONS, Engine, Store
from app.errors import WorkflowError

OWN, SME, QA, VIEW = ("initiator-1", "initiator"), ("sme-1", "sme"), ("qa-1", "qa"), ("viewer-1", "viewer")
OTHER = ("initiator-2", "initiator")


@pytest.fixture
def eng(tmp_path):
    t = iter(range(1, 10000))
    return Engine(Store(str(tmp_path / "w.db")), clock=lambda: f"2026-11-11T10:{next(t) // 60:02d}:{next(t) % 60:02d}Z")


def new(eng, **kw):
    fields = {"site": "SITE-RB", "product": "Examplamol 500 mg tablets", "lot_number": "EXA-26-001",
              "equipment_id": "TAB-PRESS-02", "detected_date": "2026-11-10", **kw}
    return eng.create(*OWN, "Compression force drifted high during the run; rejects rose.", fields)


def refused(code=None, status=None):
    return pytest.raises(WorkflowError)


def expect(code, fn, *a, **k):
    with pytest.raises(WorkflowError) as e:
        fn(*a, **k)
    assert e.value.code == code, (e.value.code, str(e.value))
    return e.value


def entry(value, verdict="verified", ok=True, reasoning="Because the sources say so."):
    return {"value": value, "reasoning": reasoning, "citations": [], "validity": {"ok": ok, "problems": [] if ok else ["bad"]},
            "verification": {"verdict": verdict, "score": 1.0, "reasons": []}}


def proposals(etype="equipment_failure", cls="major", et_flags=None, cls_flags=None):
    return {"event_type": {"entries": [entry(etype, **(et_flags or {}))]},
            "regulations": {"entries": [entry("211.192"), entry("211.68")]},
            "sops": {"entries": [entry("SOP-DEV-001"), entry("SOP-DEV-002")]},
            "classification": {"entries": [entry(cls, **(cls_flags or {}))]}}


AI_EVENT = {"call_id": "c1", "model": "qwen2.5:14b-instruct", "digest": "d", "config_version": "1.4.0"}


def at_questions(eng, p=None, **kw):
    rec = new(eng, **kw)
    rid = rec["id"]
    eng.store_proposals(rid, *OWN, proposals(**(p or {})), AI_EVENT)
    for item in ("event_type", "regulations", "sops", "classification"):
        eng.decide_proposal(rid, *OWN, item, "confirm")
    eng.advance(rid, *OWN, "questions")
    return rid


ANSWERS = {"event_date": "2026-11-09", "reported_by": "A. Operator", "immediate_action": "Press stopped; lot quarantined.",
           "equipment_status": "Tagged out.", "capa_decision": "CAPA to be raised.", "recurrence_check": "No recurrence in 24 months."}


def answer_all(eng, rid, skip=()):
    for k, v in ANSWERS.items():
        if k not in skip:
            eng.answer(rid, *OWN, k, "answer", v)


def decide_all(eng, rid, impact="no_impact"):
    eng.decide(rid, *OWN, "root_cause", "Calibration interval had been extended.", "Calibration records reviewed.")
    eng.decide(rid, *OWN, "product_impact", impact, "Sampled lot met specification.")
    eng.decide(rid, *OWN, "reportability", "not_reportable", "No distribution occurred.")


def at_draft(eng, **kw):
    rid = at_questions(eng, **kw)
    eng.waive_gap(rid, *OWN, "No AI gap questions needed for this test.")
    answer_all(eng, rid)
    decide_all(eng, rid)
    eng.advance(rid, *OWN, "checklist")
    eng.advance(rid, *OWN, "draft")
    return rid


def ai_sections(n=2, **kw):
    s = {sec: [] for sec in AI_SECTIONS}
    for i in range(n):
        s["event_summary"].append({"text": f"Compression force rose above the set point ({i}).", "citations": [{"source_id": "answer:WF-0001:description"}],
                                   "verification": {"verdict": "verified", "score": 1.0, "reasons": []}, **kw})
    return s


# ================================================================== GW-01 intake
def test_create_needs_a_description_and_the_right_role(eng):
    expect("validation", eng.create, *OWN, "  ", {})
    for who in (SME, VIEW):
        expect("role", eng.create, *who, "something happened", {})
    expect("validation", eng.create, *OWN, "x", {"colour": "red"})
    expect("validation", eng.create, *OWN, "x", {"event_type": "alien_abduction"})
    assert eng.create(*QA, "qa may start a record too", {})["created_by"] == "qa-1"


def test_intake_stores_only_what_the_person_gave_and_starts_at_proposals(eng):
    rec = eng.create(*OWN, "Tablet press force drifted.", {"site": "SITE-RB", "event_type": "equipment_failure"})
    assert rec["status"] == "in_progress" and rec["step"] == "proposals" and rec["id"] == "WF-0001"
    assert set(rec["answers"]) == {"description", "site"}                       # nothing inferred or filled in
    assert rec["intake_event_type"] == "equipment_failure" and "event_type" not in rec["decisions"]   # a guess, not a decision
    assert rec["answers"]["site"]["by"] == "initiator-1" and rec["answers"]["site"]["revision"] == 1
    assert rec["next_required"] == ["request the AI proposals (GW-02)"]
    assert eng.create(*OWN, "second", {})["id"] == "WF-0002"


def test_missing_intake_fields_become_questions_not_blanks(eng):
    rid = eng.create(*OWN, "Something odd.", {})["id"]
    st = {i["key"]: i["status"] for i in eng.checklist_view(rid)["items"]}
    assert st["description"] == "answered" and all(st[k] == "missing" for k in ("site", "product", "lot_number", "equipment_id", "detected_date"))


# ================================================================== GW-02 proposals + explicit decisions
def test_proposals_are_stored_but_nothing_is_decided_or_advanced(eng):
    rid = new(eng)["id"]
    v = eng.store_proposals(rid, *OWN, proposals(), AI_EVENT)
    assert v["proposals"]["generated"] and v["proposals"]["decided"] == {} and v["decisions"] == {} and v["step"] == "proposals"
    assert v["next_required"] == [f"confirm or change the proposed {i}" for i in ("event_type", "regulations", "sops", "classification")]


def test_only_the_initiator_requests_proposals_and_only_at_step_proposals(eng):
    rid = new(eng)["id"]
    expect("not_owner", eng.store_proposals, rid, *QA, proposals(), AI_EVENT)
    expect("validation", eng.store_proposals, rid, *OWN, {"event_type": {}}, AI_EVENT)
    eng.store_proposals(rid, *OWN, proposals(), AI_EVENT)
    q = at_questions(eng)
    expect("step_order", eng.store_proposals, q, *OWN, proposals(), AI_EVENT)


def test_each_proposal_needs_its_own_explicit_decision(eng):
    rid = new(eng)["id"]
    expect("step_order", eng.decide_proposal, rid, *OWN, "event_type", "confirm")          # nothing generated yet
    eng.store_proposals(rid, *OWN, proposals(), AI_EVENT)
    v = eng.decide_proposal(rid, *OWN, "event_type", "confirm")
    assert v["decisions"]["event_type"]["value"] == "equipment_failure" and v["decisions"]["event_type"]["via"] == "ai_proposal_confirmed"
    assert v["decisions"]["event_type"]["by"] == "initiator-1" and v["decisions"]["event_type"]["at"]
    assert list(v["proposals"]["decided"]) == ["event_type"] and v["step"] == "proposals"
    assert "classification" not in v["decisions"]                                          # confirming one confirms only that one


def test_non_owners_cannot_decide_proposals(eng):
    rid = new(eng)["id"]
    eng.store_proposals(rid, *OWN, proposals(), AI_EVENT)
    for who in (OTHER, QA, SME, VIEW):
        expect("not_owner", eng.decide_proposal, rid, *who, "event_type", "confirm")


def test_flagged_proposal_needs_an_acknowledgement_or_a_change(eng):
    rid = new(eng)["id"]
    eng.store_proposals(rid, *OWN, proposals(cls_flags={"verdict": "mismatch"}), AI_EVENT)
    e = expect("flag_needs_ack", eng.decide_proposal, rid, *OWN, "classification", "confirm")
    assert "acknowledge flagged proposal" in e.required[0]
    v = eng.decide_proposal(rid, *OWN, "classification", "confirm", ack_flag=True)
    assert v["decisions"]["classification"]["ack_flag"] is True
    bad = proposals(et_flags={"ok": False})
    rid2 = new(eng)["id"]
    eng.store_proposals(rid2, *OWN, bad, AI_EVENT)
    expect("flag_needs_ack", eng.decide_proposal, rid2, *OWN, "event_type", "confirm")      # failed validity is a flag too


def test_changing_a_proposal_needs_a_valid_value_and_a_rationale(eng):
    rid = new(eng)["id"]
    eng.store_proposals(rid, *OWN, proposals(), AI_EVENT)
    expect("validation", eng.decide_proposal, rid, *OWN, "classification", "change", None, "x")
    expect("validation", eng.decide_proposal, rid, *OWN, "classification", "change", "catastrophic", "x")
    expect("validation", eng.decide_proposal, rid, *OWN, "classification", "change", "critical", "")
    v = eng.decide_proposal(rid, *OWN, "classification", "change", "critical", "Patient-safety impact is possible.")
    assert v["decisions"]["classification"]["value"] == "critical" and v["decisions"]["classification"]["via"] == "ai_proposal_changed"
    v = eng.decide_proposal(rid, *OWN, "regulations", "change", ["211.192"], "Only this one applies.")
    assert v["decisions"]["applicable_regulations"]["value"] == ["211.192"]
    expect("validation", eng.decide_proposal, rid, *OWN, "sops", "change", "SOP-DEV-001", "must be a list")
    expect("validation", eng.decide_proposal, rid, *OWN, "bogus", "confirm")
    expect("validation", eng.decide_proposal, rid, *OWN, "event_type", "maybe")


def test_empty_proposal_cannot_be_confirmed(eng):
    rid = new(eng)["id"]
    items = proposals()
    items["sops"] = {"entries": []}
    eng.store_proposals(rid, *OWN, items, AI_EVENT)
    expect("nothing_to_confirm", eng.decide_proposal, rid, *OWN, "sops", "confirm")
    assert eng.decide_proposal(rid, *OWN, "sops", "change", ["SOP-DEV-001"], "AI found none; I chose this.")["decisions"]["applicable_sops"]["value"] == ["SOP-DEV-001"]


def test_cannot_advance_to_questions_until_every_proposal_is_decided(eng):
    rid = new(eng)["id"]
    e = expect("guard", eng.advance, rid, *OWN, "questions")
    assert "generate the AI proposals (GW-02)" in e.required
    eng.store_proposals(rid, *OWN, proposals(), AI_EVENT)
    eng.decide_proposal(rid, *OWN, "event_type", "confirm")
    e = expect("guard", eng.advance, rid, *OWN, "questions")
    assert e.required == ["confirm or change the proposed regulations", "confirm or change the proposed sops",
                          "confirm or change the proposed classification"]
    for i in ("regulations", "sops", "classification"):
        eng.decide_proposal(rid, *OWN, i, "confirm")
    assert eng.get(rid)["next_required"] == ["advance to step 'questions'"]
    assert eng.get(rid)["step"] == "proposals"                                              # all decided, STILL not advanced
    assert eng.advance(rid, *OWN, "questions")["step"] == "questions"


def test_proposals_cannot_be_regenerated_once_a_decision_exists(eng):
    rid = new(eng)["id"]
    eng.store_proposals(rid, *OWN, proposals(), AI_EVENT)
    eng.store_proposals(rid, *OWN, proposals(cls="minor"), {**AI_EVENT, "call_id": "c2"})   # fine while undecided
    eng.decide_proposal(rid, *OWN, "event_type", "confirm")
    expect("already_decided", eng.store_proposals, rid, *OWN, proposals(), AI_EVENT)


# ================================================================== step order (GW-09: never skip)
def test_steps_cannot_be_skipped_or_reversed_or_taken_by_others(eng):
    rid = new(eng)["id"]
    for to in ("checklist", "draft"):
        e = expect("skip", eng.advance, rid, *OWN, to)
        assert e.required and "request the AI proposals" in e.required[0]
    expect("validation", eng.advance, rid, *OWN, "ready")                                    # 'ready' is its own explicit action
    expect("validation", eng.advance, rid, *OWN, "proposals")
    expect("not_owner", eng.advance, rid, *QA, "questions")
    q = at_questions(eng)
    expect("skip", eng.advance, q, *OWN, "draft")
    expect("skip", eng.advance, q, *OWN, "questions")                                        # no re-advance / going back


def test_other_actions_are_refused_out_of_order(eng):
    rid = new(eng)["id"]
    expect("step_order", eng.store_gap_questions, rid, *OWN, [], AI_EVENT)
    expect("step_order", eng.decide, rid, *OWN, "root_cause", "x", "y")
    expect("step_order", eng.store_draft, rid, *OWN, ai_sections(), AI_EVENT)
    expect("step_order", eng.decide_statement, rid, *OWN, "a1-1", "accept")
    e = expect("not_ready", eng.mark_ready, rid, *OWN)
    assert any("step 'draft'" in r for r in e.required)


# ================================================================== GW-03/04 questions + adaptation
def test_questions_state_what_they_satisfy_and_why(eng):
    rid = at_questions(eng)
    qs = {q["id"]: q for q in eng.get(rid)["questions"]}
    q = qs["immediate_action"]
    assert q["source"] == "regulatory" and q["why"] and {"type": "requirement", "ref": "211.100"} in q["satisfies"]
    assert qs["reported_by"]["source"] == "sop_form" and qs["reported_by"]["satisfies"][0]["type"] == "sop"
    assert all(q["why"] and q["satisfies"] for q in qs.values())
    assert "event_type" not in qs and "classification" not in qs                              # decided via proposals, not asked again


def test_questions_adapt_to_earlier_answers(eng):
    rid = at_questions(eng)
    ids = lambda: {q["id"] for q in eng.get(rid)["questions"]}
    assert "equipment_status" in ids()                                                       # event type is equipment_failure
    assert not ids() & {"lot_disposition", "affected_lots", "notification_plan", "patient_safety_assessment", "oos_phase1_review"}
    eng.decide(rid, *OWN, "product_impact", "potential_impact", "Possible effect on tablet weight.")
    assert {"lot_disposition", "affected_lots"} <= ids()
    eng.decide(rid, *OWN, "reportability", "potentially_reportable", "Needs assessment.")
    assert "notification_plan" in ids()
    eng.decide_proposal(rid, *OWN, "classification", "change", "critical", "Patient impact cannot be excluded.")
    assert "patient_safety_assessment" in ids()
    eng.decide(rid, *OWN, "product_impact", "no_impact", "Re-assessed: no effect.")
    assert not ids() & {"lot_disposition", "affected_lots"}
    hidden = {h["key"]: h["reason"] for h in eng.checklist_view(rid)["adapted_out"]}
    assert "lot_disposition" in hidden and "product_impact is no_impact" in hidden["lot_disposition"]   # hidden WITH a reason


def test_event_type_drives_its_own_questions(eng):
    rid = at_questions(eng)
    eng.decide_proposal(rid, *OWN, "event_type", "change", "laboratory_oos", "It was a lab result.")
    ids = {q["id"] for q in eng.get(rid)["questions"]}
    assert "oos_phase1_review" in ids and "equipment_status" not in ids


def test_unknown_and_to_be_determined_are_valid_answers_carried_forward(eng):
    rid = at_questions(eng)
    eng.answer(rid, *OWN, "reported_by", "unknown")
    eng.answer(rid, *OWN, "event_date", "tbd", note="check the batch record")
    cl = {i["key"]: i for i in eng.checklist_view(rid)["items"]}
    assert cl["reported_by"]["status"] == "tbd" and cl["reported_by"]["value"] == "unknown"
    assert cl["event_date"]["status"] == "tbd" and cl["event_date"]["value"] == "to be determined"
    expect("validation", eng.answer, rid, *OWN, "capa_decision", "answer", "   ")
    expect("validation", eng.answer, rid, *OWN, "capa_decision", "maybe", "x")


def test_answers_keep_revision_history_and_are_attributed(eng):
    rid = at_questions(eng)
    eng.answer(rid, *OWN, "immediate_action", "answer", "Stopped the press.")
    eng.assign(rid, *OWN, "immediate_action", "sme-1", "sme")
    v = eng.answer(rid, *SME, "immediate_action", "answer", "Stopped the press and quarantined lot EXA-26-001.")
    a = v["answers"]["immediate_action"]
    assert a["revision"] == 2 and a["by"] == "sme-1"
    assert eng.store.load(rid)["answers"]["immediate_action"]["history"][0]["by"] == "initiator-1"   # the earlier answer is kept


def test_decision_keys_and_unknown_keys_cannot_be_answered_as_questions(eng):
    rid = at_questions(eng)
    expect("is_decision", eng.answer, rid, *OWN, "root_cause", "answer", "x")
    expect("is_decision", eng.answer, rid, *OWN, "classification", "answer", "x")
    expect("not_found", eng.answer, rid, *OWN, "no_such_question", "answer", "x")
    expect("not_found", eng.answer, rid, *OWN, "lot_disposition", "answer", "x")             # not applicable right now


def test_assignment_lets_only_the_assignee_answer(eng):
    rid = at_questions(eng)
    expect("not_permitted", eng.answer, rid, *SME, "immediate_action", "answer", "I was not assigned")
    expect("not_owner", eng.assign, rid, *QA, "immediate_action", "sme-1", "sme")
    expect("validation", eng.assign, rid, *OWN, "immediate_action", "viewer-1", "viewer")
    expect("not_found", eng.assign, rid, *OWN, "nope", "sme-1", "sme")
    v = eng.assign(rid, *OWN, "immediate_action", "sme-1", "sme")
    q = next(q for q in v["questions"] if q["id"] == "immediate_action")
    assert q["assigned_to"] == "sme-1"
    assert eng.answer(rid, *SME, "immediate_action", "answer", "Done.")["answers"]["immediate_action"]["by"] == "sme-1"
    expect("not_permitted", eng.answer, rid, *SME, "reported_by", "answer", "not mine")       # only the assigned question
    assert eng.answer(rid, *QA, "reported_by", "answer", "A. Operator")["answers"]["reported_by"]["by"] == "qa-1"   # oversight may answer


def test_evidence_is_a_placeholder_reference_with_attribution(eng):
    rid = at_questions(eng)
    expect("validation", eng.attach_evidence, rid, *OWN, "immediate_action", "  ")
    expect("not_permitted", eng.attach_evidence, rid, *SME, "immediate_action", "photo.jpg")
    v = eng.attach_evidence(rid, *OWN, "immediate_action", "press_display.jpg", "photo of the force display")
    ev = next(q for q in v["questions"] if q["id"] == "immediate_action")["evidence"][0]
    assert ev["placeholder"] is True and ev["ref"].startswith("placeholder://WF-0001/immediate_action/") and ev["by"] == "initiator-1"
    expect("not_found", eng.attach_evidence, rid, *OWN, "nope", "x")


def test_gap_questions_have_to_be_generated_or_waived_with_a_reason(eng):
    rid = at_questions(eng)
    e = expect("guard", eng.advance, rid, *OWN, "checklist")
    assert e.required == ["generate the gap questions, or waive them with a justification (GW-03)"]
    expect("validation", eng.waive_gap, rid, *OWN, " ")
    items = [{"text": "Was the force sensor calibrated within its interval?", "why": "211.68 requires equipment to be controlled.",
              "satisfies": [{"source_id": "req:211.68"}], "verification": {"verdict": "verified"}},
             {"text": "Which operator was on the press?", "why": "Training status.", "satisfies": [], "verification": {"verdict": "unsupported"}}]
    v = eng.store_gap_questions(rid, *OWN, items, AI_EVENT)
    gq = [q for q in v["questions"] if q["source"] == "gap"]
    assert [q["id"] for q in gq] == ["g1", "g2"] and gq[0]["why"] and gq[1]["verification"]["verdict"] == "unsupported"
    expect("already_generated", eng.store_gap_questions, rid, *OWN, items, AI_EVENT)
    assert eng.advance(rid, *OWN, "checklist")["step"] == "checklist"                       # generated, so allowed (missing items show in the checklist)


def test_gap_questions_are_required_checklist_items_unless_dismissed_with_a_reason(eng):
    rid = at_questions(eng)
    eng.store_gap_questions(rid, *OWN, [{"text": "Extra?", "why": "w", "satisfies": [], "verification": {"verdict": "verified"}}], AI_EVENT)
    assert "g1" in eng.checklist_view(rid)["missing"]
    expect("validation", eng.dismiss_gap_question, rid, *OWN, "g1", "")
    expect("not_found", eng.dismiss_gap_question, rid, *OWN, "g9", "x")
    eng.dismiss_gap_question(rid, *OWN, "g1", "Not relevant to this event.")
    assert "g1" not in eng.checklist_view(rid)["missing"]


def test_gap_waiver_is_recorded_with_who_when_and_why(eng):
    rid = at_questions(eng)
    eng.waive_gap(rid, *OWN, "AI model unavailable.")
    w = eng.get(rid)["gap"]["waived"]
    assert w["by"] == "initiator-1" and w["justification"] == "AI model unavailable." and w["at"]


# ================================================================== GW-05 checklist
def test_checklist_statuses_and_gating(eng):
    rid = at_questions(eng)
    eng.waive_gap(rid, *OWN, "n/a")
    cl = eng.checklist_view(rid)
    assert not cl["complete_for_draft"] and "reported_by" in cl["missing"] and "root_cause" in cl["missing"]
    assert {i["status"] for i in cl["items"]} >= {"answered", "missing"}
    ct = eng.advance(rid, *OWN, "checklist")                                                # showing the checklist is a step
    assert ct["checklist_shown"]["missing"] and ct["step"] == "checklist"
    e = expect("guard", eng.advance, rid, *OWN, "draft")
    assert any("answer 'reported_by' (missing)" in r for r in e.required) and any("root_cause" in r for r in e.required)
    answer_all(eng, rid)
    decide_all(eng, rid)
    assert eng.checklist_view(rid)["complete_for_draft"]
    assert eng.advance(rid, *OWN, "draft")["step"] == "draft"


def test_not_applicable_needs_a_justification_and_counts_as_resolved(eng):
    rid = at_questions(eng)
    expect("validation", eng.mark_not_applicable, rid, *OWN, "equipment_status", "")
    expect("not_owner", eng.mark_not_applicable, rid, *QA, "equipment_status", "x")
    expect("validation", eng.mark_not_applicable, rid, *OWN, "classification", "x")
    expect("not_found", eng.mark_not_applicable, rid, *OWN, "lot_disposition", "x")          # not on the checklist right now
    eng.mark_not_applicable(rid, *OWN, "equipment_status", "Equipment was not involved; sensor only.")
    it = next(i for i in eng.checklist_view(rid)["items"] if i["key"] == "equipment_status")
    assert it["status"] == "not_applicable" and it["justification"] and it["by"] == "initiator-1"
    assert "equipment_status" not in eng.checklist_view(rid)["missing"]


def test_checklist_counts_and_every_item_names_its_requirement(eng):
    rid = at_questions(eng)
    cl = eng.checklist_view(rid)
    assert sum(cl["counts"].values()) == len(cl["items"])
    assert all(i["satisfies"] and i["why"] for i in cl["items"])


# ================================================================== AI-05 decisions + TBD acceptance
def test_human_decisions_need_value_rationale_and_the_right_person(eng):
    rid = at_questions(eng)
    expect("validation", eng.decide, rid, *OWN, "product_impact", "", "r")
    expect("validation", eng.decide, rid, *OWN, "product_impact", "no_impact", "")
    expect("validation", eng.decide, rid, *OWN, "product_impact", "maybe_impact", "r")
    expect("validation", eng.decide, rid, *OWN, "classification", "major", "r")               # classification goes through the proposal step
    expect("not_permitted", eng.decide, rid, *SME, "root_cause", "x", "y")
    expect("not_permitted", eng.decide, rid, *VIEW, "root_cause", "x", "y")
    v = eng.decide(rid, *QA, "root_cause", "Calibration interval extended.", "Reviewed records.")   # oversight may decide
    d = v["decisions"]["root_cause"]
    assert d["by"] == "qa-1" and d["at"] and d["rationale"] == "Reviewed records." and d["via"] == "human"


def test_tbd_decision_then_explicit_acceptance_then_voided_by_a_new_decision(eng):
    rid = at_draft(eng)
    v = eng.decide(rid, *OWN, "root_cause", tbd=True)
    assert v["checklist_summary"]["tbd_unaccepted"] == ["root_cause"]
    e = expect("not_ready", eng.mark_ready, rid, *OWN)
    assert any("root_cause" in r and "to be determined at investigation" in r for r in e.required)
    expect("validation", eng.accept_tbd, rid, *OWN, "root_cause", " ")
    expect("not_tbd", eng.accept_tbd, rid, *OWN, "reportability", "x")
    expect("not_permitted", eng.accept_tbd, rid, *SME, "root_cause", "x")
    v = eng.accept_tbd(rid, *QA, "root_cause", "Root cause will be set by the investigation team.")
    acc = v["tbd_accepted"]["root_cause"]
    assert acc["by"] == "qa-1" and acc["at"] and acc["justification"]
    assert next(i for i in eng.checklist_view(rid)["items"] if i["key"] == "root_cause")["status"] == "tbd_accepted"
    eng.decide(rid, *OWN, "root_cause", "Found: calibration drift.", "Investigation result.")
    assert "root_cause" not in eng.get(rid)["tbd_accepted"]                                  # a new answer voids the acceptance


def test_tbd_answers_need_acceptance_too(eng):
    rid = at_questions(eng)
    eng.answer(rid, *OWN, "capa_decision", "tbd")
    assert "capa_decision" in eng.get(rid)["checklist_summary"]["tbd_unaccepted"]
    eng.accept_tbd(rid, *OWN, "capa_decision", "Depends on the root cause.")
    assert "capa_decision" not in eng.get(rid)["checklist_summary"]["tbd_unaccepted"]
    eng.answer(rid, *OWN, "capa_decision", "answer", "No CAPA: correction only.")
    assert eng.get(rid)["tbd_accepted"] == {}


# ================================================================== draft (GW-06, AI-13, DI-05)
def test_draft_is_only_generated_at_step_draft_and_all_ai_text_starts_pending(eng):
    rid = at_questions(eng)
    expect("step_order", eng.store_draft, rid, *OWN, ai_sections(), AI_EVENT)
    rid = at_draft(eng)
    expect("not_owner", eng.store_draft, rid, *QA, ai_sections(), AI_EVENT)
    v = eng.store_draft(rid, *OWN, ai_sections(3), AI_EVENT)
    assert v["draft"]["version"] == 1 and v["draft"]["pending_ai_statements"] == 3 and v["step"] == "draft"
    d = eng.draft_view(rid)
    stmts = [s for sec in d["sections"] for s in sec["statements"]]
    ai = [s for s in stmts if s["author"]["type"] == "ai"]
    human = [s for s in stmts if s["author"]["type"] == "human"]
    assert len(ai) == 3 and all(s["status"] == "pending" and s["author"]["model"] == "qwen2.5:14b-instruct" for s in ai)
    assert human and all(s["status"] == "human_entered" and s["author"]["user"] for s in human)
    assert "AI" in d["legend"] and "NOT part of the record" in d["legend"]


def test_every_human_statement_links_to_the_answer_it_came_from(eng):
    rid = at_draft(eng)
    eng.store_draft(rid, *OWN, ai_sections(1), AI_EVENT)
    stmts = [s for sec in eng.draft_view(rid)["sections"] for s in sec["statements"] if s["author"]["type"] == "human"]
    assert all(s["sources"] and s["sources"][0]["ref"].startswith("answer:WF-0001:") and s["sources"][0]["link"].startswith("/deviations/WF-0001/answers/")
               for s in stmts)
    rc = next(s for s in stmts if s["element"] == "root_cause")
    assert rc["text"] == "Root cause: Calibration interval had been extended.. Basis: Calibration records reviewed." or rc["text"].startswith("Root cause:")
    assert rc["author"]["user"] == "initiator-1"


def test_draft_sections_follow_the_template_order(eng):
    rid = at_draft(eng)
    eng.store_draft(rid, *OWN, ai_sections(1), AI_EVENT)
    titles = [s["title"] for s in eng.draft_view(rid)["sections"]]
    assert titles[0] == "Identification" and "Description of event" in titles and "Investigation and root cause" in titles
    assert titles.index("Identification") < titles.index("Description of event") < titles.index("Investigation and root cause")


def test_flagged_ai_statements_need_acknowledgement_to_accept(eng):
    rid = at_draft(eng)
    s = ai_sections(1)
    s["event_summary"][0]["verification"] = {"verdict": "mismatch", "score": 0.2, "reasons": ["passage_does_not_support_statement"]}
    eng.store_draft(rid, *OWN, s, AI_EVENT)
    sid = next(x["id"] for sec in eng.draft_view(rid)["sections"] for x in sec["statements"] if x["author"]["type"] == "ai")
    e = expect("flag_needs_ack", eng.decide_statement, rid, *OWN, sid, "accept")
    assert "mismatch" in str(e)
    v = eng.decide_statement(rid, *OWN, sid, "accept", ack_flag=True)
    st = next(x for sec in eng.draft_view(rid)["sections"] for x in sec["statements"] if x["id"] == sid)
    assert st["status"] == "accepted" and st["acknowledged"] is True and st["decided_by"] == "initiator-1" and st["author"]["type"] == "ai"
    assert v["draft"]["pending_ai_statements"] == 0


def test_verified_ai_statement_can_be_accepted_without_an_acknowledgement_and_stays_attributed_to_the_ai(eng):
    rid = at_draft(eng)
    eng.store_draft(rid, *OWN, ai_sections(1), AI_EVENT)
    sid = next(x["id"] for sec in eng.draft_view(rid)["sections"] for x in sec["statements"] if x["author"]["type"] == "ai")
    eng.decide_statement(rid, *OWN, sid, "accept")
    st = next(x for sec in eng.draft_view(rid)["sections"] for x in sec["statements"] if x["id"] == sid)
    assert st["status"] == "accepted" and st["author"]["type"] == "ai" and not st.get("acknowledged")     # DI-05: still the AI's text
    expect("not_pending", eng.decide_statement, rid, *OWN, sid, "accept")


def test_editing_an_ai_statement_makes_it_the_persons_and_keeps_the_ai_original(eng):
    rid = at_draft(eng)
    eng.store_draft(rid, *OWN, ai_sections(1), AI_EVENT)
    sid = next(x["id"] for sec in eng.draft_view(rid)["sections"] for x in sec["statements"] if x["author"]["type"] == "ai")
    expect("validation", eng.decide_statement, rid, *OWN, sid, "edit", " ")
    eng.decide_statement(rid, *OWN, sid, "edit", "Compression force exceeded the set point at 14:10.")
    st = next(x for sec in eng.draft_view(rid)["sections"] for x in sec["statements"] if x["id"] == sid)
    assert st["status"] == "edited" and st["author"] == {"type": "human", "user": "initiator-1", "derived_from_ai": True}
    assert st["ai_original"]["author"]["type"] == "ai" and st["ai_original"]["text"].startswith("Compression force rose")


def test_rejected_ai_statements_are_listed_not_hidden(eng):
    rid = at_draft(eng)
    eng.store_draft(rid, *OWN, ai_sections(2), AI_EVENT)
    sid = next(x["id"] for sec in eng.draft_view(rid)["sections"] for x in sec["statements"] if x["author"]["type"] == "ai")
    eng.decide_statement(rid, *OWN, sid, "reject")
    d = eng.draft_view(rid)
    assert [r["id"] for r in d["rejected"]] == [sid] and d["rejected"][0]["status"] == "rejected"


def test_statement_decisions_are_owner_only(eng):
    rid = at_draft(eng)
    eng.store_draft(rid, *OWN, ai_sections(1), AI_EVENT)
    sid = next(x["id"] for sec in eng.draft_view(rid)["sections"] for x in sec["statements"] if x["author"]["type"] == "ai")
    for who in (QA, SME, OTHER):
        expect("not_owner", eng.decide_statement, rid, *who, sid, "accept")
    expect("not_found", eng.decide_statement, rid, *OWN, "zzz", "accept")


def test_writing_standard_flags_speculation_and_blame_language(eng):
    flags = style.check("The operator was careless and probably ignored the alarm.")
    assert {f["type"] for f in flags} == {"blame", "speculation"} and {"careless", "probably"} <= {f["term"] for f in flags}
    assert style.check("Compression force rose to 14.2 kN at 14:10. The press was stopped.") == []
    assert [f["term"] for f in style.check("This was operator error.")] == ["operator error"]
    assert style.check("The sample was 'very' small") and not style.check("The average was reviewed")     # whole-word matching
    rid = at_draft(eng)
    s = ai_sections(1)
    s["event_summary"][0]["text"] = "The operator was careless."
    eng.store_draft(rid, *OWN, s, AI_EVENT)
    sid = next(x["id"] for sec in eng.draft_view(rid)["sections"] for x in sec["statements"] if x["author"]["type"] == "ai")
    st = next(x for sec in eng.draft_view(rid)["sections"] for x in sec["statements"] if x["id"] == sid)
    assert st["needs_ack"] and st["style_flags"][0]["type"] == "blame"                       # AI-13: flagged even if the citation is fine
    expect("flag_needs_ack", eng.decide_statement, rid, *OWN, sid, "accept")


def test_changing_an_answer_makes_the_draft_stale_and_blocks_everything_until_regenerated(eng):
    rid = at_draft(eng)
    eng.store_draft(rid, *OWN, ai_sections(1), AI_EVENT)
    sid = next(x["id"] for sec in eng.draft_view(rid)["sections"] for x in sec["statements"] if x["author"]["type"] == "ai")
    v = eng.answer(rid, *OWN, "immediate_action", "answer", "Press stopped; lot quarantined; QA informed.")
    assert v["draft"]["stale"] is True
    expect("stale", eng.decide_statement, rid, *OWN, sid, "accept")
    e = expect("not_ready", eng.mark_ready, rid, *OWN)
    assert any("stale" in r for r in e.required)
    v = eng.store_draft(rid, *OWN, ai_sections(1), {**AI_EVENT, "call_id": "c3"})
    assert v["draft"]["version"] == 2 and v["draft"]["stale"] is False and v["draft"]["pending_ai_statements"] == 1
    assert eng.store.load(rid)["draft_history"][0]["version"] == 1                           # the old version is kept, read-only
    expect("draft_current", eng.store_draft, rid, *OWN, ai_sections(1), AI_EVENT)


def test_changing_a_decision_after_the_draft_also_makes_it_stale(eng):
    rid = at_draft(eng)
    eng.store_draft(rid, *OWN, ai_sections(0), AI_EVENT)
    eng.decide_proposal(rid, *OWN, "classification", "change", "critical", "Re-classified after review.")
    assert eng.get(rid)["draft"]["stale"]


# ================================================================== GW-09 mark ready / cancel / freeze
def finish(eng, rid):
    for sec in eng.draft_view(rid)["sections"]:
        for s in sec["statements"]:
            if s["author"]["type"] == "ai" and s["status"] == "pending":
                eng.decide_statement(rid, *OWN, s["id"], "accept", ack_flag=True)


def test_mark_ready_lists_everything_still_needed_and_is_owner_only(eng):
    rid = at_draft(eng)
    e = expect("not_ready", eng.mark_ready, rid, *OWN)
    assert e.required == ["generate the draft"]
    eng.store_draft(rid, *OWN, ai_sections(2), AI_EVENT)
    e = expect("not_ready", eng.mark_ready, rid, *OWN)
    assert len(e.required) == 2 and all("decide AI-written statement" in r for r in e.required)
    for who in (QA, SME, OTHER):
        expect("not_owner", eng.mark_ready, rid, *who)
    assert eng.get(rid)["status"] == "in_progress"                                           # nothing moved on its own


def test_mark_ready_succeeds_only_when_every_decision_is_recorded_and_freezes_the_record(eng):
    rid = at_draft(eng)
    eng.store_draft(rid, *OWN, ai_sections(2), AI_EVENT)
    finish(eng, rid)
    assert eng.get(rid)["next_required"] == ["mark the draft ready for approval"]
    v = eng.mark_ready(rid, *OWN)
    assert v["status"] == "ready_for_approval" and v["step"] == "ready" and v["ready"]["by"] == "initiator-1" and v["ready"]["at"]
    for fn, args in [(eng.answer, ("immediate_action", "answer", "x")), (eng.decide, ("root_cause", "x", "y")),
                     (eng.advance, ("draft",)), (eng.decide_proposal, ("classification", "confirm")),
                     (eng.store_draft, (ai_sections(1), AI_EVENT)), (eng.mark_ready, ())]:
        expect("frozen", fn, rid, *OWN, *args)
    expect("not_permitted", eng.cancel, rid, *OWN, "changed my mind")                        # only QA may cancel a ready record
    assert eng.cancel(rid, *QA, "Raised in error.")["status"] == "cancelled"


def test_tbd_accepted_items_allow_ready_and_appear_in_the_draft_as_to_be_determined(eng):
    rid = at_draft(eng)
    eng.decide(rid, *OWN, "root_cause", tbd=True)
    eng.accept_tbd(rid, *OWN, "root_cause", "To be established by the investigation.")
    eng.store_draft(rid, *OWN, ai_sections(0), AI_EVENT)
    text = next(s["text"] for sec in eng.draft_view(rid)["sections"] for s in sec["statements"] if s["element"] == "root_cause")
    assert "to be determined" in text and "accepted as to be determined at investigation by initiator-1" in text
    assert eng.mark_ready(rid, *OWN)["status"] == "ready_for_approval"


def test_cancel_rules(eng):
    rid = new(eng)["id"]
    expect("not_permitted", eng.cancel, rid, *SME, "x")
    expect("validation", eng.cancel, rid, *OWN, " ")
    assert eng.cancel(rid, *OWN, "Duplicate.")["cancelled"]["by"] == "initiator-1"
    expect("cancelled", eng.cancel, rid, *OWN, "again")
    expect("cancelled", eng.answer, rid, *OWN, "site", "answer", "x")


# ================================================================== history / persistence
def test_every_action_is_recorded_in_an_append_only_event_log(eng):
    rid = at_questions(eng)
    eng.answer(rid, *OWN, "reported_by", "answer", "A. Operator")
    ev = eng.store.events(rid)
    assert [e["action"] for e in ev][:3] == ["create", "proposals_stored", "proposal_decision"]
    assert ev[-1]["action"] == "answer" and ev[-1]["actor"] == "initiator-1" and ev[-1]["ts"]
    c = sqlite3.connect(eng.store.path)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("UPDATE events SET actor='x'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("DELETE FROM events")
    c.close()


def test_refused_actions_leave_no_trace_in_state_or_events(eng):
    rid = new(eng)["id"]
    before = (eng.get(rid), len(eng.store.events(rid)))
    expect("skip", eng.advance, rid, *OWN, "draft")
    expect("step_order", eng.decide, rid, *OWN, "root_cause", "x", "y")
    assert (eng.get(rid), len(eng.store.events(rid))) == before


def test_state_survives_a_restart(tmp_path):
    path = str(tmp_path / "w.db")
    e1 = Engine(Store(path))
    rid = e1.create(*OWN, "Persisted.", {"site": "SITE-RB"})["id"]
    assert Engine(Store(path)).get(rid)["answers"]["site"]["value"] == "SITE-RB"
    expect("not_found", Engine(Store(path)).get, "WF-9999")


def test_view_next_required_walks_the_whole_loop(eng):
    rid = new(eng)["id"]
    assert eng.get(rid)["next_required"][0].startswith("request the AI proposals")
    eng.store_proposals(rid, *OWN, proposals(), AI_EVENT)
    for i in ("event_type", "regulations", "sops", "classification"):
        eng.decide_proposal(rid, *OWN, i, "confirm")
    assert eng.get(rid)["next_required"] == ["advance to step 'questions'"]
    eng.advance(rid, *OWN, "questions")
    assert "generate the gap questions (or waive them with a justification)" in eng.get(rid)["next_required"]
    eng.waive_gap(rid, *OWN, "n/a")
    answer_all(eng, rid)
    decide_all(eng, rid)
    assert eng.get(rid)["next_required"] == ["advance to step 'checklist'"] or "advance to step 'checklist'" in eng.get(rid)["next_required"]
    eng.advance(rid, *OWN, "checklist")
    assert eng.get(rid)["next_required"] == ["advance to step 'draft'"]
    eng.advance(rid, *OWN, "draft")
    assert eng.get(rid)["next_required"] == ["generate the draft"]
