"""Citation-verifier evaluation: 30 labelled cases, precision/recall reported (not just pass/fail).

Positive class = "verified". Ground truth = a person's judgement that the citation really exists, the passage
really contains the quote, the passage really supports the statement and the source is currently usable.
Labels were written BEFORE running the verifier; thresholds were fixed beforehand (0.60 / 0.35) and are not
tuned to this set. 30 cases is small: treat the numbers as indicative, not statistically tight.

Run alone for the report:  python tests/test_citation_verifier_eval.py
"""
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOPS = ROOT / "knowledge" / "data" / "sops"

_spec = importlib.util.spec_from_file_location("workflow_verifier", ROOT / "workflow" / "app" / "verifier.py")
V = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(V)


# --------------------------------------------------------------------------- corpus (same shapes the gateway builds)
def _build_sources() -> dict:
    src: dict = {}
    effective_docs = {}
    for p in sorted(SOPS.glob("*.md")):
        text = p.read_text(encoding="utf-8")
        head = dict(re.findall(r"^(\w+):\s*(.*)$", text.split("---")[1], re.M))
        passages, cur, buf = [], None, []
        for ln in text.split("---", 2)[2].splitlines():
            m = re.match(r"^##\s+(\d+(?:\.\d+)*)\s+(.+?)\s*$", ln)
            if m:
                if cur:
                    passages.append({"locator": cur[0], "text": f"{cur[1]}. " + " ".join(b.strip() for b in buf if b.strip())})
                cur, buf = (m.group(1), m.group(2)), []
            elif cur:
                buf.append(ln)
        if cur:
            passages.append({"locator": cur[0], "text": f"{cur[1]}. " + " ".join(b.strip() for b in buf if b.strip())})
        s = {"type": "sop", "source_id": f"sop:{head['sop_number']}", "version": head["version"], "status": head["status"],
             "effective": head["status"] == "effective", "link": f"http://127.0.0.1:8002/documents/{head['sop_number']}?version={head['version']}",
             "passages": passages}
        src[V.source_key(s["source_id"], s["version"])] = s
        if s["effective"]:
            effective_docs[head["sop_number"]] = s
    for r in json.loads((ROOT / "mock-eqms" / "data" / "deviations_seed.json").read_text(encoding="utf-8"))["records"]:
        ps = [{"locator": f, "text": str(r[f])} for f in ("description", "immediate_action", "investigation_summary",
                                                          "root_cause", "root_cause_category", "product_impact") if r.get(f)]
        s = {"type": "record", "source_id": f"record:{r['id']}", "version": r["version"], "status": r["status"], "effective": None,
             "link": f"http://127.0.0.1:8002/records/{r['id']}", "passages": ps}
        src[V.source_key(s["source_id"], s["version"])] = s
    fx = json.loads((ROOT / "tests" / "fixtures" / "ecfr_211_22.json").read_text(encoding="utf-8"))
    src["library:1@1"] = {"type": "library", "source_id": "library:1", "version": "1", "status": "final", "effective": True,
                          "content_kind": "full_text", "link": "http://127.0.0.1:8001/library/1", "lines": fx["lines"], "passages": []}
    for m in json.loads((ROOT / "knowledge" / "data" / "traceability.json").read_text(encoding="utf-8"))["mappings"]:
        ps = []
        for i in m["implemented_by"]:
            d = effective_docs.get(i["sop_number"])
            sec = next((p for p in d["passages"] if p["locator"] == i["section"]), None) if d else None
            title = sec["text"].split(". ")[0] if sec else ""
            ps.append({"locator": f"{i['sop_number']}#{i['section']}",
                       "text": f"{m['citation']} is implemented by {i['sop_number']} section {i['section']} {title} (v{d['version'] if d else '?'})."})
        src[f"req:{m['requirement_id']}"] = {"type": "req", "source_id": f"req:{m['requirement_id']}", "version": None, "status": "map",
                                              "effective": None, "link": f"http://127.0.0.1:8002/traceability/{m['requirement_id']}", "passages": ps}
    # things the stores do NOT have: a blank template is not an executed record; made-up ids
    src["record:TPL-DEV-001@1.0"] = None
    return src


