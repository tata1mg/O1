#!/usr/bin/env python3
"""Tiny SSE server for the live activity feed + operator steering inbox.
GET /feed streams new lines of a run's activity.log; POST /steer queues a mid-run steering message
(raw text + optional lane) into logs/steer.inbox.jsonl, which the harness delivers to the RUNNING
agent as an authoritative user turn at its next tick (0.24.0). Binds loopback, but the bind is not
the boundary — steer_request_ok is (0.24.1). Deliberately dependency-free (bare-python runnable):
the steering record is raw — the harness applies the provenance template at delivery, so the
wording lives in exactly one place (rfc_agent/steer.py)."""
import json
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FEED = Path("activity.log")  # real values parsed under __main__ — import-safe for tests (0.24.1)

# The original terminal-log page, unchanged in form — the ONLY addition is color, so each line's
# nature (thinking / narration / denial / gate / tool) and its agent are identifiable at a glance.
PAGE = r"""<!doctype html><meta charset="utf-8"><title>rfc-agent live feed</title>
<meta name=viewport content="width=device-width, initial-scale=1">
<body style="margin:0;background:#111;color:#ddd;font:12px/1.6 ui-monospace,monospace;
min-height:100vh;min-height:100dvh;display:flex;flex-direction:column">
<style>
/* full-height column: the log area grows, so the composer sits at the VIEWPORT bottom even when
   the log is shorter than the window — bare position:sticky only pins once content overflows,
   which left the bar floating mid-screen over dead space (field screenshot, 0.24.14) */
#f{flex:1 0 auto}
.ln{white-space:pre-wrap;overflow-wrap:anywhere}
.ts{color:#666}
.think{color:#c4b5fd}                                   /* 💭 reasoning — violet */
.say{color:#f5f3ff;font-weight:600}                     /* 💬 narration — near-white, bold */
.deny{color:#fbbf24}                                    /* ⛔ denials — amber */
.pass{color:#4ade80;font-weight:600}                    /* gate PASSED */
.fail{color:#f87171;font-weight:600}                    /* gate FAILED */
.mile{color:#7dd3fc}                                    /* stage/agent lifecycle — sky */
.tool{color:#8a8a8a}                                    /* tool calls — quiet grey */
</style>
<div style="padding:8px 12px;position:sticky;top:0;background:#111;border-bottom:1px solid #333">
<b style="color:#7fd7a7">rfc-agent live feed</b> <span id=s aria-live=polite style="color:#888">connecting…</span></div>
<div id=f style="margin:0;padding:10px 12px"></div>
<style>
/* steering composer — the converged agent-composer anatomy (one rounded container, target
   segment left, borderless field, icon send inside) in the page's own terminal tokens; visible
   focus ring lives on the CONTAINER via :focus-within, >=38px targets, 200ms color transitions */
#inner{max-width:768px;margin:0 auto;width:100%}
#composer{display:flex;align-items:center;gap:2px;background:#15181c;border:1px solid #2e343b;
border-radius:12px;padding:5px 6px;transition:border-color .2s,box-shadow .2s}
#composer:focus-within{border-color:#7fd7a7;box-shadow:0 0 0 2px #7fd7a733}
#composer input{background:transparent;color:#ddd;border:0;font:inherit;min-height:34px;padding:0 10px}
#composer input:focus{outline:none}
#composer input::placeholder{color:#6b7280}
#composer input[aria-invalid]{box-shadow:0 0 0 2px #f8717155;border-radius:8px}
#ldiv{width:1px;align-self:stretch;margin:4px 0;background:#2e343b;flex-shrink:0}
#go{flex-shrink:0;width:34px;height:34px;border-radius:50%;border:0;cursor:pointer;
background:#12271c;color:#7fd7a7;position:relative;display:flex;align-items:center;
justify-content:center;transition:background .2s}
#go:hover{background:#1a3a29}
#go:focus-visible{outline:none;box-shadow:0 0 0 2px #7fd7a755}
#go:active{background:#0d1e15}
#go svg{stroke:#7fd7a7;transition:opacity .2s}
#go:disabled{cursor:wait}
#go:disabled svg{opacity:0}
#go:disabled::after{content:'';position:absolute;inset:0;margin:auto;width:14px;height:14px;
border:2px solid #7fd7a7;border-top-color:transparent;border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
#lsug{display:none;position:absolute;bottom:calc(100% + 8px);left:0;min-width:260px;max-height:280px;
overflow-y:auto;background:#14171b;border:1px solid #2e343b;border-radius:10px;
box-shadow:0 10px 30px rgba(0,0,0,.65);z-index:30}
#lsug .hint{padding:5px 11px;font-size:10px;color:#6b7280;border-bottom:1px solid #23272e;
user-select:none;letter-spacing:.03em}
#lsug .lrow{padding:8px 11px;cursor:pointer;display:flex;gap:10px;justify-content:space-between;
align-items:baseline;border-left:2px solid transparent;transition:background .15s}
#lsug .lrow.sel{background:#22272e;border-left-color:#7fd7a7}
#lsug .lrow .note{color:#8a8a8a;font-size:11px;white-space:nowrap}
#lsug .lrow .note.live{color:#4ade80}
#q .chip{padding:3px 4px 3px 11px;border-radius:12px;border:1px solid #4b5563;color:#9ca3af;font-size:11px;
max-width:380px;display:inline-flex;align-items:center;gap:2px;white-space:nowrap;
transition:opacity .6s,border-color .3s,color .3s}
#q .chip .txt{overflow:hidden;text-overflow:ellipsis;min-width:0}
#q .chip .x{flex-shrink:0;border:0;background:transparent;color:inherit;opacity:.55;cursor:pointer;
font:inherit;padding:3px 7px;border-radius:8px;transition:opacity .2s,background .2s,color .2s}
#q .chip .x:hover{opacity:1;background:#2a2f36;color:#f87171}
#q .chip .x:focus-visible{outline:none;box-shadow:0 0 0 2px #7fd7a755;opacity:1}
.act{flex-shrink:0;border:1px solid #3a4149;background:transparent;color:#7fd7a7;cursor:pointer;
font:inherit;font-size:11px;padding:2px 9px;border-radius:8px;transition:background .2s,border-color .2s}
.act:hover{background:#12271c;border-color:#7fd7a7}
.act:focus-visible{outline:none;box-shadow:0 0 0 2px #7fd7a755}
#q .chip::before{content:'●';margin-right:6px;color:#6b7280}
#q .chip.sent::before{animation:pulse 1.6s ease-in-out infinite}
#q .chip.delivered{border-color:#7a5b14;color:#fbbf24}
#q .chip.delivered::before{color:#fbbf24}
#q .chip.read{border-color:#2f5c44;color:#4ade80}
#q .chip.read::before{color:#4ade80}
#q .chip.skip{border-color:#6b7280;color:#d1d5db;border-style:dashed}
#q .chip.skip::before{content:'⊘';color:#9ca3af}
#q .chip.expired{border-color:#7f1d1d;color:#f87171;border-style:dashed}
#q .chip.expired::before{content:'⊗';color:#f87171}
@keyframes pulse{50%{opacity:.35}}
@media (prefers-reduced-motion:reduce){
  #q .chip.sent::before{animation:none}
  #sb button:disabled::after{animation:none;border-top-color:#7fd7a7}
}
</style>
<!-- ONE sticky footer owns chips + composer: no hardcoded bottom offsets to drift when either
     wraps or resizes (the 0.24.9 fixed-pixel bottoms overlapped each other and the dropdown);
     content centers at a readable width instead of stretching across ultrawide panes -->
<div id=foot style="position:sticky;bottom:0;background:#0b0b0b;border-top:1px solid #262b31;
padding:8px 12px 10px">
<div id=inner>
<div id=q style="display:none;padding:0 2px 6px;gap:6px;flex-wrap:wrap"></div>
<form id=sb onsubmit="return sendSteer(event)" style="margin:0">
<div id=composer>
<span style="position:relative;display:flex;flex:0 1 190px;min-width:104px;max-width:38%">
<!-- "to", not "lane" (0.24.15): lane is harness-internal routing jargon; the operator is
     ADDRESSING an agent. Values stay the feed's own colored labels; the CLI keeps --lane. -->
<input id=lane placeholder="to · all live agents" autocomplete=off spellcheck=false
 aria-label="to — which agent receives this (blank = every live agent)" aria-expanded=false
 title="address one agent (values match the feed's labels); prefixes fan out (ground reaches
every ground shard); a not-yet-started stage queues until it starts; kept after send for
repeat steers. CLI equivalent: --lane"
 style="width:100%;padding-right:24px">
<svg id=lchev viewBox="0 0 16 16" width=12 height=12 aria-hidden=true
 style="position:absolute;right:8px;top:50%;margin-top:-6px;pointer-events:none;
transition:transform .2s;fill:none;stroke:#6b7280;stroke-width:2;stroke-linecap:round">
<path d="M4 6l4 4 4-4"/></svg>
<div id=lsug role=listbox aria-label="agent suggestions"></div>
</span>
<span id=ldiv></span>
<input id=msg maxlength=4000 placeholder="steer the running agents (gates still apply)"
 aria-label="steering message" autocomplete=off style="flex:1;min-width:0">
<button id=go aria-label="send steering message" title="send (↵)">
<svg viewBox="0 0 16 16" width=14 height=14 aria-hidden=true
 style="fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round">
<path d="M8 13V3M4 7l4-4 4 4"/></svg>
</button>
</div>
</form>
</div>
</div>
<div id=j title="jump to latest" style="display:none;position:fixed;right:18px;bottom:64px;width:38px;height:38px;
line-height:38px;text-align:center;border-radius:50%;background:#1f2937;color:#7fd7a7;border:1px solid #374151;
font-size:20px;cursor:pointer;user-select:none;box-shadow:0 2px 8px rgba(0,0,0,.5)">↓</div>
<script>
const f=document.getElementById('f'),s=document.getElementById('s'),j=document.getElementById('j');
// stable per-agent colors: same label = same color, distinct labels diverge (measured need: up to
// 9 lanes in one run — parent + shards + personas)
const AGENT=['#38bdf8','#4ade80','#c084fc','#fbbf24','#f472b6','#2dd4bf','#a3e635','#fb923c','#e879f9'];
const seen=new Map();
const laneColor=l=>{if(!seen.has(l))seen.set(l,AGENT[seen.size%AGENT.length]);return seen.get(l)};
const cls=m=>
  /^⛔/.test(m)?'deny':
  /^gate PASSED/.test(m)?'pass':
  /^gate FAILED/.test(m)?'fail':
  /^💬/.test(m)?'say':
  /^💭/.test(m)?'think':
  /^(STAGE |agent |revision)/.test(m)?'mile':'tool';
const es=new EventSource('/feed');
es.onopen=()=>s.textContent='streaming';
es.onmessage=e=>{
  if(!e.data)return;
  const m=/^\[(\d\d:\d\d:\d\d)\] \[([^\]]+)\] ([\s\S]*)$/.exec(e.data);
  const d=document.createElement('div'); d.className='ln';
  if(m){
    const [,ts,label,msg]=m;
    trackLane(label,msg);
    let mm;
    if(mm=/^🧭 operator steering read by the agent \((\d+) chars\)/.exec(msg))chipRead(+mm[1]);
    else if(mm=/^🧭 operator steering delivered \((\d+) chars\)/.exec(msg))chipDelivered(+mm[1],label);
    else if(mm=/^🧭 steering skipped \(lane '(.*)' does not match this agent\)/.exec(msg))chipSkipped(mm[1],label);
    else if(mm=/^🧭 steering expired \(lane '(.*)'\) — never delivered this invocation/.exec(msg))chipExpired(mm[1]);
    const t=document.createElement('span'); t.className='ts'; t.textContent='['+ts+'] ';
    const w=document.createElement('span'); w.style.color=laneColor(label); w.textContent='['+label+'] ';
    const b=document.createElement('span'); b.className=cls(msg); b.textContent=msg;
    d.append(t,w,b);
  } else d.textContent=e.data;
  // stick-to-bottom only if the reader was already there — scrolling up to read must not be
  // yanked back down by every new event (measured before append, or the append itself unsticks)
  const stick=atBottom();
  f.appendChild(d);
  if(stick)window.scrollTo(0,document.body.scrollHeight);
  showJump();
};
const atBottom=()=>window.innerHeight+window.scrollY>=document.body.scrollHeight-40;
const showJump=()=>{j.style.display=(atBottom()||sugOpen)?'none':'block'};
window.addEventListener('scroll',showJump,{passive:true});
// the ↓ rides just above the footer whatever its CURRENT height — chips appearing, the bar
// wrapping, the pane expanding/collapsing all change it; ResizeObserver is the native primitive
// (the 0.24.9 hardcoded bottom:64px overlapped the chip strip and broke on every pane resize)
const foot=document.getElementById('foot');
new ResizeObserver(()=>{j.style.bottom=(foot.offsetHeight+14)+'px';showJump()}).observe(foot);
window.addEventListener('resize',showJump,{passive:true});
j.onclick=()=>window.scrollTo(0,document.body.scrollHeight);
es.onerror=()=>s.textContent='disconnected';
// lane autocomplete, IN-PAGE: the native <datalist> popup renders in screen coordinates and lands
// detached from the input inside embedded browser panes (observed: suggestions painted over the
// host app's sidebar) — so the dropdown is our own DOM, anchored above the lane box. The feed
// itself is the source of truth: every label that appears is a lane, "agent started/finished"
// lifecycle lines say which are LIVE, dotted labels yield prefix targets, unseen stages queue.
const lane=document.getElementById('lane'),lsug=document.getElementById('lsug'),
      lchev=document.getElementById('lchev');
const live=new Set(),lanesSeen=new Set(),STAGES=['ingest','extract','ground','generate','review'];
// EXPLICIT open flag — never sniff the inline display style: it is '' until first toggled
// (display:none lives in the stylesheet), so the not-equal-to-'none' check read as OPEN on a
// fresh page and the first feed line auto-opened the dropdown with zero user interaction
let sel=0,sugOpen=false;
// seed lifecycle state from the WHOLE log via /lanes: the SSE replays only the last ~4KB, so a
// fresh page believed long-finished stages (ingest/extract/ground) were "queued until stage
// starts" — the tail is a window, never the truth about lifecycle (field report, 0.24.15)
fetch('/lanes').then(r=>r.json()).then(d=>{
  (d.live||[]).forEach(l=>{lanesSeen.add(l);live.add(l)});
  (d.finished||[]).forEach(l=>lanesSeen.add(l));
}).catch(()=>{});
function trackLane(label,msg){
  // suggestion candidacy requires AGENT LIFECYCLE EVIDENCE, never mere appearance as a colored
  // label: the harness's own housekeeping labels ('run' on expiry/retract lines) were being
  // offered as addressable agents (field screenshot 2026-08-21) — the anchor-map lesson again,
  // harness-authored labels must not enter the addressing vocabulary by osmosis
  if(/^agent started/.test(msg)){lanesSeen.add(label);live.add(label)}
  else if(/^agent finished/.test(msg)){lanesSeen.add(label);live.delete(label)}
  if(sugOpen)renderSug();
}
function laneOptions(){
  // ranked, not flat (0.24.15): live agents are who you actually steer; prefixes fan a note out;
  // queued-future stages deliver later by design; FINISHED lanes are DORMANT — a steer to one
  // delivers only if that agent runs again (back-jump, retry, --force), so they sort last, say so,
  // and finished SHARDS collapse under their stage prefix instead of crowding the list.
  const rank={live:0,prefix:1,queued:2,finished:3};
  const o=new Map();  // value -> [note, kind]
  for(const l of live)o.set(l,['live','live']);
  for(const l of lanesSeen)if(l.includes('.')){const p=l.split('.')[0];
    if(!o.has(p))o.set(p,['all '+p+' agents','prefix'])}
  for(const l of lanesSeen)if(!l.includes('.')&&!o.has(l))
    o.set(l,['finished — delivers only if it re-runs','finished']);
  for(const st of STAGES)if(!o.has(st))o.set(st,['queued until stage starts','queued']);
  const q=lane.value.trim().toLowerCase();
  return [...o.entries()].map(([v,[note,kind]])=>[v,note,kind])
    .filter(([v])=>!q||v.toLowerCase().includes(q))
    .sort((a,b)=>rank[a[2]]-rank[b[2]]
      ||(q?b[0].toLowerCase().startsWith(q)-a[0].toLowerCase().startsWith(q):0))
    .slice(0,8);
}
function renderSug(){
  const opts=laneOptions();
  if(!opts.length)return hideSug();
  sel=Math.max(0,Math.min(sel,opts.length-1));
  const hint=document.createElement('div');hint.className='hint';
  hint.textContent='↑↓ move · ↵ pick · esc close';
  lsug.replaceChildren(hint,...opts.map(([v,note],i)=>{
    const r=document.createElement('div');
    r.className='lrow'+(i===sel?' sel':'');
    r.dataset.v=v;r.setAttribute('role','option');
    const n=document.createElement('span');n.textContent=v;
    n.style.cssText='font-weight:600;color:'+laneColor(v);
    const t=document.createElement('span');t.className='note'+(note==='live'?' live':'');
    t.textContent=note;
    r.append(n,t);
    r.onmousedown=ev=>{ev.preventDefault();pick(v)};  // mousedown beats the input's blur
    r.onmouseenter=()=>{sel=i;highlight()};
    return r;
  }));
  lsug.style.display='block';
  sugOpen=true;
  lane.setAttribute('aria-expanded','true');
  lchev.style.transform='rotate(180deg)';
  j.style.display='none';  // the dropdown owns that corner while open — never overlap the ↓
}
function rows(){return lsug.querySelectorAll('.lrow')}
function highlight(){[...rows()].forEach((r,i)=>r.classList.toggle('sel',i===sel))}
function hideSug(){
  lsug.style.display='none';sel=0;sugOpen=false;
  lane.setAttribute('aria-expanded','false');
  lchev.style.transform='';
  showJump();
}
function pick(v){lane.value=v;hideSug();document.getElementById('msg').focus()}
lane.addEventListener('input',()=>{sel=0;renderSug()});
lane.addEventListener('focus',()=>{sel=0;renderSug()});
lane.addEventListener('blur',()=>setTimeout(hideSug,150));
lane.addEventListener('keydown',ev=>{
  const open=sugOpen,rs=rows(),n=rs.length;
  if(ev.key==='ArrowDown'&&open){sel=(sel+1)%n;highlight();ev.preventDefault()}
  else if(ev.key==='ArrowUp'&&open){sel=(sel-1+n)%n;highlight();ev.preventDefault()}
  else if(ev.key==='Escape'&&open){hideSug();ev.preventDefault()}
  else if(ev.key==='Tab'&&open){lane.value=rs[sel].dataset.v;hideSug()}
  else if(ev.key==='Enter'){ev.preventDefault();  // Enter in the lane box NEVER submits the steer
    if(open)pick(rs[sel].dataset.v);
    else document.getElementById('msg').focus()}
});
// steer outbox: every sent message becomes a chip above the bar and walks the 🧭 lifecycle the
// harness reports — sent (grey) → delivered to N agents (amber, stdin queued) → read by the agent
// (green, the CLI replayed the turn into its conversation) → pops. Matched by raw char count (the
// only key the delivery/read lines carry); older-harness stages emit no read line, so a delivered
// chip pops on a timer instead of going green.
const q=document.getElementById('q'),chips=[];
function addChip(text,laneV,id,routing){
  // Material input-chip anatomy: label + SEPARATE trailing \u2715 target; the chip body is inert.
  // ALWAYS-ACCEPT (research round 2: store-and-forward messengers, K8s Pending+FailedScheduling,
  // Cursor's confirm-free queue): routing is never a blocking decision — it seeds the chip's
  // INITIAL state, naming the reason and the fate on the message itself. \u2715 on an undelivered
  // chip retracts immediately with an UNDO offer (Raskin: never a warning when you mean undo).
  const el=document.createElement('span');
  const waiting=routing==='finished'||routing==='unknown';
  el.className='chip '+(waiting?'skip':'sent');
  const tx=document.createElement('span');tx.className='txt';
  tx.textContent=(laneV?laneV+' \u00b7 ':'')+text;
  el.appendChild(tx);
  const c={el,tx,len:text.length,lane:laneV,id:id,dcount:0,state:waiting?'skip':'sent',timer:0,text:text};
  if(routing==='finished'){
    el.title="'"+laneV+"' already finished this run \u2014 delivers only if it re-runs; expires at run end";
  }else if(routing==='unknown'){
    el.title="queued \u2014 delivers when '"+laneV+"' starts; expires at run end if it never does";
  }else{
    el.title='sent \u2014 awaiting \ud83e\udded delivery'+(laneV?" (lane '"+laneV+"' waits for that agent to be live)":'');
  }
  const x=document.createElement('button');x.type='button';x.className='x';c.x=x;
  x.textContent='\u2715';
  x.setAttribute('aria-label','retract this message');
  x.title='retract \u2014 the agents will never receive this';
  x.onclick=async()=>{
    if((c.state==='sent'||c.state==='skip')&&c.id){
      try{const r=await fetch('/retract',{method:'POST',
        headers:{'Content-Type':'application/json','X-Rfc-Steer':'1'},
        body:JSON.stringify({id:c.id})});
        const j=await r.json().catch(()=>({}));
        if(j.retracted)offerUndo(c);           // executed immediately; undo restores (6s window)
        else{s.textContent='already delivered \u2014 cannot retract';
          setTimeout(()=>s.textContent='streaming',4000)}
      }catch(e){}}
    popChip(c)};
  el.appendChild(x);
  chips.push(c);q.appendChild(el);q.style.display='flex';
}
function offerUndo(c){
  // undo-over-confirm: the retract already happened; this restores by re-posting
  s.innerHTML='retracted \u00b7 ';
  const u=document.createElement('button');u.type='button';u.className='act';u.textContent='undo';
  u.onclick=async()=>{s.textContent='streaming';await postSteer(c.text,c.lane)};
  s.appendChild(u);
  setTimeout(()=>{if(u.isConnected)s.textContent='streaming'},6000);
}
function chipExpired(laneV){
  // invocation ended with this steer undelivered — terminal state (TTL: control messages expire,
  // they are never delivered to a later run out of context)
  const c=chips.find(x=>x.lane===laneV&&(x.state==='sent'||x.state==='skip'));if(!c)return;
  c.state='expired';c.el.className='chip expired';
  c.el.title='expired — the run ended before any matching agent could take this';
  if(c.x){c.x.title='dismiss this notification';c.x.setAttribute('aria-label','dismiss this notification')}
}
function chipDelivered(len,label){
  const c=chips.find(x=>x.len===len&&x.state!=='read');if(!c)return;
  c.state='delivered';c.dcount++;
  c.el.className='chip delivered';
  // the message reached an agent — it cannot be unsaid, so the \u2715 is now a plain dismiss and
  // its affordance must say so (field question 2026-08-21: green chip still claimed 'retract')
  if(c.x){c.x.title='dismiss this notification';c.x.setAttribute('aria-label','dismiss this notification')}
  c.el.title='delivered to '+label+(c.dcount>1?' (×'+c.dcount+')':'')+' — green when the agent reads it';
  clearTimeout(c.timer);c.timer=setTimeout(()=>popChip(c),15000);
}
function chipSkipped(laneV,label){
  // a live agent looked at the steer and passed on it (lane mismatch). While the chip is still
  // 'sent', that is the OPERATOR-level truth so far: nothing running matches this lane — say so
  // instead of pulsing 'awaiting delivery' forever (field report 2026-08-20: a lane-ground steer
  // during a review-only run sat queued with no human feedback while Langfuse showed the skips).
  // A matching agent that spawns later still upgrades this chip via the normal delivered path.
  const c=chips.find(x=>x.lane===laneV&&x.state==='sent');if(!c)return;
  c.state='skip';c.el.className='chip skip';
  c.el.title="no running agent matches lane '"+laneV+"' (last checked by "+label+") — the steer "
    +"stays queued and delivers if that lane runs later (e.g. a rerun of the stage)";
}
function chipRead(len){
  const c=chips.find(x=>x.len===len);if(!c)return;
  if(c.state!=='read'){c.state='read';c.el.className='chip read';c.el.title='read by the agent';
    if(c.x){c.x.title='dismiss this notification';c.x.setAttribute('aria-label','dismiss this notification')}}
  clearTimeout(c.timer);c.timer=setTimeout(()=>popChip(c),4000);
}
function popChip(c){c.el.style.opacity='0';
  setTimeout(()=>{c.el.remove();const i=chips.indexOf(c);if(i>=0)chips.splice(i,1);
    if(!chips.length)q.style.display='none'},650)}
function knownLanes(){
  // the CLOSED value domain (select-only combobox, ARIA APG / Slack-mention pattern): live
  // lanes, their stage prefixes, canonical stages (queued-future), finished lanes — everything
  // laneOptions() can offer. Free text exists only to FILTER; commits come from this set.
  const o=new Set([...lanesSeen,...STAGES]);
  for(const l of lanesSeen)if(l.includes('.'))o.add(l.split('.')[0]);
  return o;
}
async function sendSteer(e){e.preventDefault();
  const msg=document.getElementById('msg');
  if(!msg.value.trim()){msg.focus();return false}  // Enter with no text moves focus, never a silent no-op
  const laneV=lane.value.trim();
  if(laneV&&!knownLanes().has(laneV)){
    // entry-time constraint (research verdict: validate at entry, not send — typos must be
    // structurally impossible to commit). A unique prefix completes; anything else opens the
    // filtered list instead of sending.
    const m=[...knownLanes()].filter(l=>l.toLowerCase().startsWith(laneV.toLowerCase()));
    if(m.length===1){lane.value=m[0]}
    else{lane.setAttribute('aria-invalid','true');
      s.textContent="pick an agent from the list — '"+laneV+"' isn't one (blank = all live)";
      setTimeout(()=>{s.textContent='streaming';lane.removeAttribute('aria-invalid')},4000);
      lane.focus();sel=0;renderSug();return false}
  }
  await postSteer(msg.value.trim(),lane.value.trim());
  return false}
async function postSteer(text,laneV){
  const msg=document.getElementById('msg'),go=document.getElementById('go');
  go.disabled=true;                                // double-Enter must not send the same steer twice
  // (a CSS spinner replaces the label while disabled — async feedback per the loading-buttons rule)
  try{
    const r=await fetch('/steer',{method:'POST',
      headers:{'Content-Type':'application/json','X-Rfc-Steer':'1'},
      body:JSON.stringify({lane:laneV,text:text})});
    const j=r.ok?await r.json().catch(()=>({})):{};
    if(r.ok){msg.value='';
      addChip(text,laneV,j.id,j.routing);
      s.textContent=(r.status==202?'queued, but the run has been quiet a while (right feed?) → '
        :'steering queued → ')+(laneV?"lane '"+laneV+"'":'all live agents');
      setTimeout(()=>s.textContent='streaming',r.status==202?8000:4000)}
    else s.textContent='steer failed ('+r.status+')';
  }finally{go.disabled=false;msg.focus()}}
</script>"""

