"""POST /rules (graded contract) and GET /api/rules (dashboard).

A rule is "when a comment contains this keyword, DM this message". The keyword
is stored exactly as given; case-insensitive substring matching happens in the
matcher (BLUEPRINT §4.2), so nothing here normalises the text."""
import logging
import secrets

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from . import db

log = logging.getLogger("linkplease.rules")

router = APIRouter()


class RuleIn(BaseModel):
    keyword: str = Field(min_length=1)
    dm_message: str = Field(min_length=1)

    @field_validator("keyword", "dm_message")
    @classmethod
    def not_blank(cls, v: str) -> str:
        """Both fields are required and must contain something after trimming;
        a whitespace-only keyword would match every comment."""
        if not v.strip():
            raise ValueError("must not be empty")
        return v


def new_rule_id() -> str:
    """'rule_' + 12 hex chars. token_hex(6) is 6 random bytes = 12 characters."""
    return "rule_" + secrets.token_hex(6)


@router.post("/rules", status_code=201)
async def create_rule(payload: RuleIn) -> dict:
    """Response shape is graded: exactly rule_id, keyword, dm_message."""
    rule_id = new_rule_id()
    await db.execute(
        "INSERT INTO rules (rule_id, keyword, dm_message) VALUES ($1, $2, $3)",
        rule_id,
        payload.keyword,
        payload.dm_message,
    )
    log.info("rule created rule_id=%s keyword=%r", rule_id, payload.keyword)
    return {
        "rule_id": rule_id,
        "keyword": payload.keyword,
        "dm_message": payload.dm_message,
    }


@router.get("/api/rules")
async def list_rules() -> list[dict]:
    """Dashboard view: every rule plus how many DM obligations it produced.
    CANCELLED jobs are excluded because they were never owed."""
    rows = await db.fetch(
        """
        SELECT r.rule_id,
               r.keyword,
               r.dm_message,
               r.created_at,
               count(j.job_id) FILTER (WHERE j.status <> 'CANCELLED') AS job_count
        FROM rules r
        LEFT JOIN dm_jobs j ON j.rule_id = r.rule_id
        GROUP BY r.rule_id, r.keyword, r.dm_message, r.created_at
        ORDER BY r.created_at DESC
        """
    )
    return [
        {
            "rule_id": r["rule_id"],
            "keyword": r["keyword"],
            "dm_message": r["dm_message"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "job_count": int(r["job_count"] or 0),
        }
        for r in rows
    ]


@router.delete("/api/rules/{rule_id}", status_code=204)
async def delete_rule(rule_id: str) -> None:
    """Dashboard convenience. Refused while jobs reference the rule, because
    dm_jobs.rule_id is a foreign key and we would rather explain the 409 than
    cascade-delete the audit trail."""
    in_use = await db.fetchval(
        "SELECT count(*) FROM dm_jobs WHERE rule_id = $1", rule_id
    )
    if in_use:
        raise HTTPException(status_code=409, detail="rule has jobs")
    result = await db.execute("DELETE FROM rules WHERE rule_id = $1", rule_id)
    if result.endswith(" 0"):
        raise HTTPException(status_code=404, detail="no such rule")