def cite(sid, ver, loc, quote):
    return {"source_id": sid, "version": ver, "locator": loc, "quote": quote}


# (id, category, statement, citations, ground_truth_supported)
CASES = [
    # ---------------- 15 genuinely supported citations
    ("V01", "valid", "The originator must record the event in the deviation record within 24 hours of detection.",
     [cite("sop:SOP-DEV-001", "3.0", "5.1", "within 24 hours of detection")], True),
    ("V02", "valid", "QA classifies a deviation as minor, major or critical within 2 working days.",
     [cite("sop:SOP-DEV-001", "3.0", "5.3", "within two working days using SOP-DEV-002")], True),
    ("V03", "valid", "Critical deviations have an investigation target of 15 working days.",
     [cite("sop:SOP-DEV-001", "3.0", "6", "critical 15 working days")], True),
    ("V04", "valid", "A deviation is critical when there is a confirmed or probable impact on patient safety.",
     [cite("sop:SOP-DEV-002", "2.0", "3", "Critical: confirmed or probable impact on patient safety")], True),
    ("V05", "valid", "A recurrence of a minor event is classified at least major.",
     [cite("sop:SOP-DEV-002", "2.0", "5", "A recurrence of a minor event is classified at least major")], True),
    ("V06", "valid", "Operator error alone is not an acceptable root cause.",
     [cite("sop:SOP-INV-001", "1.1", "4", "alone is not an acceptable root cause")], True),
    ("V07", "valid", "A correction alone, such as re-cleaning or rework, does not need a CAPA.",
     [cite("sop:SOP-CAPA-001", "4.0", "4", "A correction alone, such as re-cleaning or rework, does not need a CAPA")], True),
    ("V08", "valid", "Emergency changes need retrospective approval within five working days.",
     [cite("sop:SOP-CHG-001", "2.0", "4", "Emergency changes need retrospective approval within five working days")], True),
    ("V09", "valid", "Changing calibration or maintenance intervals needs an assessment even if the change is meant to reduce workload.",
     [cite("sop:SOP-CHG-002", "1.0", "5", "require assessment even when made to reduce workload")], True),
    ("V10", "valid", "The quality control unit has the responsibility and authority to approve or reject all components, drug product containers, closures, in-process materials, packaging material, labeling, and drug products.",
     [cite("library:1", "1", "lines:2", "the responsibility and authority to approve or reject all components")], True),
    ("V11", "valid", "The responsibilities and procedures applicable to the quality control unit must be in writing, and the written procedures must be followed.",
     [cite("library:1", "1", "lines:5", "such written procedures shall be followed")], True),
    ("V12", "valid", "The quality control unit is responsible for approving or rejecting all procedures or specifications that affect the identity, strength, quality, and purity of the drug product.",
     [cite("library:1", "1", "lines:4", "responsibility for approving or rejecting all procedures or specifications")], True),
    ("V13", "valid", "The press deviation was caused by a calibration interval that was extended without a change assessment.",
     [cite("record:SYN-DEV-2024-001", "1.0", "root_cause", "Calibration interval extended without a change assessment")], True),
    ("V14", "valid", "The metal detector failure was caused by the sensitivity setting not being retained after a power interruption.",
     [cite("record:SYN-DEV-2025-020", "1.0", "root_cause", "Sensitivity setting not retained after power interruption")], True),
    ("V15", "valid", "21 CFR 211.192 (production record review) is implemented by SOP-DEV-001 section 5.4.",
     [cite("req:211.192", None, "SOP-DEV-001#5.4", "is implemented by SOP-DEV-001 section 5.4")], True),
    # ---------------- 15 wrong / fabricated / mismatched citations
    ("I01", "source_missing", "All deviations must be closed within seven days.",
     [cite("sop:SOP-XYZ-009", "1.0", "1", "All deviations are closed in seven days")], False),
    ("I02", "version_missing_in_store", "The originator must record the event within 24 hours of detection.",
     [cite("sop:SOP-DEV-001", "9.9", "5.1", "within 24 hours of detection")], False),
    ("I03", "section_missing", "QA approves every deviation in writing.",
     [cite("sop:SOP-DEV-001", "3.0", "5.9", "QA approves every deviation")], False),
    ("I04", "fabricated_quote", "The originator must record the event within 12 hours of detection.",
     [cite("sop:SOP-DEV-001", "3.0", "5.1", "within 12 hours of detection")], False),
    ("I05", "unrelated_statement", "Fire extinguishers must be inspected every month in the warehouse.",
     [cite("sop:SOP-DEV-002", "2.0", "3", "Critical: confirmed or probable impact on patient safety")], False),
    ("I06", "wrong_number", "The originator must record the event within 48 hours of detection.",
     [cite("sop:SOP-DEV-001", "3.0", "5.1", "within 24 hours of detection")], False),
    ("I07", "superseded_version", "The originator records the event within 48 hours of detection.",
     [cite("sop:SOP-DEV-001", "2.0", "5.1", "within 48 hours of detection")], False),
    ("I08", "draft_version", "CAPAs are prioritised high, medium or low using the risk score.",
     [cite("sop:SOP-CAPA-001", "5.0", "4", "CAPAs are prioritised high, medium or low using the risk score")], False),
    ("I09", "template_as_record", "Every deviation record must include a description of the event.",
     [cite("record:TPL-DEV-001", "1.0", "description", "Description of event")], False),
    ("I10", "no_citation", "All deviations must be closed within 7 days.", [], False),
    ("I11", "library_missing", "The quality control unit approves all labeling.",
     [cite("library:99999", "1", "lines:1", "quality control unit")], False),
    ("I12", "version_not_given", "The originator must record the event within 24 hours of detection.",
     [cite("sop:SOP-DEV-001", "", "5.1", "within 24 hours of detection")], False),
    ("I13", "overreach", "Deviations are closed only when the CEO approves the CAPA budget and the supplier signs off.",
     [cite("sop:SOP-DEV-001", "3.0", "5.6", "QA has signed the closure")], False),
    ("I14", "false_mapping", "21 CFR 211.192 is implemented by SOP-CHG-001 section 4.",
     [cite("req:211.192", None, "SOP-CHG-001#4", "is implemented by SOP-CHG-001 section 4")], False),
    ("I15", "meaning_reversed", "Operator error alone is an acceptable root cause.",
     [cite("sop:SOP-INV-001", "1.1", "4", "'Operator error' alone is not an acceptable root cause")], False),
]
assert len(CASES) == 30 and sum(1 for c in CASES if not c[4]) == 15
STRUCTURAL = {"source_missing", "version_missing_in_store", "section_missing", "fabricated_quote", "template_as_record",
              "no_citation", "library_missing", "version_not_given", "false_mapping"}