PORT = 8765


_REPLAY_CAP = 2_000_000  # full-history replay ceiling (0.24.16): a browser renders a whole run's
# log (~0.5MB / ~2.3k lines measured) instantly, so a fresh page gets ALL history — the old 4KB
# tail silently discarded everything above it on every reload; the cap only guards pathological
# multi-run logs from freezing the tab


def resume_pos(last_event_id, size: int) -> int:
    """Where a /feed connection starts. An SSE reconnect resumes at the byte offset the page last
    received (the browser sends Last-Event-ID automatically; we stamp every message's `id:` with
    its offset) — the old handler replayed the tail again on every reconnect, duplicating lines
    into the live page. A fresh page (no id) replays history from the start, capped; a stale id
    past EOF (log rotated) falls back to the capped start."""
    if last_event_id and str(last_event_id).isdigit() and int(last_event_id) <= size:
        return int(last_event_id)
    return max(0, size - _REPLAY_CAP)


def sse_frames(pos: int, chunk: bytes) -> tuple[bytes, int]:
    """COMPLETE lines of `chunk` (read from byte `pos`) as SSE frames, each stamped with
    id = the byte offset AFTER its line so Last-Event-ID resume never duplicates or drops.
    A partial trailing line stays unconsumed for the next poll — the old code emitted it torn,
    then emitted its remainder as a separate garbage line."""
    nl = chunk.rfind(b"\n")
    if nl < 0:
        return b"", pos
    out, off = bytearray(), pos
    for raw in chunk[: nl + 1].split(b"\n")[:-1]:
        off += len(raw) + 1
        out += f"id: {off}\ndata: {raw.decode('utf-8', 'replace')}\n\n".encode()
    return bytes(out), off


