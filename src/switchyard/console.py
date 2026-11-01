"""Server-rendered operations console. No build step, no JavaScript framework."""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from typing import Any

STYLE = """
:root { --bg:#f6f7f9; --panel:#fff; --ink:#1b1f24; --muted:#5b6470; --line:#e3e6ea; --accent:#0b6bcb;
  --ok:#1f7a4d; --okbg:#e3f4ea; --wait:#8a5a00; --waitbg:#fdf1d8; --bad:#b42318; --badbg:#fde7e5; --run:#0b6bcb; --runbg:#e4effa; }
@media (prefers-color-scheme: dark) { :root { --bg:#111418; --panel:#181c21; --ink:#e7eaee; --muted:#9aa4b0; --line:#2a3038;
  --okbg:#14301f; --ok:#6fd39b; --waitbg:#33270d; --wait:#f0c062; --badbg:#3a1714; --bad:#ff8a7a; --runbg:#13283d; --run:#7bb6f0; --accent:#7bb6f0; } }
* { box-sizing:border-box } body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.45 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }
header { padding:18px 24px; border-bottom:1px solid var(--line); background:var(--panel); display:flex; gap:16px; align-items:baseline; flex-wrap:wrap }
header h1 { font-size:17px; margin:0 } header .meta { color:var(--muted) }
main { padding:20px 24px; max-width:1280px; margin:0 auto; display:grid; gap:18px }
.kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px }
.kpi { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px 14px }
.kpi b { display:block; font-size:22px; font-variant-numeric:tabular-nums } .kpi span { color:var(--muted); font-size:12px }
section { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px 16px; overflow-x:auto }
section h2 { font-size:14px; margin:0 0 10px } table { border-collapse:collapse; width:100% }
th, td { text-align:left; padding:6px 8px; border-top:1px solid var(--line); vertical-align:top } th { color:var(--muted); font-weight:500; border-top:0 }
code, .mono { font-family:ui-monospace,SFMono-Regular,Consolas,monospace; font-size:12px }
.chip { display:inline-block; padding:1px 7px; margin:1px 2px 1px 0; border-radius:999px; font-size:12px; white-space:nowrap; background:var(--line); color:var(--ink) }
.succeeded,.verified,.approved { background:var(--okbg); color:var(--ok) } .waiting,.awaiting_approval,.retry_wait,.retry { background:var(--waitbg); color:var(--wait) }
.failed,.blocked,.rejected,.high,.critical { background:var(--badbg); color:var(--bad) } .running,.executing { background:var(--runbg); color:var(--run) }
.muted { color:var(--muted) } a { color:var(--accent) } pre { white-space:pre-wrap; background:var(--bg); padding:10px; border-radius:8px; border:1px solid var(--line) }
.empty { color:var(--muted); padding:6px 0 }
button { font:inherit; padding:7px 14px; border-radius:8px; border:1px solid var(--line); background:var(--panel); color:var(--ink); cursor:pointer }
button.primary { background:var(--accent); color:#fff; border-color:var(--accent) } input { font:inherit; padding:7px 10px; border-radius:8px; border:1px solid var(--line); background:var(--bg); color:var(--ink); width:min(420px,100%) }
"""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _ts(value: float | None) -> str:
    return datetime.fromtimestamp(value, UTC).strftime("%d %b %H:%M:%S") if value else ""


def _chip(text: str, cls: str | None = None) -> str:
    return f'<span class="chip {_e(cls or text)}">{_e(text)}</span>'


def _page(title: str, body: str, refresh: bool) -> str:
    meta = '<meta http-equiv="refresh" content="5">' if refresh else ""
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f"{meta}<title>{_e(title)}</title><style>{STYLE}</style></head><body>{body}</body></html>"
    )