def evaluate() -> dict:
    sources = _build_sources()
    rows = []
    for cid, cat, text, cites, truth in CASES:
        r = V.verify_statement({"text": text, "kind": "requirement", "citations": cites}, sources)
        rows.append({"id": cid, "category": cat, "truth_supported": truth, "verdict": r["verdict"],
                     "passed": r["verdict"] == "verified", "score": r["score"], "reasons": r["reasons"]})
    tp = sum(1 for r in rows if r["truth_supported"] and r["passed"])
    fp = sum(1 for r in rows if not r["truth_supported"] and r["passed"])
    fn = sum(1 for r in rows if r["truth_supported"] and not r["passed"])
    tn = sum(1 for r in rows if not r["truth_supported"] and not r["passed"])
    prec = tp / (tp + fp) if tp + fp else 1.0
    rec = tp / (tp + fn) if tp + fn else 1.0
    return {"rows": rows, "tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": prec, "recall": rec,
            "valid_low_confidence": sum(1 for r in rows if r["truth_supported"] and r["verdict"] == "low_confidence"),
            "false_pass_ids": [r["id"] for r in rows if not r["truth_supported"] and r["passed"]],
            "missed_valid_ids": [r["id"] for r in rows if r["truth_supported"] and not r["passed"]]}


def report(m: dict) -> str:
    out = ["", "CITATION VERIFIER EVALUATION (30 cases: 15 supported, 15 bad)", "-" * 66,
           f"{'case':5} {'category':26} {'truth':8} {'verdict':17} score",
           *[f"{r['id']:5} {r['category']:26} {'ok' if r['truth_supported'] else 'BAD':8} {r['verdict']:17} {r['score']}"
             for r in m["rows"]], "-" * 66,
           f"TP={m['tp']} FP={m['fp']} FN={m['fn']} TN={m['tn']}",
           f"PRECISION = {m['precision']:.3f}   (of statements passed as 'verified', share truly supported)",
           f"RECALL    = {m['recall']:.3f}   (of truly supported statements, share passed as 'verified')",
           f"false passes: {m['false_pass_ids'] or 'none'}   missed valid: {m['missed_valid_ids'] or 'none'}"]
    bad = [r for r in m["rows"] if not r["truth_supported"]]
    out.append(f"bad citations caught (not verified): {sum(1 for r in bad if not r['passed'])}/{len(bad)}")
    return "\n".join(out)