def lane_states(feed: Path) -> dict:
    """{'live': [...], 'finished': [...]} from every lifecycle line in the activity log — last
    event per label wins, so a stage that finished and re-ran (generate on a review back-jump)
    reads live again. Scanned once per page load; a multi-MB log costs milliseconds."""
    import contextlib
    import re as _re
    state: dict = {}
    with contextlib.suppress(OSError):
        for line in feed.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _re.match(r"\[\d\d:\d\d:\d\d\] \[([^\]]+)\] agent (started|finished)$", line)
            if m:
                state[m.group(1)] = m.group(2)
    return {"live": sorted(k for k, v in state.items() if v == "started"),
            "finished": sorted(k for k, v in state.items() if v == "finished")}


def steer_request_ok(headers, port=None) -> tuple[bool, str]:
    """CSRF boundary for POST /steer (0.24.1) — the localhost bind alone was not one: a no-preflight
    "simple" POST from any web page (or a DNS-rebound tab) landed an authoritative steering turn
    in a running agent. Three independent layers:
    - custom header: makes the request non-simple, so a cross-origin attempt needs a CORS preflight
      this server never grants
    - Origin allowlist: absent (curl / rfc-agent --steer) or this server's OWN origin. Never
      reject-all: the feed page's own fetch is same-origin and same-origin non-GET requests always
      carry Origin, so reject-all bricks the steer box on the first click.
    - loopback Host: kills DNS rebinding, where Origin looks same-origin to the browser

    The reject-ALL-Origin variant for servers whose only client is a local process (no browser
    page to serve) lives in rfc_agent/localnet.py:local_request_ok — used by the thinking tap.
    Kept separate, not imported: this script stays bare-python runnable by design, and the two
    Origin policies are opposites.
    """
    port = port or PORT
    if headers.get("X-Rfc-Steer") is None:
        return False, "missing X-Rfc-Steer header"
    origin = headers.get("Origin")
    if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
        return False, f"cross-origin ({origin})"
    host = (headers.get("Host") or "").split(":")[0]
    if host not in ("127.0.0.1", "localhost"):
        return False, f"non-loopback host ({host!r})"
    return True, ""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def do_POST(self):
        # steering inbox: same record shape rfc-agent --steer writes; raw text, wrapped at delivery.
        if self.path == "/retract":
            return self._retract()
        if self.path != "/steer":
            self.send_response(404)
            self.end_headers()
            return
        ok, _why = steer_request_ok(self.headers)
        if not ok:
            self.send_response(403)
            self.end_headers()
            return
        # Liveness is a WARNING, never a rejection (0.24.3 — the 0.24.2 409 misfired on every
        # legitimate quiet phase): the feed server starts BEFORE the run creates activity.log
        # (pre-run steers are a supported, tested case), and a run parked at a boundary checkpoint
        # writes nothing for as long as the operator deliberates. Since consumption = delivery,
        # a steer queued into a not-yet/no-longer-running dir is not lost — it delivers to that
        # run dir's next child. A 202 tells the operator to verify they're on the right run.
        stale = FEED.exists() and time.time() - FEED.stat().st_mtime > 900
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 65536:  # a steer is a sentence; unbounded read was a memory hole
                self.send_response(413)
                self.end_headers()
                return
            body = json.loads(self.rfile.read(n) or b"{}")
            text = str(body.get("text") or "").strip()[:4000]
            lane = str(body.get("lane") or "").strip()[:80]
            assert text
            run_dir = FEED.resolve().parent.parent  # logs/activity.log -> run dir
            # routing prognosis is INFORMATIONAL, never blocking (research 2026-08-21 round 2:
            # once fate is bounded (TTL) and the message retractable (undo), every precedent —
            # store-and-forward messengers, K8s Pending+FailedScheduling, RabbitMQ async returns,
            # Cursor's confirm-free queue — accepts optimistically and shows status ON the message;
            # a send-time modal fails NN/g's consequential+irreversible test and habituates away).
            st = lane_states(FEED)
            routing = "live"
            if lane and not any(x == lane or x.startswith(lane) for x in st["live"]):
                if any(x == lane or x.startswith(lane) for x in st["finished"]):
                    routing = "finished"
                elif st["live"] or st["finished"]:
                    routing = "unknown"
                else:
                    routing = "prerun"  # nothing has run yet — queueing ahead is the tested case
            # synthetic = the form's `test` toggle: a feature-test steer rides the real session
            # (it must, to test anything) but is MARKED so steer analysis can exclude it — the
            # OTel user_agent.synthetic.type / GA4 traffic_type shape. Marked, never dropped.
            synthetic = bool(body.get("synthetic"))
            rec = {"ts": time.time(), "id": uuid.uuid4().hex, "lane": lane, "text": text,
                   **({"synthetic": True} if synthetic else {})}
            inbox = run_dir / "logs" / "steer.inbox.jsonl"
            inbox.parent.mkdir(parents=True, exist_ok=True)  # pre-run steers arrive before logs/
            with inbox.open("a") as f:
                f.write(json.dumps(rec) + "\n")
            from datetime import UTC, datetime
            with (run_dir / "logs" / "operator_actions.jsonl").open("a") as f:
                f.write(json.dumps({"ts": datetime.now(UTC).isoformat(timespec="seconds"),
                                    "action": "steer",
                                    "detail": {"lane": lane, "text": text[:2000],
                                               **({"synthetic": True} if synthetic else {})}}) + "\n")
            out = json.dumps({"id": rec["id"], "routing": routing}).encode()
            self.send_response(202 if stale else 200)  # 202 = queued, but verify the run is live
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        except Exception:
            self.send_response(400)
            self.end_headers()

    def _retract(self):
        """Retract a never-delivered steer: an explicit dismiss on an undelivered message is plain
        cancellation (Cursor removes queued follow-ups; messengers unsend even post-delivery).
        Tombstone = a 'retracted' entry in the delivery ledger — the exact record shape
        rfc_agent.steer.mark_consumed writes (duplicated, not imported: this script stays
        bare-python runnable by design), so every future pump's consumed_before excludes the id.
        Delivery wins any race: an id already in the ledger returns retracted:false."""
        ok, _why = steer_request_ok(self.headers)
        if not ok:
            self.send_response(403)
            self.end_headers()
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 4096:
                self.send_response(413)
                self.end_headers()
                return
            rid = str((json.loads(self.rfile.read(n) or b"{}")).get("id") or "").strip()
            assert rid
            run_dir = FEED.resolve().parent.parent
            ledger = run_dir / "logs" / "steer.delivered.jsonl"
            settled = ledger.exists() and any(
                json.loads(ln).get("id") == rid
                for ln in ledger.read_text(encoding="utf-8", errors="replace").splitlines() if ln)
            if not settled:
                ledger.parent.mkdir(parents=True, exist_ok=True)
                with ledger.open("a") as f:
                    f.write(json.dumps({"id": rid, "ts": time.time(), "lane": "retracted"}) + "\n")
                with FEED.open("a", encoding="utf-8") as f:
                    f.write(f"[{time.strftime('%H:%M:%S')}] [run] 🧭 steering retracted by the operator\n")
                # the retraction is an operator SIGNAL (a human changed their mind) — record it for
                # the trace flush like the send itself; enrich with the record's lane/text so the
                # operator:steer_retracted event is readable without cross-referencing ids (0.24.30)
                lane_v, text_v = "", ""
                inbox = run_dir / "logs" / "steer.inbox.jsonl"
                if inbox.exists():
                    for iln in inbox.read_text(encoding="utf-8", errors="replace").splitlines():
                        try:
                            irec = json.loads(iln)
                            if irec.get("id") == rid:
                                lane_v, text_v = irec.get("lane") or "", (irec.get("text") or "")[:2000]
                        except Exception:
                            pass
                from datetime import UTC, datetime
                with (run_dir / "logs" / "operator_actions.jsonl").open("a") as f:
                    f.write(json.dumps({"ts": datetime.now(UTC).isoformat(timespec="seconds"),
                                        "action": "steer_retracted",
                                        "detail": {"id": rid, "lane": lane_v, "text": text_v}}) + "\n")
            out = json.dumps({"retracted": not settled}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        except Exception:
            self.send_response(400)
            self.end_headers()

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(PAGE.encode())
            return
        if self.path == "/lanes":
            # lifecycle state from the WHOLE log, not the SSE tail: the feed replays only the last
            # ~4KB, so a fresh page had no idea ingest/extract/ground ever ran and mislabeled
            # finished stages as "queued until stage starts" (field report, 0.24.15)
            body = json.dumps(lane_states(FEED)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path != "/feed":
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # no Access-Control-Allow-Origin: the feed page is same-origin, and ACAO:* handed the run's
        # live transcript to any web page the operator had open
        self.end_headers()
        pos = resume_pos(self.headers.get("Last-Event-ID"),
                         FEED.stat().st_size if FEED.exists() else 0)
        try:
            while True:
                size = FEED.stat().st_size if FEED.exists() else 0
                if size < pos:
                    pos = 0  # log replaced/truncated under us — restart rather than hang forever
                emitted = False
                if size > pos:
                    with open(FEED, "rb") as f:
                        f.seek(pos)
                        chunk = f.read()
                    frames, pos = sse_frames(pos, chunk)
                    if frames:
                        self.wfile.write(frames)
                        self.wfile.flush()
                        emitted = True
                if not emitted:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError):
            pass


if __name__ == "__main__":
    FEED = Path(sys.argv[1])
    if len(sys.argv) > 2:
        PORT = int(sys.argv[2])
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
