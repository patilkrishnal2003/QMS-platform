"""SIMULATED identity for Phase 1: a fixed user table selected by the X-User header.
This is NOT real authentication - it exists only to exercise the two-person rule."""
from dataclasses import dataclass

from fastapi import Header, HTTPException

USERS = {"steward-a": "steward", "steward-b": "steward", "qa-1": "qa", "viewer-1": "viewer",
         "initiator-1": "initiator", "initiator-2": "initiator", "sme-1": "sme"}
APPROVER_ROLES = {"steward", "qa"}


@dataclass(frozen=True)
class User:
    name: str
    role: str


def get_user(x_user: str | None = Header(default=None)) -> User:
    if not x_user or x_user not in USERS:
        raise HTTPException(401, "unknown or missing X-User (simulated users: %s)" % ", ".join(USERS))
    return User(x_user, USERS[x_user])
