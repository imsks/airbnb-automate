"""Server-rendered dashboard.

One dependency-free HTML page on purpose. This is the only surface that shows a
problem, so it should never be broken by a frontend build step. It refreshes
itself, which is what replaces watching the worker's terminal output.
"""

from __future__ import annotations

from html import escape
from urllib.parse import urlsplit

_FUNNEL_ORDER = [
    ("discovered", "Discovered"),
    ("qualified", "Qualified"),
    ("contacted", "Contacted"),
    ("host_replied", "Host replied"),
    ("negotiating", "Negotiating"),
    ("terms_agreed", "Terms agreed"),
    ("ready_to_book", "Ready to book"),
    ("booked", "Booked"),
    ("stayed", "Stayed"),
    ("content_delivered", "Delivered"),
]

REFRESH_SECONDS = 10

_STYLE = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin:0; padding:2rem; background:#0f1115; color:#e6e6e6;
       font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; }
h1 { margin:0 0 .25rem; font-size:1.5rem; }
h2 { font-size:1rem; text-transform:uppercase; letter-spacing:.08em;
     color:#8b93a7; margin:2rem 0 .75rem; }
.sub { color:#8b93a7; margin-bottom:1.5rem; }
.cards { display:flex; gap:1rem; flex-wrap:wrap; margin-top:1rem; }
.card { background:#171a21; border:1px solid #242835; border-radius:10px;
        padding:1rem 1.25rem; min-width:140px; }
.card .n { font-size:1.9rem; font-weight:600; }
.card .l { color:#8b93a7; font-size:.75rem; text-transform:uppercase;
           letter-spacing:.06em; }
.hero { border-color:#2f6f4f; } .hero .n { color:#4ade80; }
table { width:100%; border-collapse:collapse; background:#171a21;
        border:1px solid #242835; border-radius:10px; overflow:hidden; }
th,td { padding:.6rem .9rem; text-align:left; border-bottom:1px solid #242835; }
th { color:#8b93a7; font-size:.72rem; text-transform:uppercase; letter-spacing:.06em; }
tr:last-child td { border-bottom:none; }
.alert { background:#2b1a1a; border:1px solid #6b2b2b; color:#ffb4b4;
         padding:.7rem 1rem; border-radius:8px; margin-bottom:.5rem; }
.ok { color:#4ade80; } .off { color:#ff6b6b; font-weight:600; }
.empty { color:#8b93a7; font-style:italic; }
a { color:#7aa2ff; }
.funnel { display:flex; gap:.4rem; flex-wrap:wrap; }
.step { background:#171a21; border:1px solid #242835; border-radius:8px;
        padding:.5rem .8rem; min-width:86px; }
.step .n { font-size:1.15rem; font-weight:600; } .step .l { color:#8b93a7; font-size:.7rem; }
.panel { background:#171a21; border:1px solid #242835; border-radius:10px; padding:1.25rem; }
.row { display:flex; gap:.75rem; flex-wrap:wrap; margin-bottom:.75rem; }
label { display:block; color:#8b93a7; font-size:.72rem; text-transform:uppercase;
        letter-spacing:.06em; margin-bottom:.3rem; }
input,textarea { width:100%; background:#0f1115; color:#e6e6e6; padding:.55rem .7rem;
        border:1px solid #2c3242; border-radius:7px; font:inherit; font-size:.9rem; }
textarea { min-height:104px; resize:vertical; font-family:ui-monospace,monospace; }
.field { flex:1; min-width:150px; }
button { background:#2f6f4f; color:#fff; border:0; border-radius:7px;
         padding:.6rem 1.1rem; font:inherit; font-weight:600; cursor:pointer; }
button:hover { filter:brightness(1.15); }
button.ghost { background:#242835; }
button.danger { background:#6b2b2b; }
.drawer { margin:2rem 0; border:1px solid #2a2f3a; border-radius:10px; padding:0 1.1rem; }
.drawer > summary { cursor:pointer; padding:1rem 0; font-weight:600; color:#9aa4b2; list-style:none; }
.drawer > summary::-webkit-details-marker { display:none; }
.drawer > summary::before { content:"\\25B8 "; }
.drawer[open] > summary::before { content:"\\25BE "; }
.linkbtn { display:inline-flex; align-items:center; background:#242835; color:#e6e6e6;
           text-decoration:none; border-radius:7px; padding:.6rem 1.1rem; font-weight:600; }
.linkbtn:hover { filter:brightness(1.15); }
.nav { display:flex; gap:.4rem; flex-wrap:wrap; margin:0 0 1.25rem; }
.navlink { display:inline-flex; align-items:center; background:#171a21; color:#c5cbe0;
           text-decoration:none; border:1px solid #242835; border-radius:999px;
           padding:.4rem .85rem; font-size:.85rem; font-weight:600; }
.navlink.on { background:#2f6f4f; color:#fff; border-color:#2f6f4f; }
.navlink:hover { filter:brightness(1.15); }
td.actions { white-space:nowrap; }
td.actions button { padding:.35rem .7rem; font-size:.8rem; margin-right:.35rem; }
td.actions button:last-child { margin-right:0; }
.feed { background:#171a21; border:1px solid #242835; border-radius:10px;
        max-height:340px; overflow-y:auto; font:12.5px/1.6 ui-monospace,monospace; }
.feed div { padding:.3rem .9rem; border-bottom:1px solid #1d212b; white-space:pre-wrap; }
.feed div:last-child { border-bottom:none; }
.feed .t { color:#5c6479; } .feed .s { color:#7aa2ff; }
.feed .warn { color:#ffcf8b; } .feed .err { color:#ff8b8b; }
.hint { color:#5c6479; font-size:.8rem; margin-top:.5rem; }
.message-card { margin:.6rem 0; }
.message-card summary { cursor:pointer; font-weight:600; }
.message-draft { white-space:pre-wrap; overflow-wrap:anywhere; font:inherit; }
.loops { display:grid; grid-template-columns:1fr 1fr; gap:1rem; }
.loop { background:#12151c; border:1px solid #242835; border-radius:12px; padding:1rem; }
.loop h3 { margin:0 0 .6rem; font-size:.8rem; text-transform:uppercase;
           letter-spacing:.06em; color:#7aa2ff; }
.strip { display:flex; gap:.6rem; flex-wrap:wrap; margin:1rem 0 .25rem; }
.pill { display:inline-flex; align-items:center; gap:.5rem; background:#171a21;
        border:1px solid #242835; border-radius:999px; padding:.5rem 1rem; }
.pill b { font-weight:700; font-size:1.05rem; }
.pill .k { color:#8b93a7; font-size:.7rem; text-transform:uppercase; letter-spacing:.05em; }
.pill.hot { border-color:#2f6f4f; background:#12211a; } .pill.hot b { color:#4ade80; }
.pill.warn { border-color:#6b5326; background:#211d12; } .pill.warn b { color:#ffcf8b; }
.pill.off { border-color:#6b2b2b; background:#211414; } .pill.off b { color:#ff6b6b; }
.feed .dot { display:inline-block; width:.5rem; height:.5rem; border-radius:50%;
        margin-right:.5rem; vertical-align:middle; background:#4ade80; }
.feed .dot.leased { background:#ffcf8b; }
.feed .dot.failed, .feed .dot.cancelled { background:#ff8b8b; }
.feed .dot.pending { background:#5c6479; }
@media (max-width: 720px) {
  body { padding:1rem; }
  h1 { font-size:1.35rem; }
  .loops { grid-template-columns:1fr; }
  .cards, .funnel { gap:.5rem; }
  .card { min-width:calc(50% - .5rem); flex:1; }
  .row { gap:.5rem; } .row button { flex:1 1 auto; padding:.7rem .8rem; }
  .strip { gap:.5rem; } .pill { flex:1 1 calc(50% - .5rem); justify-content:space-between; }
}
"""


def _e(value) -> str:
    return escape(str(value if value is not None else ""))


def _cards(brief: dict) -> str:
    star, budget = brief["north_star"], brief["budget"]
    return f"""
    <div class="cards">
      <div class="card hero"><div class="n">{star['closes_per_100_messages']}</div>
        <div class="l">Closes / 100 msgs</div></div>
      <div class="card"><div class="n">{star['reply_rate_pct']}%</div>
        <div class="l">Reply rate</div></div>
      <div class="card"><div class="n">{star['messages_sent']}</div>
        <div class="l">Messages sent</div></div>
      <div class="card"><div class="n">{star['deals_closed']}</div>
        <div class="l">Deals closed</div></div>
      <div class="card"><div class="n">{budget['remaining']}/{budget['max']}</div>
        <div class="l">Sends left</div></div>
      <div class="card"><div class="n {'ok' if budget['sending_enabled'] else 'off'}">
        {'LIVE' if budget['sending_enabled'] else 'FROZEN'}</div>
        <div class="l">Sending</div></div>
    </div>"""


def _nav(current: str) -> str:
    """Page links. The dashboard itself is only the stats."""
    items = (
        ("/", "Stats"),
        ("/messages", "Messages"),
        ("/attention", "Needs a human"),
        ("/ready", "Ready to book"),
        ("/loops", "Loops"),
        ("/logs", "Logs"),
        ("/leads", "Leads"),
    )
    links = "".join(
        f'<a class="navlink{" on" if href == current else ""}" href="{href}">{label}</a>'
        for href, label in items
    )
    return f'<nav class="nav">{links}</nav>'


def _controls(sending_enabled: bool) -> str:
    toggle = (
        '<button class="danger" onclick="post(\'/api/kill-switch/freeze\')">Freeze sending</button>'
        if sending_enabled
        else '<button onclick="post(\'/api/kill-switch/resume\')">Resume sending</button>'
    )
    return f'<div class="row" style="margin-top:1.25rem">{toggle}</div>'


def _office_panel(campaigns: list) -> str:
    """Read-only view of the standing office. There is no form to fill in.

    The office proposes places, researches them, drafts messages and negotiates
    on its own. A human never seeds a destination list, so this only reports
    what is already running.
    """
    note = (
        '<p class="hint">The standing office runs itself: it proposes new places '
        "all over India and abroad, researches them, drafts the outreach and "
        "negotiates \u2014 no dates, no destination list, nothing to set up. You only "
        "step in to pay, to sign back in, or when a host asks for something the "
        "rules will not allow.</p>"
    )
    if not campaigns:
        return (
            '<p class="empty">Starting up \u2014 the office will appear here the moment '
            "a worker comes online.</p>" + note
        )
    rows = "".join(
        f"<tr><td>{_e(c.name)}</td><td>{_e(c.goal)}</td><td>{_e(c.status.value)}</td>"
        f"<td><a href='/api/campaigns/{c.id}/itinerary'>Itinerary</a></td></tr>"
        for c in campaigns
    )
    return (
        "<table><tr><th>Office</th><th>Goal</th><th>Status</th><th></th></tr>"
        f"{rows}</table>" + note
    )


def _funnel(counts: dict) -> str:
    steps = "".join(
        f'<div class="step"><div class="n">{counts.get(key, 0)}</div>'
        f'<div class="l">{label}</div></div>'
        for key, label in _FUNNEL_ORDER
    )
    return f'<div class="funnel">{steps}</div>'


def _activity(records: list) -> str:
    if not records:
        return '<p class="empty">Nothing yet \u2014 create a campaign to give the agents work.</p>'
    rows = []
    for r in records:
        level = r.get("level", "INFO")
        cls = "err" if level == "ERROR" else "warn" if level == "WARNING" else ""
        stamp = _e(r.get("at", ""))[11:19]
        rows.append(
            f'<div><span class="t">{stamp}</span>  '
            f'<span class="s">{_e(r.get("source", ""))}</span>  '
            f'<span class="{cls}">{_e(r.get("message", ""))}</span></div>'
        )
    return f'<div class="feed">{"".join(rows)}</div>'


def _status_strip(brief: dict) -> str:
    """The four things worth a glance on a phone: are we sending, how much
    budget is left, and how many items are actually waiting on the human."""
    budget = brief["budget"]
    queues = brief["queues"]
    ready = len(queues.get("ready_to_book", []))
    needs = len(queues.get("needs_human", []))
    sending = budget["sending_enabled"]

    def pill(value, key, cls="") -> str:
        return f'<span class="pill {cls}"><b>{_e(value)}</b><span class="k">{_e(key)}</span></span>'

    return (
        '<div class="strip">'
        + pill("LIVE" if sending else "FROZEN", "sending", "hot" if sending else "off")
        + pill(f"{budget['remaining']}/{budget['max']}", "sends left")
        + pill(ready, "ready to book", "hot" if ready else "")
        + pill(needs, "needs you", "warn" if needs else "")
        + "</div>"
    )


_DOT_STATUSES = frozenset({"leased", "failed", "cancelled", "pending"})


def _loop_feed(records: list) -> str:
    """One loop's recent activity, derived from the durable job record."""
    if not records:
        return '<p class="empty">Nothing yet.</p>'
    rows = []
    for r in records:
        status = str(r.get("status", ""))
        cls = "err" if status in ("failed", "cancelled") else "warn" if status == "leased" else ""
        dot = status if status in _DOT_STATUSES else ""
        stamp = _e(str(r.get("when", "")))[11:19]
        rows.append(
            f'<div><span class="dot {dot}"></span><span class="t">{stamp}</span>  '
            f'<span class="s">{_e(r.get("label", ""))}</span>  '
            f'<span class="{cls}">{_e(str(r.get("detail", "")))}</span></div>'
        )
    return f'<div class="feed">{"".join(rows)}</div>'


def _loops(office: list, courier: list) -> str:
    """Office and Courier side by side, stacking on a phone."""
    return (
        '<div class="loops">'
        '<div class="loop"><h3>Office \u2014 plans &amp; writes</h3>'
        f"{_loop_feed(office)}</div>"
        '<div class="loop"><h3>Courier \u2014 sends &amp; negotiates</h3>'
        f"{_loop_feed(courier)}</div>"
        "</div>"
    )


def _ready_table(rows: list) -> str:
    if not rows:
        return '<p class="empty">Nothing waiting on you.</p>'
    body = "".join(
        f"<tr><td>{_e(r['host'])}</td><td>{_e(r['place'])}</td>"
        f"<td>{_e(r['location'])}</td>"
        f"<td>{_e(r['discount_pct'] or '')}{'%' if r['discount_pct'] else ''}</td>"
        f"<td>{_e(r['window'])}</td>"
        f"<td><a href=\"{_e(r['url'])}\" target=\"_blank\" rel=\"noopener\">Open</a></td></tr>"
        for r in rows
    )
    return (
        "<table><tr><th>Host</th><th>Place</th><th>Location</th>"
        f"<th>Discount</th><th>Window</th><th></th></tr>{body}</table>"
    )


def _needs_human_table(rows: list) -> str:
    if not rows:
        return '<p class="empty">No live threads need you.</p>'
    body = ""
    for r in rows:
        did = r["deal_id"]
        actions = (
            f'<button class="ghost" onclick="dealAction({did},\'retry\')">Retry</button>'
            f'<button class="ghost" onclick="dealAction({did},\'kill\')">Kill</button>'
            f'<button class="danger" onclick="dealDelete({did})">Delete</button>'
        )
        body += (
            f"<tr><td>{_e(r['host'])}</td><td>{_e(r['place'])}</td>"
            f"<td>{_e(r['reason'])}</td>"
            f"<td><a href=\"{_e(r['url'])}\" target=\"_blank\" rel=\"noopener\">Open</a></td>"
            f'<td class="actions">{actions}</td></tr>'
        )
    return (
        "<table><tr><th>Host</th><th>Place</th><th>Why</th><th></th>"
        f"<th>Actions</th></tr>{body}</table>"
    )


def _prompt_table(rows: list) -> str:
    if not rows:
        return '<p class="empty">No sends attributed to a prompt version yet.</p>'
    body = "".join(
        f"<tr><td>{_e(r['prompt_version'])}</td><td>{r['sent']}</td>"
        f"<td>{r['replied']}</td><td>{r['reply_rate_pct']}%</td></tr>"
        for r in rows
    )
    return (
        "<table><tr><th>Prompt version</th><th>Sent</th><th>Replied</th>"
        f"<th>Reply rate</th></tr>{body}</table>"
    )


def _cost_table(rows: list) -> str:
    if not rows:
        return '<p class="empty">No agent runs yet.</p>'
    body = "".join(
        f"<tr><td>{_e(r['agent'])}</td><td>{r['runs']}</td>"
        f"<td>{r['tokens'] or 0:,}</td><td>${(r['cost_usd'] or 0):.4f}</td>"
        f"<td>{r['failures'] or 0}</td></tr>"
        for r in rows
    )
    return (
        "<table><tr><th>Agent</th><th>Runs</th><th>Tokens</th><th>Cost</th>"
        f"<th>Failures</th></tr>{body}</table>"
    )


def _message_cards(rows: list) -> str:
  if not rows:
    return '<p class="empty">No outreach drafts yet. Generated text and delivery results appear here.</p>'
  cards = []
  for row in rows:
    label = "Delivery unconfirmed — inspect before retrying" if row["status"] == "sending" else row["status"]
    thread_link = ""
    url = row.get("thread_url") or ""
    parsed = urlsplit(url)
    if parsed.scheme == "https" and parsed.hostname in ("www.airbnb.com", "www.airbnb.co.in"):
      thread_link = f'<a href="{_e(url)}" target="_blank" rel="noopener">View Airbnb conversation</a>'
    reason = row.get("error") or row.get("blocked_reason") or ""
    cards.append(
      f'<details class="panel message-card"><summary>Message #{row["id"]} · '
      f'{_e(row["host_name"] or "Host")} · {_e(row["place_name"])} · {_e(label)}</summary>'
      f'<p>{_e(row["location"])} · {_e(row.get("prompt_version", ""))}</p>'
      f'<pre class="message-draft">{_e(row["body"])}</pre>'
      f'<p>{_e(reason)}</p>{thread_link}</details>'
    )
  return "".join(cards)


_SCRIPT = """
async function post(url, body) {
  const r = await fetch(url, {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    body: body ? JSON.stringify(body) : undefined
  });
  if (!r.ok) { alert('Failed: ' + await r.text()); return null; }
  return r.json();
}
// Needs-a-human row actions.
async function dealAction(id, verb) {
  if (await post('/api/deals/' + id + '/' + verb) !== null) location.reload();
}
async function dealDelete(id) {
  if (!confirm('Delete this deal permanently?')) return;
  const r = await fetch('/api/deals/' + id, {method: 'DELETE'});
  if (!r.ok) { alert('Failed: ' + await r.text()); return; }
  location.reload();
}
// The office runs itself, so the dashboard is read-only: just keep it fresh,
// but never reload while a message card is open for reading.
setInterval(() => {
  if (!document.querySelector('.message-card[open]')) location.reload();
}, REFRESH_MS);
"""


def _job_cards(jobs: dict) -> str:
    cards = "".join(
        f'<div class="card"><div class="n">{v}</div><div class="l">{_e(k)}</div></div>'
        for k, v in sorted(jobs.items())
    )
    return f'<div class="cards">{cards}</div>' if cards else '<p class="empty">Queue is empty.</p>'


def render_dashboard(brief: dict) -> str:
    """The stats page: rates, pipeline, queue depth, and spend. Nothing else."""
    alerts = "".join(f'<div class="alert">{_e(a)}</div>' for a in brief["anomalies"])
    act = brief["activity"]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Airbnb Automate</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{_STYLE}</style></head>
<body>
  {_nav("/")}
  <h1>Airbnb Automate</h1>
  <div class="sub">Last 24h: {act['messages_sent']} sent,
    {act['host_replies']} replies, {act['messages_blocked']} blocked
    &middot; auto-refreshes every {REFRESH_SECONDS}s</div>

  {alerts}
  {_status_strip(brief)}
  {_controls(brief['budget']['sending_enabled'])}

  <h2>At a glance</h2>
  {_cards(brief)}
  <h2>Pipeline</h2>
  {_funnel(brief['funnel'])}
  <h2>Job queue</h2>
  {_job_cards(brief['jobs'])}
  <h2>Prompt performance</h2>
  {_prompt_table(brief['prompt_performance'])}
  <h2>Agent cost</h2>
  {_cost_table(brief['cost_by_agent'])}

<script>{_SCRIPT.replace('REFRESH_MS', str(REFRESH_SECONDS * 1000))}</script>
</body></html>"""


def render_messages(rows: list) -> str:
    """Drafts and whether each one was delivered."""
    header = (
        "<h1>Messages</h1>"
        f'<div class="sub">{len(rows)} draft(s) and delivery result(s)</div>'
        "<h2>Messages — drafts and delivery</h2>"
    )
    return _shell("Messages — Airbnb Automate", header + _message_cards(rows), current="/messages")


def render_attention(rows: list) -> str:
    """Deals a person has to handle."""
    header = (
        "<h1>Needs a human</h1>"
        f'<div class="sub">{len(rows)} waiting</div>'
    )
    return _shell("Needs a human — Airbnb Automate", header + _needs_human_table(rows), current="/attention")


def render_ready(rows: list) -> str:
    """Deals that are agreed and waiting on a card."""
    header = (
        "<h1>Ready to book</h1>"
        f'<div class="sub">{len(rows)} need your card</div>'
    )
    return _shell(
        "Ready to book — Airbnb Automate",
        header + _ready_table(rows),
        current="/ready",
    )


def render_loops(office: list, courier: list, campaigns: list) -> str:
    """What the office and the courier are doing, plus the standing office."""
    body = (
        "<h1>Loops</h1>"
        '<div class="sub">What is running now</div>'
        "<h2>Loops — what's running now</h2>"
        f"{_loops(list(office), list(courier))}"
        "<h2>Standing office</h2>"
        f"{_office_panel(list(campaigns))}"
    )
    return _shell("Loops — Airbnb Automate", body, current="/loops")


def render_logs(records: list) -> str:
    """Recent worker log lines."""
    body = (
        "<h1>Logs</h1>"
        '<div class="sub">Recent agent output</div>'
        "<h2>Logs</h2>"
        f"{_activity(list(records))}"
    )
    return _shell("Logs — Airbnb Automate", body, current="/logs")


def _shell(title: str, body: str, *, current: str = "") -> str:
    """A standalone page using the same style and auto-refresh as the dashboard."""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{_e(title)}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{_STYLE}</style></head>
<body>
{_nav(current)}
{body}
<script>{_SCRIPT.replace('REFRESH_MS', str(REFRESH_SECONDS * 1000))}</script>
</body></html>"""


def _lead_status(row: dict) -> str:
    """A short, human label for where a lead is: sent, drafted, blocked, or new."""
    msg = row.get("last_message_status")
    state = row.get("deal_state") or ""
    if msg in ("sent", "sending"):
        return "contacted"
    if msg == "blocked":
        return "blocked (retrying)"
    if msg == "pending":
        return "drafted"
    if state in ("negotiating", "host_replied", "terms_agreed", "ready_to_book"):
        return state.replace("_", " ")
    return "new"


def render_leads(rows: list) -> str:
    """The dedicated Leads page: every lead the office has found, best first."""
    if not rows:
        table = (
            '<p class="empty">No leads yet — the office is still discovering and '
            "enriching. This fills in on its own.</p>"
        )
    else:
        body = ""
        for r in rows:
            score = r.get("collab_fit_score")
            score_txt = f"{score:.2f}" if score is not None else "—"
            enriched = "yes" if r.get("detail_scraped_at") else "no"
            price = r.get("price_per_night") or 0
            cur = r.get("currency") or ""
            title = r.get("title") or r.get("listing_id") or "listing"
            body += (
                f"<tr><td><a href=\"/leads/{r['lead_id']}\">{_e(title)}</a></td>"
                f"<td>{_e(r.get('location') or '')}</td>"
                f"<td>{_e(cur)} {int(price):,}</td>"
                f"<td>{_e(r.get('rating') or '')}</td>"
                f"<td>{_e(r.get('host_name') or '')}</td>"
                f"<td>{_e(score_txt)}</td>"
                f"<td>{_e(enriched)}</td>"
                f"<td>{_e(_lead_status(r))}</td></tr>"
            )
        table = (
            "<table><tr><th>Listing</th><th>Location</th><th>Price/night</th>"
            "<th>Rating</th><th>Host</th><th>Fit</th><th>Enriched</th>"
            f"<th>Status</th></tr>{body}</table>"
        )
    header = (
        '<h1>Leads</h1>'
        f'<div class="sub">{len(rows)} lead(s) &middot; '
        '<a href="/">&larr; back to dashboard</a></div>'
    )
    return _shell("Leads — Airbnb Automate", header + table, current="/leads")


def _portal_context_block(context: list) -> str:
    """Render read-only corroboration pulled from other portals, if any."""
    if not context:
        return (
            '<p class="empty">No external context yet. The office pulls Booking.com '
            "corroboration for enriched leads on its own.</p>"
        )
    blocks = ""
    for c in context:
        payload = c.get("payload") or {}
        conf = c.get("match_confidence") or 0
        reviews = payload.get("review_excerpts") or []
        amenities = payload.get("amenities") or []
        price_band = payload.get("price_band") or ""
        rating = payload.get("rating")
        bits = ""
        if rating:
            bits += f"<p><b>Rating:</b> {_e(rating)}</p>"
        if price_band:
            bits += f"<p><b>Price band:</b> {_e(price_band)}</p>"
        if amenities:
            bits += f"<p><b>Amenities:</b> {_e(', '.join(map(str, amenities[:12])))}</p>"
        if reviews:
            items = "".join(f"<li>{_e(rv)}</li>" for rv in reviews[:5])
            bits += f"<p><b>Guest reviews:</b></p><ul>{items}</ul>"
        url = c.get("external_url") or ""
        link = f' &middot; <a href="{_e(url)}" target="_blank" rel="noopener">source</a>' if url else ""
        blocks += (
            f'<div class="panel" style="margin:.6rem 0">'
            f"<h3>{_e(str(c.get('portal', 'portal')).title())} "
            f'<span class="hint">(match {int(conf * 100)}%{link})</span></h3>'
            f"{bits or '<p class=empty>No detail captured.</p>'}</div>"
        )
    return blocks


def render_lead_detail(lead, listing, context: list) -> str:
    """One lead in full: listing, the office's enrichment, and portal context."""
    li_title = getattr(listing, "title", "") or (getattr(lead, "listing_id", "") or "Lead")
    location = getattr(listing, "location", "") or ""
    host = getattr(listing, "host_name", "") or ""
    price = getattr(listing, "price_per_night", 0) or 0
    currency = getattr(listing, "currency", "") or ""
    rating = getattr(listing, "rating", 0) or 0
    url = getattr(listing, "url", "") or ""
    score = getattr(lead, "collab_fit_score", None)
    score_txt = f"{score:.2f}" if score is not None else "not scored yet"

    summary = (
        f'<div class="cards">'
        f'<div class="card hero"><div class="n">{_e(score_txt)}</div>'
        '<div class="l">Collab fit</div></div>'
        f'<div class="card"><div class="n">{_e(currency)} {int(price):,}</div>'
        '<div class="l">Price / night</div></div>'
        f'<div class="card"><div class="n">{_e(rating)}</div>'
        '<div class="l">Rating</div></div>'
        f'<div class="card"><div class="n">{_e(host or "—")}</div>'
        '<div class="l">Host</div></div>'
        "</div>"
    )

    def section(title: str, value) -> str:
        if not value:
            return ""
        if isinstance(value, (list, tuple)):
            inner = "".join(f"<li>{_e(v)}</li>" for v in value)
            content = f"<ul>{inner}</ul>"
        else:
            content = f'<p class="message-draft">{_e(value)}</p>'
        return f"<h2>{_e(title)}</h2>{content}"

    enrichment = (
        section("Description", getattr(lead, "description", ""))
        + section("House rules", getattr(lead, "house_rules", ""))
        + section("Amenities", getattr(lead, "amenities", []))
        + section("Guest reviews", getattr(lead, "review_excerpts", []))
        + section("Host bio", getattr(lead, "host_bio", ""))
    ) or '<p class="empty">Not enriched yet — the detail page has not been scraped.</p>'

    link = f' &middot; <a href="{_e(url)}" target="_blank" rel="noopener">open on Airbnb</a>' if url else ""
    header = (
        f"<h1>{_e(li_title)}</h1>"
        f'<div class="sub">{_e(location)}{link} &middot; '
        '<a href="/leads">&larr; all leads</a></div>'
    )
    body = (
        header
        + summary
        + "<h2>What the office knows</h2>"
        + enrichment
        + "<h2>External context</h2>"
        + _portal_context_block(context)
    )
    return _shell(f"{li_title} — Lead", body, current="/leads")