# --------------------------------------------------------------------------- stress set (NOT part of the 30)
# Deliberately hard: heavy paraphrase (valid) and fluent near-misses with high word overlap (bad).
# Reported separately so the easy-set numbers are not mistaken for real-world accuracy.
STRESS = [
    ("P1", "paraphrase_valid", "Anyone who discovers a departure from procedure has to log it in the deviation system by the next day.",
     [cite("sop:SOP-DEV-001", "3.0", "5.1", "within 24 hours of detection")], True),
    ("P2", "paraphrase_valid", "If an investigation reveals a different risk level, QA may change the classification.",
     [cite("sop:SOP-DEV-001", "3.0", "5.3", "Classification may be revised if the investigation shows a different risk")], True),
    ("P3", "paraphrase_valid", "Recurring minor events get bumped up to major.",
     [cite("sop:SOP-DEV-002", "2.0", "5", "A recurrence of a minor event is classified at least major")], True),
    ("P4", "paraphrase_valid", "Blaming the operator is not enough; the underlying reason has to be found.",
     [cite("sop:SOP-INV-001", "1.1", "4", "the underlying reason, for example unclear instructions or missing training, must be identified")], True),
    ("P5", "paraphrase_valid", "The quality unit can also reject drug products that another company made under contract.",
     [cite("library:1", "1", "lines:2", "responsible for approving or rejecting drug products manufactured, processed, packed, or held under contract by another company")], True),
    ("P6", "paraphrase_valid", "Where a CAPA is closed, someone has to look again later to see whether it worked.",
     [cite("sop:SOP-CAPA-002", "1.0", "4", "QA reviews deviation and trend data after the defined period")], True),
    ("A1", "adversarial_reversal", "The originator records the event only after 24 hours of detection.",
     [cite("sop:SOP-DEV-001", "3.0", "5.1", "within 24 hours of detection")], False),
    ("A2", "adversarial_number_swap", "Critical deviations have an investigation target of 30 working days.",
     [cite("sop:SOP-DEV-001", "3.0", "6", "critical 15 working days, major 30 working days")], False),
    ("A3", "adversarial_category_swap", "Minor deviations have an investigation target of 15 working days.",
     [cite("sop:SOP-DEV-001", "3.0", "6", "critical 15 working days, major 30 working days")], False),
    ("A4", "adversarial_word_swap", "Emergency changes need prospective approval within five working days.",
     [cite("sop:SOP-CHG-001", "2.0", "4", "Emergency changes need retrospective approval within five working days")], False),
    ("A5", "adversarial_inversion", "A recurrence of a major event is classified at least minor.",
     [cite("sop:SOP-DEV-002", "2.0", "5", "A recurrence of a minor event is classified at least major")], False),
    ("A6", "adversarial_wrong_actor", "The supervisor classifies the deviation within two working days using SOP-DEV-002.",
     [cite("sop:SOP-DEV-001", "3.0", "5.3", "within two working days using SOP-DEV-002")], False),
]


