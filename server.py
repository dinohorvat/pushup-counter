"""
Pushup tracker API + dashboard.
Run:  uvicorn server:app --host 0.0.0.0 --port 8005
"""

import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

import json

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

DB_PATH = Path(__file__).parent / "pushups.db"


# ── Database ────────────────────────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            reps       INTEGER NOT NULL,
            duration   REAL    NOT NULL,
            start_time REAL    NOT NULL,
            end_time   REAL    NOT NULL,
            avg_pace   REAL    NOT NULL,
            created_at TEXT    DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(lifespan=lifespan)

# ── WebSocket management ────────────────────────────────────────────────────
ws_clients: set[WebSocket] = set()


async def broadcast(data: dict):
    """Send a JSON message to all connected WebSocket clients."""
    msg = json.dumps(data)
    dead = []
    for ws in ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ws_clients.discard(ws)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.add(ws)
    try:
        while True:
            await ws.receive_text()  # keep connection alive
    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.discard(ws)


@app.post("/api/live")
async def live_event(event: dict):
    """Receive live events from the detector and broadcast to dashboards."""
    await broadcast(event)
    return {"status": "ok"}


# ── Models ──────────────────────────────────────────────────────────────────
class SessionIn(BaseModel):
    reps: int
    duration: float
    start_time: float
    end_time: float
    avg_pace: float


# ── API ─────────────────────────────────────────────────────────────────────
@app.post("/api/sessions")
async def create_session(s: SessionIn):
    conn = get_db()
    conn.execute(
        "INSERT INTO sessions (reps, duration, start_time, end_time, avg_pace) "
        "VALUES (?, ?, ?, ?, ?)",
        (s.reps, s.duration, s.start_time, s.end_time, s.avg_pace),
    )
    conn.commit()
    conn.close()
    await broadcast({"type": "session_saved", "reps": s.reps})
    return {"status": "ok"}


@app.get("/api/sessions")
def list_sessions(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM sessions ORDER BY start_time DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    conn.close()
    return {"sessions": [dict(r) for r in rows], "total": total}


class SessionUpdate(BaseModel):
    reps: int


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: int):
    conn = get_db()
    cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()
    deleted = cur.rowcount
    conn.close()
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    await broadcast({"type": "session_deleted", "id": session_id})
    return {"status": "ok"}


@app.patch("/api/sessions/{session_id}")
async def update_session(session_id: int, body: SessionUpdate):
    conn = get_db()
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Session not found")
    # Recalculate avg_pace based on new reps
    duration = row["duration"]
    new_pace = round(duration / body.reps, 2) if body.reps > 0 else 0
    conn.execute(
        "UPDATE sessions SET reps = ?, avg_pace = ? WHERE id = ?",
        (body.reps, new_pace, session_id),
    )
    conn.commit()
    conn.close()
    await broadcast({"type": "session_updated", "id": session_id, "reps": body.reps})
    return {"status": "ok"}


@app.get("/api/stats")
def stats():
    conn = get_db()
    c = conn.cursor()

    total_reps = c.execute("SELECT COALESCE(SUM(reps),0) FROM sessions").fetchone()[0]
    total_sessions = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    best_session = c.execute("SELECT COALESCE(MAX(reps),0) FROM sessions").fetchone()[0]
    avg_reps = c.execute("SELECT COALESCE(AVG(reps),0) FROM sessions").fetchone()[0]

    now = time.time()
    week_ago = now - 7 * 86400
    month_ago = now - 30 * 86400

    reps_week = c.execute(
        "SELECT COALESCE(SUM(reps),0) FROM sessions WHERE start_time >= ?",
        (week_ago,),
    ).fetchone()[0]

    reps_month = c.execute(
        "SELECT COALESCE(SUM(reps),0) FROM sessions WHERE start_time >= ?",
        (month_ago,),
    ).fetchone()[0]

    # Streak: consecutive days with at least one session (going backwards from today)
    rows = c.execute(
        "SELECT DISTINCT date(start_time, 'unixepoch', 'localtime') AS d "
        "FROM sessions ORDER BY d DESC"
    ).fetchall()
    conn.close()

    streak = 0
    if rows:
        from datetime import date, timedelta

        today = date.today()
        expected = today
        for row in rows:
            d = date.fromisoformat(row["d"])
            if d == expected:
                streak += 1
                expected -= timedelta(days=1)
            elif d < expected:
                break

    return {
        "total_reps": total_reps,
        "total_sessions": total_sessions,
        "best_session": best_session,
        "avg_reps": round(avg_reps, 1),
        "current_streak": streak,
        "reps_week": reps_week,
        "reps_month": reps_month,
    }


# ── Dashboard ───────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD_HTML


DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pushup Tracker</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
  :root{--bg:#0f1117;--card:#1a1d27;--border:#2a2d3a;--text:#e4e4e7;
        --dim:#71717a;--accent:#6366f1;--accent2:#818cf8;--green:#22c55e}
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:var(--bg);color:var(--text);padding:20px;max-width:960px;margin:0 auto}
  h1{font-size:1.5rem;margin-bottom:24px;font-weight:600;letter-spacing:-.02em}
  h1 span{color:var(--accent2)}

  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
         gap:12px;margin-bottom:28px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:12px;
        padding:16px}
  .card .label{font-size:.75rem;color:var(--dim);text-transform:uppercase;
               letter-spacing:.05em;margin-bottom:4px}
  .card .value{font-size:1.75rem;font-weight:700}
  .card .sub{font-size:.8rem;color:var(--dim);margin-top:2px}

  .chart-wrap{background:var(--card);border:1px solid var(--border);border-radius:12px;
              padding:20px;margin-bottom:28px}
  .chart-wrap h2{font-size:.9rem;color:var(--dim);margin-bottom:12px;font-weight:500}

  .sessions h2{font-size:.9rem;color:var(--dim);margin-bottom:12px;font-weight:500;
                text-transform:uppercase;letter-spacing:.05em}
  .session{display:flex;justify-content:space-between;align-items:center;
           padding:12px 16px;background:var(--card);border:1px solid var(--border);
           border-radius:10px;margin-bottom:8px}
  .session .left{display:flex;flex-direction:column;gap:2px}
  .session .date{font-size:.85rem;font-weight:500}
  .session .meta{font-size:.75rem;color:var(--dim)}
  .session .right{display:flex;align-items:center;gap:12px}
  .session .reps{font-size:1.25rem;font-weight:700;color:var(--accent2)}
  .session .actions{display:flex;gap:4px;opacity:0;transition:opacity .15s}
  .session:hover .actions{opacity:1}
  .action-btn{background:none;border:1px solid var(--border);border-radius:6px;
    color:var(--dim);cursor:pointer;padding:4px 8px;font-size:.75rem;transition:all .15s}
  .action-btn:hover{border-color:var(--accent);color:var(--accent2)}
  .action-btn.del:hover{border-color:#ef4444;color:#ef4444}

  .modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);
    z-index:100;align-items:center;justify-content:center}
  .modal-overlay.active{display:flex}
  .modal{background:var(--card);border:1px solid var(--border);border-radius:14px;
    padding:24px;width:min(340px,90vw)}
  .modal h3{font-size:1rem;margin-bottom:16px;font-weight:600}
  .modal input{width:100%;padding:10px 12px;background:var(--bg);border:1px solid var(--border);
    border-radius:8px;color:var(--text);font-size:1rem;outline:none;margin-bottom:16px}
  .modal input:focus{border-color:var(--accent)}
  .modal-btns{display:flex;gap:8px;justify-content:flex-end}
  .modal-btns button{padding:8px 16px;border-radius:8px;border:none;cursor:pointer;
    font-size:.85rem;font-weight:500}
  .btn-cancel{background:var(--border);color:var(--text)}
  .btn-save{background:var(--accent);color:#fff}
  .btn-save:hover{background:var(--accent2)}

  .live-banner{display:none;align-items:center;gap:10px;padding:14px 18px;
    background:linear-gradient(135deg,#1e1b4b,#312e81);border:1px solid #4338ca;
    border-radius:12px;margin-bottom:20px;animation:fadeIn .3s ease}
  .live-banner.active{display:flex}
  .live-dot{width:10px;height:10px;background:#22c55e;border-radius:50%;
    animation:pulse 1.5s ease-in-out infinite}
  .live-banner .live-label{font-size:.8rem;color:#a5b4fc;text-transform:uppercase;
    letter-spacing:.06em;font-weight:600}
  .live-banner .live-reps{font-size:1.5rem;font-weight:700;margin-left:auto;color:#e0e7ff}
  @keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(.85)}}
  @keyframes fadeIn{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:translateY(0)}}

  .empty{text-align:center;color:var(--dim);padding:40px 0;font-size:.9rem}
  @media(max-width:480px){.cards{grid-template-columns:repeat(2,1fr)}
    body{padding:14px}}
</style>
</head>
<body>
<h1><span>&#9650;</span> Pushup Tracker</h1>
<div class="live-banner" id="liveBanner">
  <div class="live-dot"></div>
  <span class="live-label">Live Session</span>
  <span class="live-reps" id="liveReps">0 reps</span>
</div>
<div class="cards" id="cards"></div>
<div class="chart-wrap"><h2>Reps / Day — Last 30 Days</h2>
  <canvas id="chart" height="140"></canvas>
</div>
<div class="sessions">
  <h2>Set History</h2>
  <div id="list"></div>
</div>
<div class="modal-overlay" id="editModal">
  <div class="modal">
    <h3>Edit Set</h3>
    <input type="number" id="editReps" min="1" placeholder="Reps">
    <div class="modal-btns">
      <button class="btn-cancel" onclick="closeEdit()">Cancel</button>
      <button class="btn-save" onclick="saveEdit()">Save</button>
    </div>
  </div>
</div>
<script>
let chart;

