from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel

from .store import Eqms, EqmsError

app = FastAPI(title="mock-eqms")
_eqms: Eqms | None = None


def get_eqms() -> Eqms:
    global _eqms
    if _eqms is None:
        _eqms = Eqms()
    return _eqms


def _err(e: EqmsError) -> HTTPException:
    return HTTPException(e.status, str(e))


@app.get("/health")
def health() -> dict:
    return {"service": "mock-eqms", "status": "ok"}


# ---- read-only connection (IK-02): GET only ----
read = APIRouter(prefix="/read", tags=["read-only"])


@read.get("/records")
def list_records(type: str | None = None, severity: str | None = None, status: str | None = None,
                 site: str | None = None, limit: int = 100, eqms: Eqms = Depends(get_eqms)):
    """Executed (filled-in) records, optionally by event type. Never returns blank templates."""
    return eqms.list_records(type, severity, status, site, min(limit, 1000))


@read.get("/records/{record_id}")
def get_record(record_id: str, eqms: Eqms = Depends(get_eqms)):
    try:
        return eqms.get_record(record_id)
    except EqmsError as e:
        raise _err(e)


@read.get("/templates")
def list_templates(eqms: Eqms = Depends(get_eqms)):
    return eqms.list_templates()


# ---- write with human confirmation (IK-09, Should priority) ----
write = APIRouter(prefix="/write", tags=["write"])


class WriteIn(BaseModel):
    record: dict
    human_confirmed: bool = False
    confirmed_by: str = ""


@write.post("/records", status_code=201)
def write_record(body: WriteIn, eqms: Eqms = Depends(get_eqms)):
    """Only accepted after an explicit human confirmation; stored as a DRAFT, never submitted."""
    try:
        return eqms.write_record(body.record, body.human_confirmed, body.confirmed_by)
    except EqmsError as e:
        raise _err(e)


app.include_router(read)
app.include_router(write)