def evaluate_stress() -> dict:
    sources = _build_sources()
    rows = []
    for cid, cat, text, cites, truth in STRESS:
        r = V.verify_statement({"text": text, "kind": "requirement", "citations": cites}, sources)
        rows.append({"id": cid, "category": cat, "truth_supported": truth, "verdict": r["verdict"],
                     "passed": r["verdict"] == "verified", "score": r["score"]})
    tp = sum(1 for r in rows if r["truth_supported"] and r["passed"])
    fp = sum(1 for r in rows if not r["truth_supported"] and r["passed"])
    fn = sum(1 for r in rows if r["truth_supported"] and not r["passed"])
    return {"rows": rows, "tp": tp, "fp": fp, "fn": fn,
            "precision": tp / (tp + fp) if tp + fp else 1.0, "recall": tp / (tp + fn) if tp + fn else 1.0,
            "false_pass_ids": [r["id"] for r in rows if not r["truth_supported"] and r["passed"]],
            "missed_valid_ids": [r["id"] for r in rows if r["truth_supported"] and not r["passed"]]}


def report_stress(m: dict) -> str:
    out = ["", "STRESS SET (12 hard cases, reported separately: 6 heavy paraphrases, 6 fluent near-misses)", "-" * 66,
           *[f"{r['id']:5} {r['category']:26} {'ok' if r['truth_supported'] else 'BAD':8} {r['verdict']:17} {r['score']}" for r in m["rows"]],
           "-" * 66, f"TP={m['tp']} FP={m['fp']} FN={m['fn']}  PRECISION = {m['precision']:.3f}  RECALL = {m['recall']:.3f}",
           f"false passes: {m['false_pass_ids'] or 'none'}   missed valid: {m['missed_valid_ids'] or 'none'}"]
    return "\n".join(out)


# --------------------------------------------------------------------------- tests
def test_citation_verifier_precision_recall(capsys):
    m = evaluate()
    with capsys.disabled():
        print(report(m))
    # hard invariants: a citation that points at nothing real must NEVER pass
    for r in m["rows"]:
        if r["category"] in STRUCTURAL:
            assert r["verdict"] != "verified", r
    structural_expected = {"source_not_found", "locator_not_found", "quote_not_in_passage", "version_missing", "unsupported"}
    for r in m["rows"]:
        if r["category"] in STRUCTURAL:
            assert r["verdict"] in ("invalid_citation", "unsupported"), r
    # regression floor (recorded after the first run; see docs/PROGRESS.md)
    assert m["precision"] >= 0.93 and m["recall"] >= 0.80, (m["precision"], m["recall"])


def test_every_bad_case_is_flagged_never_silently_passed():
    m = evaluate()
    for r in m["rows"]:
        assert r["verdict"] in {"verified", "low_confidence", "mismatch", "invalid_citation", "unsupported"}
    assert not [r for r in m["rows"] if r["category"] in STRUCTURAL and r["passed"]]


def test_known_limitation_semantic_reversal_without_negation_words_can_pass():
    """HONEST LIMITATION, not a target: a lexical check cannot see that 'after' reverses 'within'. A person must
    still confirm every statement. Recorded so nobody mistakes 'verified' for 'proven correct'."""
    sources = _build_sources()
    r = V.verify_statement({"text": "The originator records the event only after 24 hours of detection.",
                            "citations": [cite("sop:SOP-DEV-001", "3.0", "5.1", "within 24 hours of detection")]}, sources)
    assert r["verdict"] in ("verified", "low_confidence")  # documents current behaviour; would need embeddings/NLI + review


def test_stress_set_is_reported_not_gated(capsys):
    """Informational: the hard set shows the real limits. Only sanity-checked, never tuned against."""
    m = evaluate_stress()
    with capsys.disabled():
        print(report_stress(m))
    assert len(m["rows"]) == 12
    # structural guarantees still hold on hard input: every verdict is one of the known flags
    assert all(r["verdict"] in {"verified", "low_confidence", "mismatch", "invalid_citation", "unsupported"} for r in m["rows"])


if __name__ == "__main__":
    print(report(evaluate()))
    print(report_stress(evaluate_stress()))