def render_console(engine: Any) -> str:
    runs = engine.list_runs(40)
    counts: dict[str, int] = {}
    for r in runs:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    approvals = engine.approvals.list("pending")
    exceptions = engine.reporter.list("open")
    chain = engine.audit.verify()
    spend = engine.costs.day_total()

    kpis = "".join(
        f'<div class="kpi"><b>{v}</b><span>{_e(k)}</span></div>'
        for k, v in [
            ("runs running", counts.get("running", 0)),
            ("runs waiting on people", counts.get("waiting", 0)),
            ("runs succeeded", counts.get("succeeded", 0)),
            ("runs failed", counts.get("failed", 0)),
            ("approvals pending", len(approvals)),
            ("open exceptions", len(exceptions)),
            ("model spend today", f"${spend:.4f}"),
        ]
    )

    approval_rows = "".join(
        f"<tr><td>{_chip(a['risk_class'].replace('_', ' '), 'waiting')}</td><td><code>{_e(a['action'])}</code><br>{_e(a['preview'].get('summary'))}</td>"
        f"<td>{_e(', '.join(a['preview'].get('reasons', [])[1:]) or a['preview'].get('reasons', [''])[0])}</td>"
        f"<td>{_e(', '.join(a['required_roles']))}</td><td class='mono'>{_e(a['run_id'])}</td>"
        f"<td><a href='/approvals/{_e(a['id'])}/review'>Review</a></td></tr>"
        for a in approvals
    )
    approvals_html = (
        f"<table><tr><th>Class</th><th>Action</th><th>Why a person decides</th><th>Who can approve</th><th>Run</th><th></th></tr>{approval_rows}</table>"
        if approvals
        else '<div class="empty">Nothing is waiting for approval.</div>'
    )

    exception_rows = "".join(
        f"<tr><td>{_chip(x['severity'])}</td><td><code>{_e(x['kind'])}</code></td><td>{_e(x['message'])}<br><span class='muted'>{_e(x.get('suggested_action'))}</span></td>"
        f"<td class='mono'>{_e(x['run_id'])}</td><td>{_ts(x['created_at'])}</td></tr>"
        for x in exceptions
    )
    exceptions_html = (
        f"<table><tr><th>Severity</th><th>Kind</th><th>What happened / next step</th><th>Run</th><th>When</th></tr>{exception_rows}</table>"
        if exceptions
        else '<div class="empty">No open exceptions.</div>'
    )

    run_rows = []
    for r in runs:
        chips = "".join(_chip(s["name"], s["status"]) for s in engine.steps(r["id"]))
        run_rows.append(
            f"<tr><td class='mono'>{_e(r['id'])}</td><td>{_e(r['workflow'])}</td><td>{_chip(r['status'])}</td>"
            f"<td>{chips}</td><td>${engine.costs.run_total(r['id']):.4f}</td><td>{_ts(r['created_at'])}</td></tr>"
        )
    runs_html = (
        f"<table><tr><th>Run</th><th>Workflow</th><th>Status</th><th>Steps</th><th>Model cost</th><th>Started</th></tr>{''.join(run_rows)}</table>"
        if run_rows
        else '<div class="empty">No runs yet. Submit one with <code>switchyard submit bakery</code>.</div>'
    )

    audit_rows = "".join(
        f"<tr><td class='mono'>{e['seq']}</td><td>{_ts(e['ts'])}</td><td class='mono'>{_e(e['actor'])}</td><td><code>{_e(e['event'])}</code></td>"
        f"<td class='mono'>{_e(e['subject'])}</td><td class='mono muted'>{_e(json.dumps(e['data'])[:140])}</td></tr>"
        for e in engine.audit.entries(limit=30)
    )
    chain_text = (
        f"audit chain intact ({chain.checked} entries)"
        if chain.ok
        else f"AUDIT CHAIN BROKEN at #{chain.first_bad_seq}: {chain.reason}"
    )

    body = f"""
<header><h1>Switchyard · operations console</h1><span class="meta">mode: {_e(engine.settings.mode)} · {_e(chain_text)} · refreshes every 5 s</span></header>
<main>
<div class="kpis">{kpis}</div>
<section><h2>Waiting for a person</h2>{approvals_html}</section>
<section><h2>Open exceptions</h2>{exceptions_html}</section>
<section><h2>Runs</h2>{runs_html}</section>
<section><h2>Audit log (latest)</h2><table><tr><th>#</th><th>When</th><th>Actor</th><th>Event</th><th>Subject</th><th>Data</th></tr>{audit_rows}</table></section>
</main>"""
    return _page("Switchyard console", body, refresh=True)


def render_approval(engine: Any, approval: dict[str, Any]) -> str:
    preview = approval["preview"]
    payload = preview.get("payload", {})
    if "body" in payload:
        content = f"<p><b>To:</b> {_e(', '.join(payload.get('to', [])))}<br><b>Subject:</b> {_e(payload.get('subject'))}</p><pre>{_e(payload['body'])}</pre>"
    else:
        content = f"<pre>{_e(json.dumps(payload, indent=2, ensure_ascii=False))}</pre>"
    reasons = "".join(f"<li>{_e(r)}</li>" for r in preview.get("reasons", []))
    decided = (
        ""
        if approval["status"] == "pending"
        else f"<p>{_chip(approval['status'])} by {_e(approval['decided_by'])} at {_ts(approval['decided_at'])}</p>"
    )
    body = f"""
<header><h1>Approval {_e(approval["id"])}</h1><span class="meta"><a href="/">← console</a> · run {_e(approval["run_id"])}</span></header>
<main>
<section><h2>{_e(approval["action"])} · {_e(approval["risk_class"].replace("_", " "))}</h2>
<p>Why this needs a person:</p><ul>{reasons}</ul>
<p class="muted">Who can approve: {_e(", ".join(approval["required_roles"]))}. This decision is bound to payload hash
<code>{_e(approval["payload_hash"][:16])}…</code>; if the content changes, the approval no longer counts.</p>
{decided}</section>
<section><h2>Exactly what will be sent</h2>{content}</section>
<section><h2>Decide</h2>
<p><input id="token" type="password" placeholder="Your approver token" autocomplete="off"></p>
<p><input id="note" placeholder="Note (optional)"></p>
<p><button class="primary" onclick="decide('approve')">Approve and send</button> <button onclick="decide('reject')">Reject</button></p>
<p id="out" class="muted"></p></section>
</main>
<script>
async function decide(decision) {{
  const r = await fetch('/approvals/{_e(approval["id"])}/decision', {{method: 'POST',
    headers: {{'Content-Type': 'application/json', 'Authorization': 'Bearer ' + document.getElementById('token').value}},
    body: JSON.stringify({{decision, note: document.getElementById('note').value || null}})}});
  const data = await r.json();
  document.getElementById('out').textContent = r.ok ? ('Recorded: ' + data.status + ' by ' + data.decided_by) : ('Not recorded: ' + (data.detail || r.status));
}}
</script>"""
    return _page(f"Approval {approval['id']}", body, refresh=False)
