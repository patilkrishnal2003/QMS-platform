"""workflow: PASSIVE service (no network, no HTTP client). The gateway orchestrates retrieval and the
LLM call and hands data to these endpoints, which only compute: prompt building, output parsing and
citation verification."""
from fastapi import FastAPI, HTTPException

from . import prompt, verifier

app = FastAPI(title="workflow")


@app.get("/health")
def health() -> dict:
    return {"service": "workflow", "status": "ok"}


@app.post("/internal/build-prompt")
def build_prompt(payload: dict):
    try:
        return prompt.build_prompt(payload["query"], payload.get("sources", []), payload["system_prompt"],
                                   int(payload.get("max_sources", 10)), int(payload.get("max_context_chars", 12000)),
                                   payload.get("passage_template", prompt.DEFAULT_PASSAGE_TEMPLATE),
                                   payload.get("instructions", prompt.DEFAULT_INSTRUCTIONS))
    except KeyError as e:
        raise HTTPException(400, f"missing field: {e}")
    except prompt.PromptConfigError as e:
        raise HTTPException(400, str(e))


@app.post("/internal/parse")
def parse(payload: dict):
    return prompt.parse_output(payload.get("raw", ""))


@app.post("/internal/verify")
def verify(payload: dict):
    """sources: {"<source_id>@<version>": source-or-null}. A null/missing source means 'does not exist'."""
    return verifier.verify(payload.get("statements", []), payload.get("sources", {}), payload.get("thresholds"))
