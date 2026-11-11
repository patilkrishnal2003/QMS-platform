"""Loads SOPs and the deviation template from flat files (a simulated document
management system). Stdlib only."""
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

STATUSES = {"draft", "approved", "effective", "superseded", "obsolete"}
REQUIRED_HEADER = ("sop_number", "title", "version", "status", "effective_date", "synthetic")
SECTION_RE = re.compile(r"^##\s+(\d+(?:\.\d+)*)\s+(.+?)\s*$")


class LoadError(Exception):
    pass


@dataclass
class Section:
    section_id: str
    title: str
    text: str


@dataclass
class Document:
    sop_number: str
    title: str
    version: str
    status: str
    effective_date: date | None
    supersedes: str | None
    owner: str
    file: str
    sections: list[Section] = field(default_factory=list)

    def is_effective(self, today: date) -> bool:
        """Effective = status 'effective' AND the effective date has been reached."""
        return self.status == "effective" and self.effective_date is not None and self.effective_date <= today

    def section(self, section_id: str) -> Section | None:
        return next((s for s in self.sections if s.section_id == section_id), None)


def _parse_date(value: str, where: str) -> date | None:
    if value.strip().lower() in ("none", ""):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError as e:
        raise LoadError(f"{where}: bad effective_date {value!r}") from e


def parse_sop(path: Path) -> Document:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        raise LoadError(f"{path.name}: missing header block")
    try:
        end = lines.index("---", 1)
    except ValueError as e:
        raise LoadError(f"{path.name}: unterminated header block") from e
    head: dict[str, str] = {}
    for ln in lines[1:end]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            head[k.strip()] = v.strip()
    missing = [k for k in REQUIRED_HEADER if k not in head]
    if missing:
        raise LoadError(f"{path.name}: missing header fields {missing}")
    if head["synthetic"].lower() != "true":
        raise LoadError(f"{path.name}: must be labelled synthetic: true")
    if head["status"] not in STATUSES:
        raise LoadError(f"{path.name}: status must be one of {sorted(STATUSES)}")
    doc = Document(
        sop_number=head["sop_number"], title=head["title"], version=head["version"], status=head["status"],
        effective_date=_parse_date(head["effective_date"], path.name),
        supersedes=None if head.get("supersedes", "none") == "none" else head["supersedes"],
        owner=head.get("owner", ""), file=path.name)
    if doc.status == "effective" and doc.effective_date is None:
        raise LoadError(f"{path.name}: status effective requires an effective_date")
    cur: tuple[str, str] | None = None
    buf: list[str] = []

    def flush():
        if cur:
            doc.sections.append(Section(cur[0], cur[1], " ".join(x.strip() for x in buf if x.strip())))

    for ln in lines[end + 1:]:
        m = SECTION_RE.match(ln)
        if m:
            flush()
            cur, buf = (m.group(1), m.group(2)), []
        elif cur:
            buf.append(ln)
    flush()
    if not doc.sections:
        raise LoadError(f"{path.name}: no sections found")
    return doc


def load_sops(directory: Path) -> list[Document]:
    docs = [parse_sop(p) for p in sorted(directory.glob("*.md"))]
    if not docs:
        raise LoadError(f"no SOP files in {directory}")
    seen = {}
    for d in docs:
        key = (d.sop_number, d.version)
        if key in seen:
            raise LoadError(f"duplicate SOP version {key}: {seen[key]} and {d.file}")
        seen[key] = d.file
    return docs


def load_template(path: Path) -> dict:
    t = json.loads(path.read_text(encoding="utf-8"))
    if t.get("synthetic") is not True:
        raise LoadError(f"{path.name}: must be labelled synthetic")
    if t.get("record_kind") != "template":
        raise LoadError(f"{path.name}: record_kind must be 'template'")
    if not t.get("fields"):
        raise LoadError(f"{path.name}: no fields")
    return t


def load_traceability(path: Path) -> dict:
    t = json.loads(path.read_text(encoding="utf-8"))
    if t.get("synthetic") is not True:
        raise LoadError(f"{path.name}: must be labelled synthetic")
    return t
