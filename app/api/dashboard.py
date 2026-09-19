"""Server-rendered dashboard.

One dependency-free HTML page on purpose. This is the only surface that shows a
problem, so it should never be broken by a frontend build step. It refreshes
itself, which is what replaces watching the worker's terminal output.
"""

from __future__ import annotations

from html import escape

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
.feed { background:#171a21; border:1px solid #242835; border-radius:10px;
        max-height:340px; overflow-y:auto; font:12.5px/1.6 ui-monospace,monospace; }
.feed div { padding:.3rem .9rem; border-bottom:1px solid #1d212b; white-space:pre-wrap; }
.feed div:last-child { border-bottom:none; }
.feed .t { color:#5c6479; } .feed .s { color:#7aa2ff; }
.feed .warn { color:#ffcf8b; } .feed .err { color:#ff8b8b; }
.hint { color:#5c6479; font-size:.8rem; margin-top:.5rem; }
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


def _controls(sending_enabled: bool) -> str:
    toggle = (
        '<button class="danger" onclick="post(\'/api/kill-switch/freeze\')">Freeze sending</button>'
        if sending_enabled
        else '<button onclick="post(\'/api/kill-switch/resume\')">Resume sending</button>'
    )
    return f"""
    <div class="row" style="margin-top:1.25rem">
      {toggle}
      <button class="ghost" onclick="post('/api/jobs/sync-inbox')">Sync inbox</button>
      <button class="ghost" onclick="post('/api/campaigns/0/tick')">Plan work now</button>
      <button class="ghost" onclick="location.reload()">Refresh</button>
    </div>"""


def _campaign_form(campaigns: list, suggested: list[str]) -> str:
    if campaigns:
        rows = "".join(
            f"<tr><td>{_e(c.name)}</td><td>{_e(c.window_start)} \u2192 {_e(c.window_end)}</td>"
            f"<td>{_e(c.origin)}</td><td>{_e(c.status.value)}</td>"
            f"<td><a href='/api/campaigns/{c.id}/itinerary'>Itinerary</a></td></tr>"
            for c in campaigns
        )
        return (
            "<table><tr><th>Campaign</th><th>Window</th><th>From</th><th>Status</th>"
            f"<th></th></tr>{rows}</table>"
            '<p class="hint">The worker is already planning against these. '
            "Add another below if you want a second trip running in parallel.</p>"
            + _new_campaign_panel(suggested, collapsed=True)
        )
    return _new_campaign_panel(suggested, collapsed=False)


def _new_campaign_panel(suggested: list[str], collapsed: bool) -> str:
    places = "\n".join(suggested)
    panel = f"""
    <div class="panel" id="campaign-panel" {'style="display:none"' if collapsed else ''}>
      <div class="row">
        <div class="field"><label>Name</label>
          <input id="c-name" placeholder="Winter tour"></div>
        <div class="field"><label>From</label>
          <input id="c-origin" placeholder="Delhi"></div>
      </div>
      <div class="row">
        <div class="field"><label>Window start</label>
          <input id="c-start" placeholder="2026-11"></div>
        <div class="field"><label>Window end</label>
          <input id="c-end" placeholder="2027-02"></div>
        <div class="field"><label>Nights per stay</label>
          <input id="c-nights" type="number" value="7"></div>
        <div class="field"><label>Guests</label>
          <input id="c-guests" type="number" value="2"></div>
      </div>
      <label>Destinations to consider \u2014 one per line</label>
      <textarea id="c-places" placeholder="Goa, India&#10;Gokarna, Karnataka">{_e(places)}</textarea>
      <p class="hint">These are candidates, not a plan. The Scout researches each
        one and the Router decides which make the route, and in which month.</p>
      <button onclick="createCampaign()">Create campaign &amp; start work</button>
    </div>"""
    if collapsed:
        return (
            '<button class="ghost" style="margin-top:.75rem" '
            "onclick=\"document.getElementById('campaign-panel').style.display='block'\">"
            "New campaign</button>" + panel
        )
    return panel


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
        return '<p class="empty">No blocked deals.</p>'
    body = "".join(
        f"<tr><td>{_e(r['host'])}</td><td>{_e(r['place'])}</td>"
        f"<td>{_e(r['reason'])}</td>"
        f"<td><a href=\"{_e(r['url'])}\" target=\"_blank\" rel=\"noopener\">Open</a></td></tr>"
        for r in rows
    )
    return f"<table><tr><th>Host</th><th>Place</th><th>Why</th><th></th></tr>{body}</table>"


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
async function createCampaign() {
  const val = id => document.getElementById(id).value.trim();
  if (!val('c-name')) { alert('Give the campaign a name.'); return; }
  const places = val('c-places').split('\\n').map(s => s.trim()).filter(Boolean);
  const res = await post('/api/campaigns', {
    name: val('c-name'), origin: val('c-origin'),
    window_start: val('c-start'), window_end: val('c-end'),
    stay_nights: parseInt(val('c-nights') || '7'),
    guests: parseInt(val('c-guests') || '2'),
    places: places
  });
  if (res) location.reload();
}
// Pause auto-refresh while typing so the form is never wiped mid-edit.
let typing = false;
document.addEventListener('input', () => { typing = true; });
setInterval(() => { if (!typing) location.reload(); }, REFRESH_MS);
"""


def render_dashboard(
    brief: dict,
    *,
    activity: list = (),
    campaigns: list = (),
    suggested_places: list = (),
) -> str:
    """Render the whole control surface as a standalone HTML page."""
    alerts = "".join(f'<div class="alert">{_e(a)}</div>' for a in brief["anomalies"])
    act = brief["activity"]
    queues = brief["queues"]
    jobs = brief["jobs"]
    job_cards = "".join(
        f'<div class="card"><div class="n">{v}</div><div class="l">{_e(k)}</div></div>'
        for k, v in sorted(jobs.items())
    ) or '<p class="empty">Queue is empty.</p>'

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Airbnb Automate</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{_STYLE}</style></head>
<body>
  <h1>Airbnb Automate</h1>
  <div class="sub">Last 24h: {act['messages_sent']} sent,
    {act['host_replies']} replies, {act['messages_blocked']} blocked
    &middot; auto-refreshes every {REFRESH_SECONDS}s</div>

  {alerts}
  {_cards(brief)}
  {_controls(brief['budget']['sending_enabled'])}

  <h2>Campaigns</h2>
  {_campaign_form(list(campaigns), list(suggested_places))}

  <h2>Live activity</h2>
  {_activity(list(activity))}

  <h2>Pipeline</h2>
  {_funnel(brief['funnel'])}

  <h2>Ready to book &mdash; needs your card</h2>
  {_ready_table(queues['ready_to_book'])}

  <h2>Needs a human</h2>
  {_needs_human_table(queues['needs_human'])}

  <h2>Job queue</h2>
  <div class="cards">{job_cards}</div>

  <h2>Prompt performance</h2>
  {_prompt_table(brief['prompt_performance'])}

  <h2>Agent cost</h2>
  {_cost_table(brief['cost_by_agent'])}

<script>{_SCRIPT.replace('REFRESH_MS', str(REFRESH_SECONDS * 1000))}</script>
</body></html>"""