function fmt(ts){
  const d=new Date(ts*1000);
  return d.toLocaleDateString('en-US',{month:'short',day:'numeric'})+
    ' '+d.toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit'});
}
function dur(s){
  if(s<60)return Math.round(s)+'s';
  return Math.floor(s/60)+'m '+Math.round(s%60)+'s';
}

async function load(){
  const [statsR,sessR]=await Promise.all([
    fetch('/api/stats').then(r=>r.json()),
    fetch('/api/sessions?limit=100').then(r=>r.json())
  ]);
  const s=statsR;

  document.getElementById('cards').innerHTML=`
    <div class="card"><div class="label">Total Reps</div>
      <div class="value">${s.total_reps.toLocaleString()}</div></div>
    <div class="card"><div class="label">Sets</div>
      <div class="value">${s.total_sessions}</div></div>
    <div class="card"><div class="label">Best Set</div>
      <div class="value">${s.best_session}</div><div class="sub">reps</div></div>
    <div class="card"><div class="label">Streak</div>
      <div class="value">${s.current_streak}</div><div class="sub">days</div></div>
    <div class="card"><div class="label">This Week</div>
      <div class="value">${s.reps_week}</div><div class="sub">reps</div></div>
    <div class="card"><div class="label">This Month</div>
      <div class="value">${s.reps_month}</div><div class="sub">reps</div></div>
  `;

  // Chart data — aggregate reps per day for last 30 days
  const days={};
  const now=Date.now();
  for(let i=29;i>=0;i--){
    const d=new Date(now-i*86400000);
    days[d.toISOString().slice(0,10)]=0;
  }
  for(const sess of sessR.sessions){
    const d=new Date(sess.start_time*1000).toISOString().slice(0,10);
    if(d in days) days[d]+=sess.reps;
  }
  const labels=Object.keys(days).map(d=>{
    const p=d.split('-');return p[1]+'/'+p[2];
  });
  const data=Object.values(days);

  if(chart) chart.destroy();
  const ctx=document.getElementById('chart').getContext('2d');
  chart=new Chart(ctx,{type:'bar',data:{labels,datasets:[{
    data,backgroundColor:'#6366f1',borderRadius:4,maxBarThickness:18
  }]},options:{responsive:true,plugins:{legend:{display:false}},
    scales:{x:{ticks:{color:'#71717a',font:{size:10}},grid:{display:false}},
            y:{ticks:{color:'#71717a'},grid:{color:'#2a2d3a'},beginAtZero:true}}}});

  // Session list
  const list=document.getElementById('list');
  if(!sessR.sessions.length){
    list.innerHTML='<div class="empty">No sets yet — go do some pushups!</div>';
    return;
  }
  list.innerHTML=sessR.sessions.map(s=>`
    <div class="session">
      <div class="left">
        <div class="date">${fmt(s.start_time)}</div>
        <div class="meta">${dur(s.duration)} &middot; ${s.avg_pace}s/rep</div>
      </div>
      <div class="right">
        <div class="actions">
          <button class="action-btn" onclick="openEdit(${s.id},${s.reps})">edit</button>
          <button class="action-btn del" onclick="delSet(${s.id})">del</button>
        </div>
        <div class="reps">${s.reps}</div>
      </div>
    </div>`).join('');
}

let editId=null;
function openEdit(id,reps){
  editId=id;
  document.getElementById('editReps').value=reps;
  document.getElementById('editModal').classList.add('active');
  document.getElementById('editReps').focus();
}
function closeEdit(){
  document.getElementById('editModal').classList.remove('active');
  editId=null;
}
async function saveEdit(){
  const reps=parseInt(document.getElementById('editReps').value);
  if(!reps||reps<1)return;
  await fetch('/api/sessions/'+editId,{method:'PATCH',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({reps})});
  closeEdit();
  load();
}
async function delSet(id){
  if(!confirm('Delete this set?'))return;
  await fetch('/api/sessions/'+id,{method:'DELETE'});
  load();
}
document.getElementById('editModal').addEventListener('click',function(e){
  if(e.target===this)closeEdit();
});
document.getElementById('editReps').addEventListener('keydown',function(e){
  if(e.key==='Enter')saveEdit();
  if(e.key==='Escape')closeEdit();
});

load();
setInterval(load,30000);

// ── WebSocket for live updates ──
function connectWS(){
  const proto=location.protocol==='https:'?'wss:':'ws:';
  const ws=new WebSocket(proto+'//'+location.host+'/ws');
  ws.onmessage=function(e){
    const ev=JSON.parse(e.data);
    const banner=document.getElementById('liveBanner');
    const repsEl=document.getElementById('liveReps');
    if(ev.type==='session_start'){
      banner.classList.add('active');
      repsEl.textContent='0 reps';
    }else if(ev.type==='rep'){
      banner.classList.add('active');
      repsEl.textContent=ev.reps+(ev.reps===1?' rep':' reps');
    }else if(ev.type==='session_end'||ev.type==='session_saved'){
      banner.classList.remove('active');
      load();
    }
  };
  ws.onclose=function(){setTimeout(connectWS,3000);};
  ws.onerror=function(){ws.close();};
}
connectWS();
</script>
</body>
</html>
"""
