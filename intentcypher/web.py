"""Minimal web UI: ask a question, provide connection details in the form
(falling back to .env / environment), see Cypher, parameters and rows.

Run: python -m intentcypher.web  (serves on 127.0.0.1:8765)
"""

from __future__ import annotations

import json
import threading
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from intentsql.jev_client import JevClient

from intentcypher.connection import apply_env_file, connection_config
from intentcypher.semantic_cypher import run_read

app = FastAPI(title="IntentCypher prototype", version="0.1.0")
apply_env_file()  # System One settings from .env, real env vars take precedence
_lock = threading.Lock()  # one question at a time; local single-user prototype

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>IntentCypher</title>
<style>
 body{font:14px/1.45 -apple-system,Segoe UI,sans-serif;max-width:860px;margin:2rem auto;padding:0 1rem;color:#111}
 h1{font-size:1.25rem} code,pre{font:12px/1.5 ui-monospace,monospace;background:#f4f4f4}
 pre{padding:.8rem;border-radius:6px;overflow:auto;white-space:pre-wrap}
 form{display:grid;gap:.5rem;grid-template-columns:repeat(auto-fit,minmax(180px,1fr))}
 input,button{padding:.45rem;border:1px solid #ccc;border-radius:4px}
 button{grid-column:1/-1;background:#2563eb;color:#fff;border:0;font-weight:600;cursor:pointer}
 details{margin:.8rem 0} .refused{color:#b00} .ok{color:#060}
</style></head><body>
<h1>IntentCypher — Jev decisions, parameterized Cypher</h1>
<form id="f">
 <input name="question" placeholder="question, e.g. five companies founded after 1999" style="grid-column:1/-1" required>
 <input name="uri" placeholder="NEO4J_URI (optional, overrides .env)">
 <input name="username" placeholder="username (optional)">
 <input name="password" type="password" placeholder="password (optional)">
 <input name="database" placeholder="database (optional)">
 <button>Ask</button>
</form>
<div id="out"></div>
<script>
document.getElementById('f').onsubmit = async (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  document.getElementById('out').textContent = 'thinking...';
  const res = await fetch('/api/ask', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(data)});
  const answer = await res.json();
  const out = [];
  if (answer.cypher) out.push('<pre>' + answer.cypher.replace(/</g,'&lt;') + '</pre>');
  if (answer.parameters) out.push('<p>Parameters: <code>' +
      JSON.stringify(answer.parameters).replace(/</g,'&lt;') + '</code></p>');
  if (answer.status === 'answered') {
    out.push('<p class="ok">' + answer.rows.length + ' rows</p>');
    for (const row of answer.rows)
      out.push('<pre>' + JSON.stringify(row).replace(/</g,'&lt;') + '</pre>');
    out.push('<details><summary>plan / usage</summary><pre>' +
        JSON.stringify({plan: answer.plan, jev_calls: answer.jev_calls,
                        input_tokens: answer.input_tokens,
                        output_tokens: answer.output_tokens})
            .replace(/</g,'&lt;') + '</pre></details>');
  } else {
    out.push('<p class="refused">refused: ' + (answer.reason || 'error') + '</p>');
  }
  document.getElementById('out').innerHTML = out.join('');
};
</script></body></html>"""


class AskRequest(BaseModel):
    question: str
    uri: str | None = None
    username: str | None = None
    password: str | None = None
    database: str | None = None


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _PAGE


@app.post("/api/ask")
def ask(request: AskRequest) -> JSONResponse:
    try:
        config = connection_config(env={}, uri=request.uri, username=request.username,
                                   password=request.password, database=request.database)
    except ConnectionError as exc:
        return JSONResponse({"status": "error", "reason": str(exc)}, status_code=400)
    with _lock:
        answer = run_read(request.question, config, JevClient())
    payload: dict[str, Any] = {
        "status": answer.status,
        "reason": answer.reason,
        "cypher": answer.query,
        "parameters": json.loads(json.dumps(answer.parameters, default=str)),
        "rows": json.loads(json.dumps(answer.rows, default=str)),
        "plan": answer.plan,
        "jev_calls": answer.jev_calls,
        "input_tokens": answer.input_tokens,
        "output_tokens": answer.output_tokens,
    }
    return JSONResponse(payload)


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765)


if __name__ == "__main__":
    main()
