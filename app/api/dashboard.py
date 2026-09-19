"""Server-rendered dashboard.

Kept as one dependency-free template on purpose: this is the only surface that
surfaces a problem, so it should never be broken by a frontend build step.
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

_STYLE = """
:root { color-scheme: dark; }
body { margin:0; padding:2rem; background:#0f1115; color:#e6e6e6;
       font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; }
h1 { margin:0 0 .25rem; font-size:1.5rem; }
h2 { font-size:1rem; text-transform:uppercase; letter-spacing:.08em;
     color:#8b93a7; margin:2rem 0 .75rem; }
.sub { color:#8b93a7; margin-bottom:2rem; }
.cards { display:flex; gap:1rem; flex-wrap:wrap; margin-top:1rem; }
.card { background:#171a21; border:1px solid #242835; border-radius:10px;
        padding:1rem 1.25rem; min-width:150px; }
.card .n { font-size:1.9rem; font-weight:600; }
.card .l { color:#8b93a7; font-size:.8rem; text-transform:uppercase;
           letter-spacing:.06em; }
.hero { border-color:#2f6f4f; }
.hero .n { color:#4ade80; }
table { width:100%; border-collapse:collapse; background:#171a21;
        border:1px solid #242835; border-radius:10px; overflow:hidden; }
th,td { padding:.6rem .9rem; text-align:left; border-bottom:1px solid #242835; }
th { color:#8b93a7; font-size:.75rem; text-transform:uppercase;
     letter-spacing:.06em; }
tr:last-child td { border-bottom:none; }
.alert { background:#2b1a1a; border:1px solid #6b2b2b; color:#ffb4b4;
         padding:.7rem 1rem; border-radius:8px; margin-bottom:.5rem; }
.ok { color:#4ade80; }
.off { color:#ff6b6b; font-weight:600; }
.empty { color:#8b93a7; font-style:italic; }
a { color:#7aa2ff; }
.funnel { display:flex; gap:.4rem; flex-wrap:wrap; }
.step { background:#171a21; border:1px solid #242835; border-radius:8px;
        padding:.5rem .8rem; min-width:86px; }
.step .n { font-size:1.15rem; font-weight:600; }
.step .l { color:#8b93a7; font-size:.7rem; }
"""


def _e(value) -> str:
    return escape(str(value if value is not None else ""))


def _cards(brief: dict) -> str:
    star = brief["north_star"]
    budget = brief["budget"]
    return f"""
    <div class="cards">
      <div class="card hero">
        <div class="n">{star['closes_per_100_messages']}</div>
        <div class="l">Closes / 100 msgs</div>
      </div>
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


def _funnel(counts: dict) -> str:
    steps = "".join(
        f'<div class="step"><div class="n">{counts.get(key, 0)}</div>'
        f'<div class="l">{label}</div></div>'
        for key, label in _FUNNEL_ORDER
    )
    return f'<div class="funnel">{steps}</div>'


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


def render_dashboard(brief: dict) -> str:
    """Render the daily brief as a standalone HTML page."""
    alerts = "".join(f'<div class="alert">{_e(a)}</div>' for a in brief["anomalies"])
    activity = brief["activity"]
    queues = brief["queues"]

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Airbnb Automate</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{_STYLE}</style></head>
<body>
  <h1>Airbnb Automate</h1>
  <div class="sub">Generated {_e(brief['generated_at'])} ·
    last 24h: {activity['messages_sent']} sent,
    {activity['host_replies']} replies,
    {activity['messages_blocked']} blocked</div>

  {alerts}
  {_cards(brief)}

  <h2>Pipeline</h2>
  {_funnel(brief['funnel'])}

  <h2>Ready to book — needs your card</h2>
  {_ready_table(queues['ready_to_book'])}

  <h2>Needs a human</h2>
  {_needs_human_table(queues['needs_human'])}

  <h2>Prompt performance</h2>
  {_prompt_table(brief['prompt_performance'])}

  <h2>Agent cost</h2>
  {_cost_table(brief['cost_by_agent'])}

  <h2>Job queue</h2>
  <div class="cards">{''.join(
      f'<div class="card"><div class="n">{v}</div><div class="l">{_e(k)}</div></div>'
      for k, v in sorted(brief['jobs'].items())
  ) or '<p class="empty">Queue is empty.</p>'}</div>
</body></html>"""
