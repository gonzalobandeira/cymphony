"""Optional HTTP server for observability, control, and setup wizard."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from .orchestrator import Orchestrator

logger = logging.getLogger(__name__)

_CSS = """
  body{font-family:system-ui,sans-serif;margin:2rem;background:#0f0f10;color:#e0e0e0}
  h1{color:#a78bfa}
  h2{color:#7dd3fc;border-bottom:1px solid #333;padding-bottom:.3rem}
  a{color:#7dd3fc}
  table{border-collapse:collapse;width:100%;margin-bottom:2rem}
  th{text-align:left;background:#1e1e2e;color:#94a3b8;padding:.5rem .75rem}
  td{padding:.4rem .75rem;border-bottom:1px solid #1e1e2e}
  tr:hover td{background:#1a1a2e}
  .stat{display:inline-block;background:#1e1e2e;border-radius:6px;padding:.5rem 1rem;margin:.25rem}
  .stat span{font-size:1.5rem;font-weight:700;color:#a78bfa}
  .empty{color:#555;font-style:italic}
  input,select{width:100%;box-sizing:border-box;background:#1e1e2e;color:#e0e0e0;border:1px solid #444;
    border-radius:4px;padding:.5rem .75rem;font-size:1rem;margin-top:.25rem}
  input:focus,select:focus{outline:none;border-color:#a78bfa}
  label{color:#94a3b8;font-size:.85rem;display:block;margin-bottom:.15rem}
  .field{margin-bottom:1.25rem}
  .btn{background:#7c3aed;color:#fff;border:none;padding:.55rem 1.4rem;border-radius:4px;
    cursor:pointer;font-size:.95rem}
  .btn:hover{background:#6d28d9}
  .btn:disabled{opacity:.5;cursor:not-allowed}
  .btn-ghost{background:transparent;color:#94a3b8;border:1px solid #444}
  .btn-ghost:hover{background:#1e1e2e;color:#e0e0e0}
  .err{color:#f87171;font-size:.85rem;margin-top:.3rem}
  .ok{color:#4ade80;font-size:.85rem;margin-top:.3rem}
  .step{display:none}.step.active{display:block}
  .stepper{display:flex;gap:.5rem;margin-bottom:2rem;align-items:center}
  .sdot{width:28px;height:28px;border-radius:50%;background:#1e1e2e;border:2px solid #333;
    display:flex;align-items:center;justify-content:center;font-size:.75rem;color:#555;font-weight:700}
  .sdot.active{border-color:#a78bfa;color:#a78bfa}
  .sdot.done{background:#4ade80;border-color:#4ade80;color:#0f0f10}
  .sline{flex:1;height:2px;background:#333}
  .sline.done{background:#4ade80}
  nav{margin-bottom:2rem;font-size:.85rem;color:#555}
  nav a{color:#7dd3fc;text-decoration:none}
  .row{display:flex;gap:.75rem;margin-top:1rem}
  details summary{cursor:pointer;color:#94a3b8;font-size:.9rem;margin-bottom:.75rem}
  .checkgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:.4rem}
  .checkgrid label{display:flex;align-items:center;gap:.5rem;font-size:.9rem;
    background:#1e1e2e;padding:.4rem .6rem;border-radius:4px;cursor:pointer;color:#e0e0e0}
  .checkgrid input[type=checkbox]{width:auto;margin:0}
"""


def _json_response(data: object, status: int = 200) -> web.Response:
    return web.Response(
        status=status,
        content_type="application/json",
        text=json.dumps(data, default=str),
    )


def _html_response(html: str) -> web.Response:
    return web.Response(content_type="text/html", text=html)


def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · Cymphony</title>
<style>{_CSS}</style>
</head>
<body>
{body}
</body>
</html>"""


def build_app(orchestrator: "Orchestrator | None", workflow_path: Path) -> web.Application:
    """Build and return the aiohttp Application."""
    app = web.Application()
    app["orchestrator"] = orchestrator
    app["workflow_path"] = workflow_path

    # Dashboard / setup
    app.router.add_get("/", _handle_dashboard)
    app.router.add_get("/setup", _handle_setup)
    app.router.add_get("/settings", _handle_settings)

    # Orchestrator API
    app.router.add_get("/api/v1/state", _handle_state)
    app.router.add_post("/api/v1/refresh", _handle_refresh)

    # Config CRUD
    app.router.add_get("/api/v1/config", _handle_config_get)
    app.router.add_post("/api/v1/config", _handle_config_post)

    # Setup helper API (used by wizard + settings)
    app.router.add_post("/api/v1/setup/validate-key", _handle_setup_validate_key)
    app.router.add_get("/api/v1/setup/projects", _handle_setup_projects)
    app.router.add_get("/api/v1/setup/members", _handle_setup_members)
    app.router.add_get("/api/v1/setup/states", _handle_setup_states)

    # Per-issue debug (must be last to avoid shadowing setup routes)
    app.router.add_get("/api/v1/{identifier}", _handle_issue)

    return app


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

async def _handle_dashboard(request: web.Request) -> web.Response:
    """GET / — redirect to /setup when no orchestrator, otherwise show monitoring UI."""
    orch: Orchestrator | None = request.app["orchestrator"]
    if orch is None:
        raise web.HTTPFound("/setup")

    snap = orch.snapshot()
    running = snap.get("running", [])
    retrying = snap.get("retrying", [])
    totals = snap.get("codex_totals", {})

    running_rows = ""
    for r in running:
        session_id = r.get("session_id") or "—"
        running_rows += (
            f"<tr><td>{r.get('issue_identifier','')}</td>"
            f"<td>{r.get('state','')}</td>"
            f"<td>{r.get('turn_count','')}</td>"
            f"<td>{r.get('started_at','')}</td>"
            f"<td style='font-size:0.8em'>{session_id}</td></tr>\n"
        )

    retry_rows = ""
    for r in retrying:
        retry_rows += (
            f"<tr><td>{r.get('issue_identifier','')}</td>"
            f"<td>{r.get('attempt','')}</td>"
            f"<td>{r.get('due_in_seconds','')}s</td>"
            f"<td>{str(r.get('error',''))[:80]}</td></tr>\n"
        )

    body = f"""
<h1>Cymphony</h1>
<nav><a href="/settings">⚙ Settings</a> &nbsp;·&nbsp; <a href="/api/v1/state">JSON state</a></nav>

<h2>Token Totals</h2>
<div>
  <div class="stat">Input tokens<br><span>{totals.get('input_tokens', 0):,}</span></div>
  <div class="stat">Output tokens<br><span>{totals.get('output_tokens', 0):,}</span></div>
  <div class="stat">Total tokens<br><span>{totals.get('total_tokens', 0):,}</span></div>
  <div class="stat">Running time<br><span>{totals.get('seconds_running', 0):.0f}s</span></div>
</div>

<h2>Running ({len(running)})</h2>
{"<p class='empty'>No active agents.</p>" if not running else f'''
<table>
<thead><tr><th>Issue</th><th>State</th><th>Turns</th><th>Started</th><th>Session ID</th></tr></thead>
<tbody>{running_rows}</tbody>
</table>'''}

<h2>Retry Queue ({len(retrying)})</h2>
{"<p class='empty'>No retries scheduled.</p>" if not retrying else f'''
<table>
<thead><tr><th>Issue</th><th>Attempt</th><th>Due In</th><th>Error</th></tr></thead>
<tbody>{retry_rows}</tbody>
</table>'''}

<p style="color:#555;font-size:.8rem">
  <a href="javascript:location.reload()">refresh</a>
</p>
"""
    return _html_response(_page("Dashboard", body))


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------

_SETUP_JS = """
const API = {
  validateKey: (key) => fetch('/api/v1/setup/validate-key', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({api_key: key})
  }).then(r => r.json()),
  projects: (key) => fetch(`/api/v1/setup/projects?api_key=${encodeURIComponent(key)}`).then(r => r.json()),
  members: (key) => fetch(`/api/v1/setup/members?api_key=${encodeURIComponent(key)}`).then(r => r.json()),
  states: (key, pid) => fetch(`/api/v1/setup/states?api_key=${encodeURIComponent(key)}&project_id=${encodeURIComponent(pid)}`).then(r => r.json()),
  saveConfig: (body) => fetch('/api/v1/config', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(body)
  }).then(r => r.json()),
};

let currentStep = 0;
let validatedKey = '';
let selectedProject = null;

const steps = () => document.querySelectorAll('.step');
const dots = () => document.querySelectorAll('.sdot');
const lines = () => document.querySelectorAll('.sline');

function showStep(n) {
  steps().forEach((s, i) => s.classList.toggle('active', i === n));
  dots().forEach((d, i) => {
    d.classList.toggle('active', i === n);
    d.classList.toggle('done', i < n);
  });
  lines().forEach((l, i) => l.classList.toggle('done', i < n));
  currentStep = n;
}

function setMsg(id, msg, isErr) {
  const el = document.getElementById(id);
  el.textContent = msg;
  el.className = isErr ? 'err' : 'ok';
}

async function validateKey() {
  const key = document.getElementById('api_key').value.trim();
  if (!key) return setMsg('key_msg', 'Enter your Linear API key.', true);
  setMsg('key_msg', 'Validating…', false);
  document.getElementById('btn_validate').disabled = true;
  const res = await API.validateKey(key).catch(() => ({ok: false}));
  document.getElementById('btn_validate').disabled = false;
  if (res.ok) {
    validatedKey = key;
    setMsg('key_msg', '✓ Key is valid', false);
    await loadProjects();
    showStep(1);
  } else {
    setMsg('key_msg', '✗ Invalid key — check and try again.', true);
  }
}

async function loadProjects() {
  const sel = document.getElementById('project_sel');
  sel.innerHTML = '<option value="">Loading…</option>';
  const projects = await API.projects(validatedKey).catch(() => []);
  sel.innerHTML = '<option value="">— select a project —</option>';
  projects.forEach(p => {
    const o = document.createElement('option');
    o.value = JSON.stringify(p);
    o.textContent = p.name;
    sel.appendChild(o);
  });
}

async function onProjectSelect() {
  const sel = document.getElementById('project_sel');
  if (!sel.value) { selectedProject = null; return; }
  selectedProject = JSON.parse(sel.value);
  document.getElementById('btn_next1').disabled = false;
}

async function goStep2() {
  if (!selectedProject) return;
  const [members, states] = await Promise.all([
    API.members(validatedKey).catch(() => []),
    API.states(validatedKey, selectedProject.id).catch(() => []),
  ]);
  // Members
  const mSel = document.getElementById('assignee_sel');
  mSel.innerHTML = '<option value="">All issues (no filter)</option>';
  members.forEach(m => {
    const o = document.createElement('option');
    o.value = m.name;
    o.textContent = `${m.name} (${m.email})`;
    mSel.appendChild(o);
  });
  // States
  const activeGrid = document.getElementById('active_states');
  const termGrid = document.getElementById('terminal_states');
  const DEFAULT_ACTIVE = ['Todo', 'In Progress'];
  const DEFAULT_TERM = ['Done', 'Cancelled', 'Canceled', 'Duplicate', 'Closed'];
  [activeGrid, termGrid].forEach(g => g.innerHTML = '');
  states.forEach(s => {
    const mkCb = (grid, checked) => {
      const lbl = document.createElement('label');
      const cb = document.createElement('input');
      cb.type = 'checkbox'; cb.value = s.name; cb.checked = checked;
      lbl.appendChild(cb);
      lbl.appendChild(document.createTextNode(' ' + s.name));
      grid.appendChild(lbl);
    };
    mkCb(activeGrid, DEFAULT_ACTIVE.includes(s.name));
    mkCb(termGrid, DEFAULT_TERM.includes(s.name));
  });
  showStep(2);
}

function checkedValues(gridId) {
  return [...document.querySelectorAll(`#${gridId} input[type=checkbox]:checked`)].map(cb => cb.value);
}

async function save() {
  const btn = document.getElementById('btn_save');
  btn.disabled = true; btn.textContent = 'Saving…';

  const workspaceRoot = document.getElementById('workspace_root').value.trim() || '~/cymphony-workspaces';
  const assignee = document.getElementById('assignee_sel').value;
  const pollMs = parseInt(document.getElementById('poll_ms').value) || 30000;
  const maxAgents = parseInt(document.getElementById('max_agents').value) || 10;
  const maxTurns = parseInt(document.getElementById('max_turns').value) || 20;

  const payload = {
    tracker: {
      kind: 'linear',
      api_key: validatedKey,
      project_slug: selectedProject.slugId,
      assignee: assignee || null,
      active_states: checkedValues('active_states'),
      terminal_states: checkedValues('terminal_states'),
    },
    workspace: { root: workspaceRoot },
    polling: { interval_ms: pollMs },
    agent: { max_concurrent_agents: maxAgents, max_turns: maxTurns },
  };
  if (!payload.tracker.assignee) delete payload.tracker.assignee;

  const res = await API.saveConfig(payload).catch(e => ({ok: false, error: String(e)}));
  btn.disabled = false; btn.textContent = 'Save & Start';
  if (res.ok) {
    document.getElementById('save_msg').textContent = '✓ Config saved. Redirecting…';
    document.getElementById('save_msg').className = 'ok';
    setTimeout(() => location.href = '/', 1500);
  } else {
    document.getElementById('save_msg').textContent = '✗ ' + (res.error || 'Save failed');
    document.getElementById('save_msg').className = 'err';
  }
}
"""


async def _handle_setup(request: web.Request) -> web.Response:
    """GET /setup — multi-step setup wizard."""
    body = f"""
<h1>Set up Cymphony</h1>
<p style="color:#94a3b8">Connect your Linear account and configure the orchestrator.</p>

<div class="stepper">
  <div class="sdot active" id="d0">1</div>
  <div class="sline" id="l0"></div>
  <div class="sdot" id="d1">2</div>
  <div class="sline" id="l1"></div>
  <div class="sdot" id="d2">3</div>
  <div class="sline" id="l2"></div>
  <div class="sdot" id="d3">4</div>
</div>

<!-- Step 0: API Key -->
<div class="step active" id="step0">
  <h2>Linear API Key</h2>
  <div class="field">
    <label for="api_key">Personal API key — generate at <a href="https://linear.app/settings/api" target="_blank">linear.app/settings/api</a></label>
    <input id="api_key" type="password" placeholder="lin_api_…" autocomplete="off">
    <div id="key_msg" class="ok"></div>
  </div>
  <button class="btn" id="btn_validate" onclick="validateKey()">Validate &amp; Continue →</button>
</div>

<!-- Step 1: Project -->
<div class="step" id="step1">
  <h2>Select Project</h2>
  <div class="field">
    <label for="project_sel">Linear project to monitor</label>
    <select id="project_sel" onchange="onProjectSelect()">
      <option value="">Loading…</option>
    </select>
  </div>
  <div class="row">
    <button class="btn btn-ghost" onclick="showStep(0)">← Back</button>
    <button class="btn" id="btn_next1" onclick="goStep2()" disabled>Next →</button>
  </div>
</div>

<!-- Step 2: Assignee + States -->
<div class="step" id="step2">
  <h2>Assignee &amp; States</h2>
  <div class="field">
    <label for="assignee_sel">Assignee filter (optional)</label>
    <select id="assignee_sel">
      <option value="">All issues (no filter)</option>
    </select>
  </div>
  <div class="field">
    <label>Active states <span style="color:#555">(orchestrator picks up issues in these states)</span></label>
    <div class="checkgrid" id="active_states"></div>
  </div>
  <div class="field">
    <label>Terminal states <span style="color:#555">(orchestrator stops watching issues in these states)</span></label>
    <div class="checkgrid" id="terminal_states"></div>
  </div>
  <div class="row">
    <button class="btn btn-ghost" onclick="showStep(1)">← Back</button>
    <button class="btn" onclick="showStep(3)">Next →</button>
  </div>
</div>

<!-- Step 3: Workspace + Advanced + Save -->
<div class="step" id="step3">
  <h2>Workspace &amp; Options</h2>
  <div class="field">
    <label for="workspace_root">Workspace root directory</label>
    <input id="workspace_root" type="text" placeholder="~/cymphony-workspaces">
  </div>
  <details>
    <summary>Advanced options</summary>
    <div class="field">
      <label for="poll_ms">Poll interval (ms)</label>
      <input id="poll_ms" type="number" value="30000" min="5000">
    </div>
    <div class="field">
      <label for="max_agents">Max concurrent agents</label>
      <input id="max_agents" type="number" value="10" min="1">
    </div>
    <div class="field">
      <label for="max_turns">Max turns per issue</label>
      <input id="max_turns" type="number" value="20" min="1">
    </div>
  </details>
  <div class="row">
    <button class="btn btn-ghost" onclick="showStep(2)">← Back</button>
    <button class="btn" id="btn_save" onclick="save()">Save &amp; Start</button>
  </div>
  <div id="save_msg" style="margin-top:.75rem"></div>
</div>

<script>{_SETUP_JS}</script>
"""
    return _html_response(_page("Setup", body))


# ---------------------------------------------------------------------------
# Settings (always accessible)
# ---------------------------------------------------------------------------

_SETTINGS_JS = """
const _API = {
  validateKey: (key) => fetch('/api/v1/setup/validate-key', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({api_key: key})
  }).then(r=>r.json()),
  projects: (key) => fetch(`/api/v1/setup/projects?api_key=${encodeURIComponent(key)}`).then(r=>r.json()),
  members: (key) => fetch(`/api/v1/setup/members?api_key=${encodeURIComponent(key)}`).then(r=>r.json()),
  states: (key, pid) => fetch(`/api/v1/setup/states?api_key=${encodeURIComponent(key)}&project_id=${encodeURIComponent(pid)}`).then(r=>r.json()),
  save: (body) => fetch('/api/v1/config', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)
  }).then(r=>r.json()),
};
let _validatedKey = '';

async function sValidateKey() {
  const key = document.getElementById('s_api_key').value.trim();
  if (!key) return;
  setMsg('s_key_msg', 'Validating…', false);
  const res = await _API.validateKey(key).catch(()=>({ok:false}));
  if (res.ok) {
    _validatedKey = key;
    setMsg('s_key_msg', '✓ Valid', false);
    sLoadProjects();
    sLoadMembers();
  } else {
    setMsg('s_key_msg', '✗ Invalid key', true);
  }
}

async function sLoadProjects() {
  if (!_validatedKey) return;
  const projects = await _API.projects(_validatedKey).catch(()=>[]);
  const sel = document.getElementById('s_project');
  const cur = sel.value;
  sel.innerHTML = '<option value="">— manual entry below —</option>';
  projects.forEach(p => {
    const o = document.createElement('option');
    o.value = p.slugId;
    o.textContent = p.name + ' (' + p.slugId + ')';
    sel.appendChild(o);
  });
  if (cur) sel.value = cur;
  sel.onchange = () => {
    if (sel.value) document.getElementById('s_project_slug').value = sel.value;
    if (sel.value && _validatedKey) sLoadStates(sel.value);
  };
}

async function sLoadMembers() {
  if (!_validatedKey) return;
  const members = await _API.members(_validatedKey).catch(()=>[]);
  const sel = document.getElementById('s_assignee_sel');
  const cur = sel.value;
  sel.innerHTML = '<option value="">All issues (no filter)</option>';
  members.forEach(m => {
    const o = document.createElement('option');
    o.value = m.name;
    o.textContent = m.name + ' (' + m.email + ')';
    sel.appendChild(o);
  });
  if (cur) sel.value = cur;
  sel.onchange = () => document.getElementById('s_assignee').value = sel.value;
}

async function sLoadStates(slugId) {
  if (!_validatedKey || !slugId) return;
  const projects = await _API.projects(_validatedKey).catch(()=>[]);
  const proj = projects.find(p=>p.slugId===slugId);
  if (!proj) return;
  const states = await _API.states(_validatedKey, proj.id).catch(()=>[]);
  ['s_active_states','s_terminal_states'].forEach(gridId => {
    const grid = document.getElementById(gridId);
    if (grid.querySelector('input')) return; // already populated
    const curVals = gridId==='s_active_states'
      ? document.getElementById('s_active_val').value.split(',').map(s=>s.trim())
      : document.getElementById('s_terminal_val').value.split(',').map(s=>s.trim());
    grid.innerHTML = '';
    states.forEach(s => {
      const lbl = document.createElement('label');
      const cb = document.createElement('input');
      cb.type='checkbox'; cb.value=s.name; cb.checked=curVals.includes(s.name);
      lbl.appendChild(cb);
      lbl.appendChild(document.createTextNode(' '+s.name));
      grid.appendChild(lbl);
    });
  });
}

function sCheckedValues(gridId) {
  const cbs = document.querySelectorAll(`#${gridId} input[type=checkbox]:checked`);
  if (!cbs.length) return null;
  return [...cbs].map(cb=>cb.value);
}

async function ssSave() {
  const btn = document.getElementById('s_save_btn');
  btn.disabled=true; btn.textContent='Saving…';

  const apiKey = document.getElementById('s_api_key').value.trim();
  const slugId = document.getElementById('s_project_slug').value.trim();
  const assignee = document.getElementById('s_assignee').value.trim();
  const workspaceRoot = document.getElementById('s_workspace').value.trim();
  const pollMs = parseInt(document.getElementById('s_poll_ms').value)||30000;
  const maxAgents = parseInt(document.getElementById('s_max_agents').value)||10;
  const maxTurns = parseInt(document.getElementById('s_max_turns').value)||20;

  const activeStates = sCheckedValues('s_active_states')
    || document.getElementById('s_active_val').value.split(',').map(s=>s.trim()).filter(Boolean);
  const termStates = sCheckedValues('s_terminal_states')
    || document.getElementById('s_terminal_val').value.split(',').map(s=>s.trim()).filter(Boolean);

  const payload = {
    tracker: {
      kind: 'linear',
      api_key: apiKey || undefined,
      project_slug: slugId || undefined,
      assignee: assignee || null,
      active_states: activeStates,
      terminal_states: termStates,
    },
    workspace: workspaceRoot ? { root: workspaceRoot } : undefined,
    polling: { interval_ms: pollMs },
    agent: { max_concurrent_agents: maxAgents, max_turns: maxTurns },
  };
  if (!payload.tracker.assignee) delete payload.tracker.assignee;
  if (!payload.tracker.api_key) delete payload.tracker.api_key;
  if (!payload.tracker.project_slug) delete payload.tracker.project_slug;
  if (!payload.workspace) delete payload.workspace;

  const res = await _API.save(payload).catch(e=>({ok:false,error:String(e)}));
  btn.disabled=false; btn.textContent='Save';
  if (res.ok) {
    setMsg('s_msg','✓ Config saved — reload Cymphony to apply.',false);
  } else {
    setMsg('s_msg','✗ '+(res.error||'Save failed'),true);
  }
}

function setMsg(id,msg,isErr){
  const el=document.getElementById(id);
  el.textContent=msg; el.className=isErr?'err':'ok';
}
"""


async def _handle_settings(request: web.Request) -> web.Response:
    """GET /settings — settings editor (always accessible)."""
    wp: Path = request.app["workflow_path"]
    from .workflow import load_workflow
    from .models import WorkflowError
    try:
        wf = load_workflow(wp)
        raw = wf.config
    except WorkflowError:
        raw = {}

    tracker = raw.get("tracker") or {}
    workspace = raw.get("workspace") or {}
    polling = raw.get("polling") or {}
    agent = raw.get("agent") or {}

    api_key = tracker.get("api_key", "$LINEAR_API_KEY")
    project_slug = tracker.get("project_slug", "")
    assignee = tracker.get("assignee", "") or ""
    active_states = tracker.get("active_states") or ["Todo", "In Progress"]
    terminal_states = tracker.get("terminal_states") or ["Done", "Cancelled", "Canceled", "Duplicate", "Closed"]
    workspace_root = workspace.get("root", "")
    poll_ms = polling.get("interval_ms", 30000)
    max_agents = agent.get("max_concurrent_agents", 10)
    max_turns = agent.get("max_turns", 20)

    active_csv = ", ".join(active_states)
    terminal_csv = ", ".join(terminal_states)

    body = f"""
<h1>Settings</h1>
<nav><a href="/">← Dashboard</a></nav>

<h2>Linear</h2>
<div class="field">
  <label for="s_api_key">API key (or <code>$LINEAR_API_KEY</code>)</label>
  <input id="s_api_key" type="password" value="{api_key}" autocomplete="off">
  <div class="row" style="margin-top:.5rem">
    <button class="btn btn-ghost" style="padding:.35rem .9rem;font-size:.85rem" onclick="sValidateKey()">Validate &amp; load dropdowns</button>
    <span id="s_key_msg" style="align-self:center"></span>
  </div>
</div>

<div class="field">
  <label for="s_project">Project (browse after validating key)</label>
  <select id="s_project"><option value="">— validate key to browse —</option></select>
  <label for="s_project_slug" style="margin-top:.5rem">Or enter project slug manually</label>
  <input id="s_project_slug" type="text" value="{project_slug}" placeholder="my-project-abc123">
</div>

<div class="field">
  <label for="s_assignee_sel">Assignee filter (browse after validating key)</label>
  <select id="s_assignee_sel"><option value="">All issues (no filter)</option></select>
  <label for="s_assignee" style="margin-top:.5rem">Or enter username manually</label>
  <input id="s_assignee" type="text" value="{assignee}" placeholder="username">
</div>

<div class="field">
  <label>Active states <span style="color:#555">(validate key + select project to get checkboxes)</span></label>
  <div class="checkgrid" id="s_active_states"></div>
  <input type="hidden" id="s_active_val" value="{active_csv}">
  <input id="s_active_text" type="text" value="{active_csv}" placeholder="Todo, In Progress"
    style="margin-top:.5rem" oninput="document.getElementById('s_active_val').value=this.value">
</div>

<div class="field">
  <label>Terminal states</label>
  <div class="checkgrid" id="s_terminal_states"></div>
  <input type="hidden" id="s_terminal_val" value="{terminal_csv}">
  <input id="s_terminal_text" type="text" value="{terminal_csv}" placeholder="Done, Cancelled, Closed"
    style="margin-top:.5rem" oninput="document.getElementById('s_terminal_val').value=this.value">
</div>

<h2>Workspace</h2>
<div class="field">
  <label for="s_workspace">Root directory</label>
  <input id="s_workspace" type="text" value="{workspace_root}" placeholder="~/cymphony-workspaces">
</div>

<h2>Advanced</h2>
<div class="field">
  <label for="s_poll_ms">Poll interval (ms)</label>
  <input id="s_poll_ms" type="number" value="{poll_ms}" min="5000">
</div>
<div class="field">
  <label for="s_max_agents">Max concurrent agents</label>
  <input id="s_max_agents" type="number" value="{max_agents}" min="1">
</div>
<div class="field">
  <label for="s_max_turns">Max turns per issue</label>
  <input id="s_max_turns" type="number" value="{max_turns}" min="1">
</div>

<div class="row">
  <button class="btn" id="s_save_btn" onclick="ssSave()">Save</button>
  <a href="/" class="btn btn-ghost" style="text-decoration:none;display:inline-flex;align-items:center">Cancel</a>
</div>
<div id="s_msg" style="margin-top:.75rem"></div>

<script>{_SETTINGS_JS}</script>
"""
    return _html_response(_page("Settings", body))


# ---------------------------------------------------------------------------
# Orchestrator API
# ---------------------------------------------------------------------------

async def _handle_state(request: web.Request) -> web.Response:
    orch: Orchestrator | None = request.app["orchestrator"]
    if orch is None:
        return _json_response({"error": "orchestrator not running"}, status=503)
    return _json_response(orch.snapshot())


async def _handle_refresh(request: web.Request) -> web.Response:
    orch: Orchestrator | None = request.app["orchestrator"]
    if orch is None:
        return _json_response({"error": "orchestrator not running"}, status=503)
    coalesced = orch.request_immediate_poll()
    return _json_response({
        "queued": True,
        "coalesced": coalesced,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "operations": ["reconcile", "dispatch"],
    }, status=202)


async def _handle_issue(request: web.Request) -> web.Response:
    identifier = request.match_info["identifier"].upper()
    orch: Orchestrator | None = request.app["orchestrator"]
    if orch is None:
        return _json_response({"error": "orchestrator not running"}, status=503)
    snap = orch.snapshot()
    issue_data: dict | None = None
    for entry in snap.get("running", []):
        if entry.get("issue_identifier") == identifier:
            issue_data = {"tracked": True, "status": "running", **entry}
            break
    if issue_data is None:
        for entry in snap.get("retrying", []):
            if entry.get("issue_identifier") == identifier:
                issue_data = {
                    "tracked": True,
                    "status": "retry_scheduled",
                    "last_error": entry.get("error"),
                    **entry,
                }
                break
    if issue_data is None:
        return _json_response(
            {"error": {"code": "issue_not_found", "message": f"Issue {identifier} is not tracked"}},
            status=404,
        )
    return _json_response(issue_data)


# ---------------------------------------------------------------------------
# Config CRUD
# ---------------------------------------------------------------------------

async def _handle_config_get(request: web.Request) -> web.Response:
    """GET /api/v1/config — return current WORKFLOW.md config as JSON."""
    wp: Path = request.app["workflow_path"]
    from .workflow import load_workflow
    from .models import WorkflowError
    try:
        wf = load_workflow(wp)
        return _json_response({"ok": True, "config": wf.config})
    except WorkflowError as exc:
        return _json_response({"ok": False, "error": str(exc)})


async def _handle_config_post(request: web.Request) -> web.Response:
    """POST /api/v1/config — merge body into WORKFLOW.md frontmatter."""
    wp: Path = request.app["workflow_path"]
    try:
        updates: dict = await request.json()
    except Exception:
        return _json_response({"ok": False, "error": "invalid JSON body"}, status=400)

    from .workflow import write_frontmatter
    try:
        write_frontmatter(wp, updates)
        logger.info(f"action=config_saved path={wp}")
        return _json_response({"ok": True})
    except Exception as exc:
        logger.error(f"action=config_save_failed error={exc}")
        return _json_response({"ok": False, "error": str(exc)}, status=500)


# ---------------------------------------------------------------------------
# Setup helper API
# ---------------------------------------------------------------------------

async def _handle_setup_validate_key(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        api_key = body.get("api_key", "").strip()
    except Exception:
        return _json_response({"ok": False, "error": "invalid JSON"}, status=400)
    if not api_key:
        return _json_response({"ok": False, "error": "api_key required"}, status=400)

    # Resolve env var references
    if api_key.startswith("$"):
        import os
        api_key = os.environ.get(api_key[1:], "")

    from .linear import validate_api_key
    valid = await validate_api_key(api_key)
    return _json_response({"ok": valid})


async def _handle_setup_projects(request: web.Request) -> web.Response:
    api_key = _resolve_key(request.rel_url.query.get("api_key", ""))
    if not api_key:
        return _json_response({"error": "api_key required"}, status=400)
    from .linear import list_projects
    try:
        return _json_response(await list_projects(api_key))
    except Exception as exc:
        return _json_response({"error": str(exc)}, status=502)


async def _handle_setup_members(request: web.Request) -> web.Response:
    api_key = _resolve_key(request.rel_url.query.get("api_key", ""))
    if not api_key:
        return _json_response({"error": "api_key required"}, status=400)
    from .linear import list_members
    try:
        return _json_response(await list_members(api_key))
    except Exception as exc:
        return _json_response({"error": str(exc)}, status=502)


async def _handle_setup_states(request: web.Request) -> web.Response:
    api_key = _resolve_key(request.rel_url.query.get("api_key", ""))
    project_id = request.rel_url.query.get("project_id", "").strip()
    if not api_key or not project_id:
        return _json_response({"error": "api_key and project_id required"}, status=400)
    from .linear import list_team_states
    try:
        return _json_response(await list_team_states(api_key, project_id))
    except Exception as exc:
        return _json_response({"error": str(exc)}, status=502)


def _resolve_key(key: str) -> str:
    """Expand $VAR references in an API key string."""
    key = key.strip()
    if key.startswith("$"):
        import os
        return os.environ.get(key[1:], "")
    return key


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def start_server(
    orchestrator: "Orchestrator | None",
    workflow_path: Path,
    port: int,
) -> web.AppRunner:
    """Start the HTTP server and return the runner (caller must keep reference)."""
    app = build_app(orchestrator, workflow_path)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    logger.info(f"action=http_server_started host=127.0.0.1 port={port}")
    return runner
