"""Explicit, user-triggered fetch of one registry source -> library ingest.
Every outbound request goes through allowlist.safe_get (allow-list checked first);
the hand-off to the library goes through allowlist.internal_post."""
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from . import allowlist

ECFR_HOST = "www.ecfr.gov"
FR_HOST = "www.federalregister.gov"
ECFR_TITLES = "https://www.ecfr.gov/api/versioner/v1/titles.json"
BLOCKS = {"HEAD", "P", "FP", "CITA", "AUTH", "SOURCE", "HD1", "HD2", "HD3", "FTNT", "EDNOTE", "PSPACE"}


class FetchError(Exception):
    pass


@dataclass
class Doc:
    title: str
    text: str
    document_date: str
    version_or_docket: str
    fetched_url: str
    status: str = "final"
    content_kind: str = "full_text"  # or "metadata_only"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def ecfr_xml_to_text(xml: str) -> str:
    """Flatten eCFR XML into one paragraph per line so diffs are line-based."""
    root = ET.fromstring(xml)
    lines: list[str] = []

    def emit(el):
        if el.tag in BLOCKS or (len(el) == 0 and (el.text or "").strip()):
            t = _norm("".join(el.itertext()))
            if t:
                lines.append(t)
            return
        for ch in el:
            emit(ch)

    emit(root)
    return "\n".join(lines)


def _get(url: str, get):
    r = get(url)
    if r.status_code != 200:
        raise FetchError(f"{url} -> HTTP {r.status_code}")
    return r


def fetch_ecfr(url: str, get) -> Doc:
    if "{date}" in url:
        titles = _get(ECFR_TITLES, get).json()["titles"]
        date = next(t["up_to_date_as_of"] for t in titles if t["number"] == 21)
        url = url.replace("{date}", date)
    else:
        m = re.search(r"/full/(\d{4}-\d{2}-\d{2})/", url)
        date = m.group(1) if m else ""
    xml = _get(url, get).text
    text = ecfr_xml_to_text(xml)
    if not text:
        raise FetchError("eCFR response contained no text")
    q = dict(p.split("=", 1) for p in urlparse(url).query.split("&") if "=" in p)
    cite = "21 CFR " + (q["section"] if q.get("section") else f"Part {q.get('part', '')}")
    return Doc(title=cite, text=text, document_date=date, version_or_docket=cite, fetched_url=url)


def fetch_federal_register(url: str, get) -> Doc:
    """METADATA ONLY, by design. federalregister.gov serves its full-text pages (.txt/.xml/.html)
    behind bot protection (302 -> unblock.federalregister.gov), so only what the JSON API
    reliably returns is stored: title, date, document number, docket, abstract, source URL.
    The item is flagged content_kind='metadata_only'. This is a known limitation, not a bug."""
    meta_url = url + ("&" if "?" in url else "?") + (
        "fields[]=title&fields[]=publication_date&fields[]=document_number"
        "&fields[]=docket_ids&fields[]=abstract&fields[]=html_url")
    meta = _get(meta_url, get).json()
    docket = ", ".join(meta.get("docket_ids") or []) or "(none)"
    text = "\n".join([
        f"Title: {meta['title']}",
        f"Publication date: {meta['publication_date']}",
        f"Federal Register document number: {meta['document_number']}",
        f"Docket: {docket}",
        f"Source URL: {meta.get('html_url') or url}",
        f"Abstract: {_norm(meta.get('abstract') or '(none provided)')}",
        "Full text: NOT STORED (metadata-only record)",
    ])
    return Doc(title=meta["title"], text=text, document_date=meta["publication_date"],
               version_or_docket=f"{meta['document_number']} / {docket}", fetched_url=url,
               content_kind="metadata_only")


def fetch_document(source: dict, get=None) -> Doc:
    get = get or allowlist.safe_get
    allowlist.check_url(source["url"])  # refuse before ANY request, even if get is swapped
    host = urlparse(source["url"]).hostname
    if host == ECFR_HOST:
        return fetch_ecfr(source["url"], get)
    if host == FR_HOST:
        return fetch_federal_register(source["url"], get)
    raise FetchError(f"no adapter for host {host!r}")


def ingest(source: dict, doc: Doc, actor: str, post=None) -> dict:
    post = post or allowlist.internal_post
    payload = {
        "source_id": source["id"], "source_url": source["url"], "fetched_url": doc.fetched_url,
        "title": doc.title, "text": doc.text, "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "document_date": doc.document_date, "version_or_docket": doc.version_or_docket,
        "status": doc.status, "content_kind": doc.content_kind, "legal_weight": source["legal_weight"],
        "auto_accept": bool(source["auto_accept"]), "submitted_by": actor,
    }
    r = post("/internal/ingest", payload)
    if r.status_code != 200:
        raise FetchError(f"library ingest failed: HTTP {r.status_code} {r.text[:200]}")
    return r.json()


# Fixed Phase 1 sources. FR document number 2018-26957 is hard-coded for repeatability.
SEED_SOURCES = [
    {"name": "21 CFR Part 211 - cGMP for finished pharmaceuticals (eCFR)",
     "url": "https://www.ecfr.gov/api/versioner/v1/full/{date}/title-21.xml?part=211",
     "jurisdiction": "US-FDA", "product_types": ["drug"], "document_types": ["regulation"],
     "check_frequency": "manual", "legal_weight": "regulation", "auto_accept": False},
    {"name": "FDA Guidance: Data Integrity and Compliance With Drug CGMP (FR 2018-26957)",
     "url": "https://www.federalregister.gov/api/v1/documents/2018-26957.json",
     "jurisdiction": "US-FDA", "product_types": ["drug"], "document_types": ["guidance"],
     "check_frequency": "manual", "legal_weight": "guidance", "auto_accept": False},
]
