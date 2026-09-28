#!/usr/bin/env python3
"""
Signature Bridge — sign on your phone, receive on your computer.

How it works
------------
1. Run:            python3 signature_bridge.py
2. A page opens on this computer showing a QR code and a 4-digit PIN.
3. Scan the QR with your phone, enter the PIN -> a white signing pad opens.
4. Pick an ink colour/thickness, sign (pinch to zoom, two fingers to pan),
   optionally add a printed name/date, then tap Send.
5. The signature appears on the computer, where you can save it as
   PNG (white or transparent) or as text (base64 data URL in a .txt).
   Each signature is also auto-saved to a ./signatures folder.

If the phone can't open the QR link on your Wi-Fi (it just spins or says
"can't connect") even though both devices are on the same network, your
router is probably blocking device-to-device connections ("client isolation",
common on guest/public Wi-Fi). Route around it with the built-in tunnel:

    python3 signature_bridge.py --tunnel

(needs cloudflared: winget install --id Cloudflare.cloudflared)

Options
-------
  --port 8765            change the port
  --tunnel               auto-start a cloudflared tunnel so the phone reaches
                         this PC over the internet (bypasses router isolation)
  --public-url URL       use a tunnel URL you started yourself instead, e.g.
                         cloudflared tunnel --url http://localhost:8765
                         python3 signature_bridge.py --public-url https://xyz.trycloudflare.com
  --no-pin               don't require the phone to enter the PIN (the PIN keeps
                         strangers off a public tunnel URL; on by default)
  --save-dir DIR         folder to auto-save received signatures into
                         (default: ./signatures; use '' to disable)
  --no-open              don't auto-open the browser

Tunneling needs cloudflared; everything else is Python 3.8+ standard library.
"""

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import base64
import datetime
import random
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ----------------------------------------------------------------------------
# In-memory session store
# ----------------------------------------------------------------------------
# sid -> {"png", "opened", "created", "pin", "verified", "attempts"}
SESSIONS = {}
LOCK = threading.Lock()
BASE_URL = ""          # set in main()
REQUIRE_PIN = True     # set in main() (disabled by --no-pin)
SAVE_DIR = None        # set in main(); None disables auto-save
MAX_PIN_ATTEMPTS = 8   # per session, before it locks


def gen_pin():
    return f"{random.randint(0, 9999):04d}"


def save_signature_png(png_dataurl, sid):
    """Decode a data:image/png;base64 URL and write it to SAVE_DIR.

    Returns the file path, or None if saving is disabled. Never raises — a disk
    problem must not turn a received signature into an error for the phone.
    """
    if not SAVE_DIR:
        return None
    try:
        b64 = png_dataurl.split(",", 1)[1]
        data = base64.b64decode(b64)
        os.makedirs(SAVE_DIR, exist_ok=True)
        # Timestamp + microseconds + sid keeps names unique even for rapid resends.
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = os.path.join(SAVE_DIR, f"signature-{stamp}-{sid[:4]}.png")
        with open(path, "wb") as f:
            f.write(data)
        return path
    except Exception:
        return None

# ----------------------------------------------------------------------------
# Desktop page (QR + receiver)
# ----------------------------------------------------------------------------
DESKTOP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Signature Bridge</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrious/4.0.2/qrious.min.js"></script>
<style>
  :root{
    --ink:#141B33; --paper:#F6F5F0; --line:#D8D5C9;
    --accent:#2E6E62; --accent-soft:#DFEAE6; --muted:#6B6F7E;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{
    font-family:'Inter',system-ui,sans-serif;background:var(--paper);color:var(--ink);
    min-height:100vh;
    background-image:repeating-linear-gradient(0deg, transparent 0 31px, rgba(20,27,51,0.035) 31px 32px);
  }
  .wrap{max-width:640px;margin:0 auto;padding:44px 24px 60px}
  h1{font-family:'Fraunces',serif;font-weight:700;font-size:clamp(28px,4.5vw,40px);letter-spacing:-0.02em}
  .sub{color:var(--muted);margin-top:6px;font-size:15px}
  .panel{background:#fff;border:1px solid var(--line);border-radius:12px;padding:26px;margin-top:26px;
         box-shadow:0 1px 0 rgba(20,27,51,0.04)}
  .steps{display:flex;gap:18px;flex-wrap:wrap;margin-top:18px;font-size:13.5px;color:var(--muted)}
  .steps b{color:var(--ink)}
  .qr-area{display:flex;flex-direction:column;align-items:center;gap:14px;padding:8px 0}
  #qr{border:1px solid var(--line);border-radius:10px;background:#fff}
  .url{font-size:12.5px;color:var(--muted);word-break:break-all;text-align:center;max-width:44ch;user-select:all}
  .pin-box{display:flex;flex-direction:column;align-items:center;gap:4px;
    background:var(--accent-soft);border:1px solid #C9DDD6;border-radius:10px;padding:10px 22px;margin-top:4px}
  .pin-label{font-size:12px;color:var(--accent);font-weight:600;text-transform:uppercase;letter-spacing:.04em}
  .pin-digits{font-family:'Fraunces',serif;font-weight:700;font-size:30px;color:var(--ink);letter-spacing:8px;
    padding-left:8px;user-select:all}
  .status{display:flex;align-items:center;gap:10px;justify-content:center;margin-top:6px;
          font-size:14px;font-weight:500}
  .dot{width:9px;height:9px;border-radius:50%;background:var(--muted);animation:pulse 1.4s infinite}
  .dot.on{background:var(--accent)}
  @keyframes pulse{0%,100%{opacity:.35}50%{opacity:1}}
  #result{display:none}
  .sig-card{
    background:
      repeating-conic-gradient(#f0efe9 0 25%, #ffffff 0 50%) 0 0 / 22px 22px;
    border:1px dashed var(--line);border-radius:10px;padding:20px;
    display:flex;justify-content:center;align-items:center;min-height:150px;
  }
  .sig-card img{max-width:100%;max-height:260px}
  .actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}
  button{
    font-family:'Inter',sans-serif;font-weight:600;font-size:14px;border-radius:8px;
    padding:11px 16px;cursor:pointer;transition:transform .08s, background .15s;
  }
  .primary{background:var(--ink);color:#fff;border:1px solid var(--ink)}
  .primary:hover{background:#0d1226}
  .ghost{background:#fff;color:var(--ink);border:1px solid var(--line)}
  .ghost:hover{background:var(--paper)}
  button:active{transform:translateY(1px)}
  button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  h2{font-family:'Fraunces',serif;font-size:17px;font-weight:700;margin-bottom:14px}
  .toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%) translateY(80px);
    background:var(--ink);color:#fff;padding:10px 18px;border-radius:8px;font-size:14px;
    opacity:0;transition:all .25s;pointer-events:none}
  .toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
  @media (prefers-reduced-motion: reduce){ *{transition:none!important;animation:none!important} }
</style>
</head>
<body>
<div class="wrap">
  <h1>Signature Bridge</h1>
  <p class="sub">Sign on your phone — receive it here.</p>

  <div class="panel" id="qrPanel">
    <h2>▦ Scan with your phone</h2>
    <div class="qr-area">
      <canvas id="qr" width="220" height="220" aria-label="QR code linking to the signing pad"></canvas>
      <div class="url" id="signUrl">…</div>
      <div class="pin-box" id="pinBox" style="display:none">
        <span class="pin-label">Enter this code on your phone</span>
        <span class="pin-digits" id="pinDigits">— — — —</span>
      </div>
      <div class="status"><span class="dot" id="dot"></span><span id="statusText">Waiting for phone…</span></div>
    </div>
    <div class="steps">
      <span><b>1</b>&nbsp;Scan the code</span>
      <span><b>2</b>&nbsp;Enter the code &amp; sign</span>
      <span><b>3</b>&nbsp;Tap Send — it lands here</span>
    </div>
  </div>

  <div class="panel" id="result">
    <h2>🖋 Signature received</h2>
    <div class="sig-card"><img id="sigImg" alt="Received signature"></div>
    <div class="actions">
      <button class="primary" id="savePngT">Save PNG (transparent)</button>
      <button class="primary" id="savePngW">Save PNG (white)</button>
      <button class="ghost" id="saveTxt">Save as text (.txt)</button>
      <button class="ghost" id="copyB64">Copy base64</button>
      <button class="ghost" id="newSig">New signature</button>
    </div>
    <p style="font-size:12.5px;color:var(--muted);margin-top:12px">
      The .txt / base64 is a <code>data:image/png</code> URL — paste it straight into an
      <code>&lt;img src&gt;</code>, CSS, or a database field. Redo &amp; resend from the phone updates this preview live.
    </p>
  </div>
</div>
<div class="toast" id="toast">Saved ✓</div>


<script>
(function(){
  let sid = null, lastPng = null, timer = null;
  const $ = id => document.getElementById(id);

  function toast(msg){ const t=$('toast'); t.textContent=msg; t.classList.add('show');
    setTimeout(()=>t.classList.remove('show'),1600); }

  function renderQR(url){
    $('signUrl').textContent = url;
    try{
      new QRious({ element: $('qr'), value: url, size: 220, level: 'M',
                   padding: 14, background:'#ffffff', foreground:'#141B33' });
    }catch(e){
      $('qr').style.display='none';
      $('signUrl').insertAdjacentHTML('afterend',
        '<div style="color:#7F1D1D;font-size:13px">QR library failed to load — type the link above into your phone browser.</div>');
    }
  }

  async function newSession(){
    if(timer) clearInterval(timer);
    lastPng = null;
    $('result').style.display='none';
    $('qrPanel').style.display='block';
    $('dot').classList.remove('on');
    $('statusText').textContent='Waiting for phone…';
    const r = await fetch('/new'); const j = await r.json();
    sid = j.sid; renderQR(j.sign_url);
    if(j.pin){
      $('pinDigits').textContent = j.pin.split('').join(' ');
      $('pinBox').style.display = 'flex';
    } else {
      $('pinBox').style.display = 'none';
    }
    timer = setInterval(poll, 900);
  }

  async function poll(){
    try{
      const r = await fetch('/result?s='+sid); if(!r.ok) return;
      const j = await r.json();
      if(j.opened && !j.ready){
        $('dot').classList.add('on');
        $('statusText').textContent='Phone connected — waiting for your signature…';
      }
      if(j.ready && j.png !== lastPng){
        lastPng = j.png;
        $('sigImg').src = j.png;
        $('result').style.display='block';
        $('dot').classList.add('on');
        $('statusText').textContent='Signature received ✓ (phone can redo & resend)';
      }
    }catch(e){ /* server briefly unreachable; keep polling */ }
  }

  function download(href, name){
    const a=document.createElement('a'); a.href=href; a.download=name; a.click();
  }

  $('savePngT').addEventListener('click', ()=>{ if(lastPng){ download(lastPng,'signature-transparent.png'); toast('Saved ✓'); }});

  $('savePngW').addEventListener('click', ()=>{
    if(!lastPng) return;
    const img=new Image();
    img.onload=()=>{
      const c=document.createElement('canvas'); c.width=img.width; c.height=img.height;
      const x=c.getContext('2d'); x.fillStyle='#fff'; x.fillRect(0,0,c.width,c.height); x.drawImage(img,0,0);
      download(c.toDataURL('image/png'),'signature-white.png'); toast('Saved ✓');
    };
    img.src=lastPng;
  });

  $('saveTxt').addEventListener('click', ()=>{
    if(!lastPng) return;
    const blob=new Blob([lastPng],{type:'text/plain'});
    download(URL.createObjectURL(blob),'signature.txt'); toast('Saved ✓');
  });

  $('copyB64').addEventListener('click', async ()=>{
    if(!lastPng) return;
    try{ await navigator.clipboard.writeText(lastPng); toast('Copied ✓'); }
    catch(e){ toast('Copy blocked — use Save as text'); }
  });

  $('newSig').addEventListener('click', newSession);

  newSession();
})();
</script>
</body>
</html>
"""

# ----------------------------------------------------------------------------
# Phone page (blank white signing pad)
# ----------------------------------------------------------------------------
PHONE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<meta name="theme-color" content="#ffffff">
<title>Sign here</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{height:100%;overflow:hidden;background:#fff;
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
    touch-action:none;-webkit-user-select:none;user-select:none}
  button{font:inherit;font-weight:600;font-size:15px;border-radius:10px;padding:10px 16px;cursor:pointer}
  .ghost{background:#fff;border:1px solid #D8D5C9;color:#141B33}
  .primary{background:#141B33;border:1px solid #141B33;color:#fff}
  button:disabled{opacity:.35}

  /* ---- PIN gate ---- */
  #gate{position:fixed;inset:0;z-index:5;background:#F6F5F0;display:flex;
    align-items:center;justify-content:center;padding:24px}
  .gate-card{background:#fff;border:1px solid #D8D5C9;border-radius:16px;
    padding:28px 24px;max-width:340px;width:100%;text-align:center;
    box-shadow:0 6px 24px rgba(20,27,51,.08)}
  .gate-card h1{font-size:21px;color:#141B33;margin-bottom:8px}
  .gate-card p{color:#6B6F7E;font-size:14.5px;line-height:1.4;margin-bottom:18px}
  #pinInput{width:100%;text-align:center;font-size:34px;letter-spacing:14px;
    font-weight:700;color:#141B33;border:2px solid #D8D5C9;border-radius:12px;
    padding:12px 8px 12px 22px;margin-bottom:12px;background:#fff}
  #pinInput:focus{outline:none;border-color:#2E6E62}
  #gateMsg{min-height:20px;font-size:13.5px;color:#B91C1C;margin-bottom:12px}
  #pinGo{width:100%}
  #gateStatus{color:#6B6F7E;font-size:15px}
  .hide{display:none!important}

  /* ---- Pad ---- */
  /* One fixed header holds the action row (Undo/Clear/Send) stacked above the
     tools row (ink/size/name/date) so they never overlap. */
  #topbar{position:fixed;top:0;left:0;right:0;z-index:3;background:rgba(255,255,255,.96);
    border-bottom:1px solid #ECEAE2;backdrop-filter:blur(6px);
    padding-top:env(safe-area-inset-top)}
  #bar{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:10px 12px}
  #bar #send{padding:11px 22px}
  .grp{display:flex;gap:8px}
  #tools{display:flex;align-items:center;gap:12px;padding:8px 12px 10px;
    border-top:1px solid #F1EFE7;overflow-x:auto;white-space:nowrap;touch-action:pan-x}
  #tools .lab{font-size:12px;color:#9A9788;font-weight:600}
  .swatches,.widths{display:flex;gap:7px;align-items:center}
  .sw{width:26px;height:26px;border-radius:50%;padding:0;border:2px solid #fff;
    box-shadow:0 0 0 1px #D8D5C9;cursor:pointer}
  .sw.on{box-shadow:0 0 0 2px #2E6E62}
  .wd{width:30px;height:26px;padding:0;display:flex;align-items:center;justify-content:center;
    border:1px solid #D8D5C9;border-radius:8px;background:#fff}
  .wd.on{border-color:#2E6E62;background:#DFEAE6}
  .wd .dot{background:#141B33;border-radius:50%}
  #nameInput{flex:0 0 130px;min-width:110px;font-size:14px;font-weight:500;color:#141B33;
    border:1px solid #D8D5C9;border-radius:8px;padding:7px 10px;background:#fff}
  #nameInput:focus{outline:none;border-color:#2E6E62}
  .datechk{display:flex;align-items:center;gap:5px;font-size:13px;color:#6B6F7E;font-weight:600}
  .datechk input{width:16px;height:16px}

  #cv{position:fixed;inset:0;display:block;background:#fff;touch-action:none}
  #hint{position:fixed;left:0;right:0;top:52%;text-align:center;color:#C9C7BD;
    font-size:17px;font-style:italic;pointer-events:none;z-index:1}
  #zbar{position:fixed;right:12px;bottom:calc(14px + env(safe-area-inset-bottom));z-index:2;
    display:flex;align-items:center;gap:8px;background:rgba(255,255,255,.92);
    border:1px solid #ECEAE2;border-radius:20px;padding:5px 6px 5px 12px;backdrop-filter:blur(6px)}
  #zbar #zlabel{font-size:12.5px;color:#6B6F7E;font-weight:600;min-width:38px;text-align:right}
  #zbar .mini{font-size:13px;padding:6px 12px;border-radius:14px}
  #gesturehint{position:fixed;left:0;right:0;bottom:calc(16px + env(safe-area-inset-bottom));
    text-align:center;color:#C9C7BD;font-size:12.5px;pointer-events:none;z-index:1}

  #done{position:fixed;inset:0;background:#fff;z-index:6;display:none;
    flex-direction:column;align-items:center;justify-content:center;gap:16px;text-align:center;padding:24px}
  #done .check{width:64px;height:64px;border-radius:50%;background:#DFEAE6;color:#2E6E62;
    display:flex;align-items:center;justify-content:center;font-size:32px}
  #done h1{font-size:20px;color:#141B33}
  #done p{color:#6B6F7E;font-size:14.5px;max-width:32ch}
</style>
</head>
<body>
<!-- PIN gate -->
<div id="gate">
  <div class="gate-card">
    <div id="gateStatus">Connecting…</div>
    <div id="pinForm" class="hide">
      <h1>Enter code</h1>
      <p>Type the 4-digit code shown on your computer screen.</p>
      <input id="pinInput" inputmode="numeric" pattern="[0-9]*" maxlength="4"
             autocomplete="off" aria-label="4-digit code">
      <div id="gateMsg"></div>
      <button id="pinGo" class="primary">Continue</button>
    </div>
  </div>
</div>

<!-- Signing pad -->
<div id="pad" class="hide">
  <div id="topbar">
  <div id="bar">
    <div class="grp">
      <button class="ghost" id="undo" disabled>Undo</button>
      <button class="ghost" id="clear" disabled>Clear</button>
    </div>
    <button class="primary" id="send" disabled>Send ✓</button>
  </div>
  <div id="tools">
    <span class="lab">Ink</span>
    <div class="swatches" id="swatches">
      <button class="sw on" data-color="#141B33" style="background:#141B33" aria-label="Black ink"></button>
      <button class="sw" data-color="#1D4ED8" style="background:#1D4ED8" aria-label="Blue ink"></button>
      <button class="sw" data-color="#B91C1C" style="background:#B91C1C" aria-label="Red ink"></button>
    </div>
    <span class="lab">Size</span>
    <div class="widths" id="widths">
      <button class="wd" data-w="2.5" aria-label="Thin"><span class="dot" style="width:5px;height:5px"></span></button>
      <button class="wd on" data-w="3.5" aria-label="Medium"><span class="dot" style="width:8px;height:8px"></span></button>
      <button class="wd" data-w="6" aria-label="Thick"><span class="dot" style="width:12px;height:12px"></span></button>
    </div>
    <input id="nameInput" placeholder="Name (optional)" maxlength="40" autocomplete="off">
    <label class="datechk"><input type="checkbox" id="dateChk">Date</label>
  </div>
  </div>
  <canvas id="cv" aria-label="Signature pad — draw with one finger, pinch to zoom"></canvas>
  <div id="hint">Sign here with your finger</div>
  <div id="gesturehint">One finger draws · pinch to zoom · two fingers to pan</div>
  <div id="zbar">
    <button class="ghost mini" id="zoomReset">Reset view</button>
    <span id="zlabel">100%</span>
  </div>
</div>

<!-- Sent overlay -->
<div id="done">
  <div class="check">✓</div>
  <h1>Signature sent</h1>
  <p>It's now on your computer. Made a mistake? You can redo it and send again.</p>
  <button class="primary" id="again">Redo &amp; resend</button>
</div>

<script>
(function(){
  var sid = new URLSearchParams(location.search).get('s') || '';
  var enteredPin = '';
  var $ = function(id){ return document.getElementById(id); };

  // ---------- PIN gate ----------
  function showPad(){
    $('gate').classList.add('hide');
    $('pad').classList.remove('hide');
    resize();
  }
  function gateStatus(msg){ var s=$('gateStatus'); s.classList.remove('hide'); s.textContent=msg;
    $('pinForm').classList.add('hide'); }
  function showPinForm(){ $('gateStatus').classList.add('hide'); $('pinForm').classList.remove('hide');
    setTimeout(function(){ $('pinInput').focus(); }, 50); }

  function verify(pin){
    return fetch('/verify?s='+encodeURIComponent(sid)+'&pin='+encodeURIComponent(pin||''))
      .then(function(r){ return r.json(); });
  }
  function probe(){
    verify('').then(function(j){
      if(j.ok){ showPad(); }
      else if(j.unknown){ gateStatus('This session expired. Get a new QR code from the computer.'); }
      else if(j.locked){ gateStatus('Too many wrong codes. Tap "New signature" on the computer for a fresh code.'); }
      else { showPinForm(); }
    }).catch(function(){ gateStatus('Could not reach the computer. Check the connection and reload.'); });
  }
  $('pinGo').addEventListener('click', function(){
    var pin = ($('pinInput').value || '').replace(/\D/g,'').slice(0,4);
    if(pin.length < 4){ $('gateMsg').textContent = 'Enter all 4 digits.'; return; }
    $('gateMsg').textContent = '';
    verify(pin).then(function(j){
      if(j.ok){ enteredPin = pin; showPad(); }
      else if(j.locked){ gateStatus('Too many wrong codes. Tap "New signature" on the computer for a fresh code.'); }
      else {
        $('gateMsg').textContent = 'Wrong code' + (j.left!=null ? ' — '+j.left+' tries left' : '') + '. Try again.';
        $('pinInput').value=''; $('pinInput').focus();
      }
    }).catch(function(){ $('gateMsg').textContent = 'Could not reach the computer. Try again.'; });
  });
  $('pinInput').addEventListener('keydown', function(e){ if(e.key==='Enter') $('pinGo').click(); });

  // ---------- Pad ----------
  var cv = $('cv'), ctx = cv.getContext('2d');
  var pen = { color:'#141B33', width:3.5 };
  var strokes = [];            // [{pts:[{x,y}], color, width}] in WORLD coords
  var cur = null;              // stroke in progress
  var view = { scale:1, tx:0, ty:0 };
  var MIN_S = 0.4, MAX_S = 8;
  var pointers = new Map();    // pointerId -> {x,y} screen (CSS px)
  var drawId = null;           // pointerId currently drawing
  var gesture = null;          // pinch/pan snapshot

  function cssSize(){ return { w: window.innerWidth, h: window.innerHeight }; }
  function screenToWorld(x, y){ return { x:(x - view.tx)/view.scale, y:(y - view.ty)/view.scale }; }

  function resize(){
    var dpr = window.devicePixelRatio || 1, s = cssSize();
    cv.width = Math.round(s.w * dpr); cv.height = Math.round(s.h * dpr);
    redraw();
  }

  function drawStroke(s){
    ctx.strokeStyle = s.color; ctx.fillStyle = s.color; ctx.lineWidth = s.width;
    ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    var p = s.pts;
    if(p.length === 1){ ctx.beginPath(); ctx.arc(p[0].x, p[0].y, s.width/2, 0, 7); ctx.fill(); return; }
    ctx.beginPath(); ctx.moveTo(p[0].x, p[0].y);
    for(var i=1;i<p.length;i++) ctx.lineTo(p[i].x, p[i].y);
    ctx.stroke();
  }

  function redraw(){
    var dpr = window.devicePixelRatio || 1;
    ctx.setTransform(1,0,0,1,0,0);
    ctx.clearRect(0, 0, cv.width, cv.height);
    ctx.setTransform(dpr*view.scale, 0, 0, dpr*view.scale, dpr*view.tx, dpr*view.ty);
    for(var i=0;i<strokes.length;i++) drawStroke(strokes[i]);
    if(cur) drawStroke(cur);
    updateUI();
  }

  function updateUI(){
    var has = strokes.length > 0 || (cur && cur.pts.length > 0);
    $('undo').disabled  = strokes.length === 0;
    $('clear').disabled = !has;
    $('send').disabled  = strokes.length === 0;
    $('hint').style.display = has ? 'none' : 'block';
    $('gesturehint').style.display = has ? 'none' : 'block';
    $('zlabel').textContent = Math.round(view.scale*100) + '%';
  }

  // ---- pointer / touch handling ----
  function twoPoints(){ var a=[]; pointers.forEach(function(v){ a.push(v); }); return a; }
  function dist(a,b){ return Math.hypot(a.x-b.x, a.y-b.y); }
  function mid(a,b){ return { x:(a.x+b.x)/2, y:(a.y+b.y)/2 }; }

  cv.addEventListener('pointerdown', function(e){
    e.preventDefault();
    try{ cv.setPointerCapture(e.pointerId); }catch(err){}
    pointers.set(e.pointerId, { x:e.clientX, y:e.clientY });
    if(pointers.size === 1){
      drawId = e.pointerId;
      cur = { pts:[ screenToWorld(e.clientX, e.clientY) ], color:pen.color, width:pen.width };
      redraw();
    } else if(pointers.size === 2){
      // second finger: abandon any in-progress stroke, start a pinch/pan gesture
      cur = null; drawId = null;
      var p = twoPoints();
      gesture = { d0:dist(p[0],p[1]), m0:mid(p[0],p[1]),
                  s0:view.scale, tx0:view.tx, ty0:view.ty };
      redraw();
    }
  });

  cv.addEventListener('pointermove', function(e){
    if(!pointers.has(e.pointerId)) return;
    e.preventDefault();
    pointers.set(e.pointerId, { x:e.clientX, y:e.clientY });
    if(gesture && pointers.size >= 2){
      var p = twoPoints();
      var d = dist(p[0],p[1]), m = mid(p[0],p[1]);
      var scale = Math.min(MAX_S, Math.max(MIN_S, gesture.s0 * (d / (gesture.d0||1))));
      // keep the world point under the original mid-point fixed, then follow the mid-point
      var wmx = (gesture.m0.x - gesture.tx0) / gesture.s0;
      var wmy = (gesture.m0.y - gesture.ty0) / gesture.s0;
      view.scale = scale;
      view.tx = m.x - wmx * scale;
      view.ty = m.y - wmy * scale;
      redraw();
    } else if(drawId === e.pointerId && cur){
      cur.pts.push( screenToWorld(e.clientX, e.clientY) );
      redraw();
    }
  });

  function onUp(e){
    var wasDraw = (e.pointerId === drawId);
    pointers.delete(e.pointerId);
    if(gesture){
      if(pointers.size < 2) gesture = null;   // leftover finger will NOT resume drawing
      return;
    }
    if(wasDraw){
      if(cur && cur.pts.length) strokes.push(cur);
      cur = null; drawId = null; redraw();
    }
  }
  cv.addEventListener('pointerup', onUp);
  cv.addEventListener('pointercancel', onUp);

  // ---- toolbar ----
  $('swatches').addEventListener('click', function(e){
    var b = e.target.closest('.sw'); if(!b) return;
    pen.color = b.getAttribute('data-color');
    Array.prototype.forEach.call(this.children, function(c){ c.classList.remove('on'); });
    b.classList.add('on');
  });
  $('widths').addEventListener('click', function(e){
    var b = e.target.closest('.wd'); if(!b) return;
    pen.width = parseFloat(b.getAttribute('data-w'));
    Array.prototype.forEach.call(this.children, function(c){ c.classList.remove('on'); });
    b.classList.add('on');
  });
  $('undo').addEventListener('click', function(){ strokes.pop(); redraw(); });
  $('clear').addEventListener('click', function(){ strokes = []; cur = null; redraw(); });
  $('zoomReset').addEventListener('click', function(){ view = { scale:1, tx:0, ty:0 }; redraw(); });

  // ---- export ----
  function exportPng(){
    var minX=1e9, minY=1e9, maxX=-1e9, maxY=-1e9;
    for(var i=0;i<strokes.length;i++){ var p=strokes[i].pts; for(var j=0;j<p.length;j++){
      if(p[j].x<minX)minX=p[j].x; if(p[j].y<minY)minY=p[j].y;
      if(p[j].x>maxX)maxX=p[j].x; if(p[j].y>maxY)maxY=p[j].y; } }
    if(minX===1e9) return null;
    var PAD=24, SCALE=2;
    var sigW = Math.max(1, maxX-minX), sigH = Math.max(1, maxY-minY);

    // label lines (name / date), baked under the signature
    var lines = [];
    var nm = ($('nameInput').value || '').trim();
    if(nm) lines.push({ text:nm, color:'#141B33', size:22 });
    if($('dateChk').checked){
      var d = new Date();
      lines.push({ text: d.toLocaleDateString(), color:'#6B6F7E', size:16 });
    }

    var meas = document.createElement('canvas').getContext('2d');
    var textW = 0, labelH = 0;
    lines.forEach(function(ln){
      meas.font = '600 '+ln.size+'px -apple-system,Segoe UI,Roboto,sans-serif';
      textW = Math.max(textW, meas.measureText(ln.text).width);
      labelH += ln.size*1.35;
    });
    if(lines.length) labelH += 16;

    var contentW = Math.max(sigW, textW);
    var w = contentW + PAD*2, h = sigH + labelH + PAD*2;
    var c = document.createElement('canvas');
    c.width = Math.round(w*SCALE); c.height = Math.round(h*SCALE);
    var x = c.getContext('2d');
    x.setTransform(SCALE,0,0,SCALE,0,0);

    // draw signature, centered horizontally within contentW
    x.save();
    x.translate(PAD + (contentW - sigW)/2 - minX, PAD - minY);
    x.lineCap='round'; x.lineJoin='round';
    for(var s=0;s<strokes.length;s++){
      var st = strokes[s];
      x.strokeStyle = st.color; x.fillStyle = st.color; x.lineWidth = st.width;
      if(st.pts.length===1){ x.beginPath(); x.arc(st.pts[0].x, st.pts[0].y, st.width/2, 0, 7); x.fill(); continue; }
      x.beginPath(); x.moveTo(st.pts[0].x, st.pts[0].y);
      for(var k=1;k<st.pts.length;k++) x.lineTo(st.pts[k].x, st.pts[k].y);
      x.stroke();
    }
    x.restore();

    // draw label lines centered under the signature
    var ly = PAD + sigH + 20;
    x.textAlign = 'center';
    lines.forEach(function(ln){
      ly += ln.size;
      x.font = '600 '+ln.size+'px -apple-system,Segoe UI,Roboto,sans-serif';
      x.fillStyle = ln.color;
      x.fillText(ln.text, PAD + contentW/2, ly);
      ly += ln.size*0.35;
    });
    return c.toDataURL('image/png');   // transparent background; ink + label only
  }

  // ---- send ----
  $('send').addEventListener('click', function(){
    var btn = this;
    if(strokes.length === 0) return;
    var png = exportPng();
    if(!png) return;
    btn.disabled = true; btn.textContent = 'Sending…';
    fetch('/submit', {
      method:'POST', headers:{ 'Content-Type':'application/json' },
      body: JSON.stringify({ sid:sid, pin:enteredPin, png:png })
    }).then(function(r){ return r.json(); }).then(function(j){
      if(j.ok){ $('done').style.display = 'flex'; }
      else if(j.error === 'not-verified'){ alert('This session needs the code again — reload and re-enter it.'); }
      else { alert('The computer rejected this session — ask it for a new QR code.'); }
    }).catch(function(){
      alert('Could not reach the computer. Are both devices connected?');
    }).then(function(){ btn.disabled = false; btn.textContent = 'Send ✓'; });
  });

  $('again').addEventListener('click', function(){
    $('done').style.display = 'none';
    strokes = []; cur = null; view = { scale:1, tx:0, ty:0 }; redraw();
  });

  window.addEventListener('resize', resize);
  if(window.visualViewport) window.visualViewport.addEventListener('resize', resize);

  probe();
})();
</script>
</body>
</html>
"""

# ----------------------------------------------------------------------------
# HTTP handler
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SignatureBridge/1.0"

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path == "/":
            self._send(200, DESKTOP_HTML)

        elif u.path == "/sign":
            self._send(200, PHONE_HTML)

        elif u.path == "/new":
            sid = uuid.uuid4().hex[:10]
            pin = gen_pin()
            now = time.time()
            with LOCK:
                for k in list(SESSIONS):            # prune sessions older than 1 h
                    if now - SESSIONS[k]["created"] > 3600:
                        SESSIONS.pop(k, None)
                SESSIONS[sid] = {"png": None, "opened": False, "created": now,
                                 "pin": pin, "verified": not REQUIRE_PIN, "attempts": 0}
            self._send(200, json.dumps({
                "sid": sid,
                "sign_url": BASE_URL + "/sign?s=" + sid,
                "pin": pin if REQUIRE_PIN else None,
            }), "application/json")

        elif u.path == "/verify":
            # Phone posts the PIN shown on the computer before it may sign.
            sid = (q.get("s") or [""])[0]
            pin = (q.get("pin") or [""])[0]
            with LOCK:
                sess = SESSIONS.get(sid)
                if sess is None:
                    resp = {"ok": False, "unknown": True}
                elif not REQUIRE_PIN or sess["verified"]:
                    sess["verified"] = True
                    sess["opened"] = True
                    resp = {"ok": True}
                elif pin == "":
                    # A probe (phone asking "is a PIN needed?") — don't count it.
                    resp = {"ok": False, "need_pin": True,
                            "locked": sess["attempts"] >= MAX_PIN_ATTEMPTS}
                elif sess["attempts"] >= MAX_PIN_ATTEMPTS:
                    resp = {"ok": False, "locked": True}
                elif pin == sess["pin"]:
                    sess["verified"] = True
                    sess["opened"] = True
                    resp = {"ok": True}
                else:
                    sess["attempts"] += 1
                    resp = {"ok": False, "need_pin": True,
                            "left": max(0, MAX_PIN_ATTEMPTS - sess["attempts"]),
                            "locked": sess["attempts"] >= MAX_PIN_ATTEMPTS}
            self._send(200, json.dumps(resp), "application/json")

        elif u.path == "/opened":
            sid = (q.get("s") or [""])[0]
            with LOCK:
                if sid in SESSIONS:
                    SESSIONS[sid]["opened"] = True
            self._send(200, '{"ok":true}', "application/json")

        elif u.path == "/result":
            sid = (q.get("s") or [""])[0]
            with LOCK:
                sess = SESSIONS.get(sid)
                payload = None if sess is None else {
                    "opened": sess["opened"],
                    "ready": sess["png"] is not None,
                    "png": sess["png"],
                }
            if payload is None:
                self._send(404, '{"error":"unknown session"}', "application/json")
            else:
                self._send(200, json.dumps(payload), "application/json")

        else:
            self._send(404, "Not found", "text/plain; charset=utf-8")

    def do_POST(self):
        if urlparse(self.path).path != "/submit":
            self._send(404, '{"ok":false}', "application/json")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 8_000_000:
                self._send(413, '{"ok":false,"error":"payload size"}', "application/json")
                return
            payload = json.loads(self.rfile.read(length))
            sid = payload.get("sid", "")
            png = payload.get("png", "")
            pin = payload.get("pin", "")
            valid_png = isinstance(png, str) and png.startswith("data:image/png;base64,")
            ok = False
            reason = "bad-request"
            with LOCK:
                sess = SESSIONS.get(sid) if sid else None
                if sess is None:
                    reason = "unknown-session"
                elif not valid_png:
                    reason = "bad-image"
                elif REQUIRE_PIN and not (sess["verified"] or pin == sess["pin"]):
                    reason = "not-verified"
                else:
                    sess["verified"] = True     # a correct PIN in the body counts too
                    sess["png"] = png
                    ok = True
        except Exception:
            self._send(400, '{"ok":false,"error":"bad-request"}', "application/json")
            return
        # Respond first; disk writes and logging must never turn a stored
        # signature into an error for the phone.
        self._send(200 if ok else 400,
                   json.dumps({"ok": ok} if ok else {"ok": False, "error": reason}),
                   "application/json")
        if ok:
            saved = save_signature_png(png, sid)
            try:
                note = f" -> {saved}" if saved else ""
                print(f"  [OK] Signature received (session {sid}, {length/1024:.0f} KB){note}")
            except Exception:
                pass

    def log_message(self, fmt, *args):   # keep the console quiet
        pass

# ----------------------------------------------------------------------------
# Startup
# ----------------------------------------------------------------------------
def find_cloudflared():
    """Locate the cloudflared binary on PATH or in its usual Windows install dirs."""
    exe = shutil.which("cloudflared")
    if exe:
        return exe
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.environ.get("LOCALAPPDATA")):
        if not base:
            continue
        cand = os.path.join(base, "cloudflared", "cloudflared.exe")
        if os.path.exists(cand):
            return cand
    return None


def start_tunnel(port, timeout=30):
    """Launch a cloudflared quick tunnel to localhost:<port>.

    Returns (public_url, process). Raises RuntimeError if cloudflared is missing
    or no URL appears within `timeout` seconds. The process is left running and
    should be terminated on shutdown.
    """
    exe = find_cloudflared()
    if not exe:
        raise RuntimeError(
            "cloudflared is not installed. Install it (winget install --id "
            "Cloudflare.cloudflared) or run without --tunnel.")

    proc = subprocess.Popen(
        [exe, "tunnel", "--url", f"http://localhost:{port}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    url_re = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
    result = {"url": None}

    def reader():
        for line in proc.stdout:                     # blocks until the pipe closes
            if result["url"] is None:
                m = url_re.search(line)
                if m:
                    result["url"] = m.group(0)

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    deadline = time.time() + timeout
    while result["url"] is None and time.time() < deadline:
        if proc.poll() is not None:                  # cloudflared exited early
            raise RuntimeError("cloudflared exited before providing a tunnel URL.")
        time.sleep(0.3)

    if result["url"] is None:
        proc.terminate()
        raise RuntimeError(f"cloudflared did not produce a tunnel URL within {timeout}s.")

    return result["url"], proc


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))       # no packets sent; just picks the route
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()

def candidate_ips():
    """All non-loopback IPv4 addresses on this machine (Wi-Fi, Ethernet, VPN, WSL...)."""
    ips = set()
    ips.add(lan_ip())
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))

def main():
    global BASE_URL
    # Make console output safe on any locale (Windows consoles often use cp1252,
    # which can't encode characters like ✓ or — and would otherwise crash prints).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Sign on your phone, receive on your computer.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--ip", default=None,
                    help="Force the IP used in the QR (use your Wi-Fi adapter's IPv4 if "
                         "auto-detection picks a VPN/WSL/virtual adapter)")
    ap.add_argument("--public-url", default=None,
                    help="Tunnel URL (e.g. from cloudflared) to use in the QR instead of the LAN address")
    ap.add_argument("--tunnel", action="store_true",
                    help="Auto-start a cloudflared tunnel so the phone reaches this PC over the "
                         "internet. Use this if the phone can't open the QR link on the LAN "
                         "(e.g. the router blocks device-to-device / 'client isolation').")
    ap.add_argument("--no-pin", action="store_true",
                    help="Don't require the phone to enter the PIN shown on the computer "
                         "(the PIN stops strangers using a public tunnel URL).")
    ap.add_argument("--save-dir", default="signatures",
                    help="Folder to auto-save each received signature PNG into "
                         "(default: ./signatures). Use '' to disable auto-saving.")
    ap.add_argument("--no-open", action="store_true", help="Don't auto-open the browser")
    args = ap.parse_args()

    global REQUIRE_PIN, SAVE_DIR
    REQUIRE_PIN = not args.no_pin
    SAVE_DIR = os.path.abspath(args.save_dir) if args.save_dir else None

    # Start the HTTP server first so the tunnel has something to point at.
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    ip = args.ip or lan_ip()
    tunnel_proc = None
    if args.tunnel and not args.public_url:
        print("  Starting cloudflared tunnel… (a few seconds)")
        try:
            public_url, tunnel_proc = start_tunnel(args.port)
            args.public_url = public_url
        except RuntimeError as e:
            print(f"  [!] Tunnel failed: {e}")
            print("      Falling back to the LAN address.")

    BASE_URL = (args.public_url.rstrip("/") if args.public_url
                else f"http://{ip}:{args.port}")

    local_url = f"http://127.0.0.1:{args.port}"
    others = [c for c in candidate_ips() if c != ip]
    print()
    print("  Signature Bridge")
    print("  " + "-" * 46)
    print(f"  Open on this computer : {local_url}")
    print(f"  QR will point phone to: {BASE_URL}/sign?s=<session>")
    if args.public_url:
        print("  Tunnel active — the phone reaches this PC over the internet.")
        print("  (Works even if your Wi-Fi blocks device-to-device connections.)")
    elif ip == "127.0.0.1":
        print("  [!] Could not detect a LAN address — your phone won't reach 127.0.0.1.")
        print("      Fix: connect to Wi-Fi, or use a tunnel:")
        print("        python3 signature_bridge.py --tunnel")
    else:
        print("  Phone must be on the same Wi-Fi.")
        print("  If the phone can't open the link, your router likely blocks")
        print("  device-to-device connections — use a tunnel instead:")
        print("        python3 signature_bridge.py --tunnel")
    if others and not args.public_url:
        print(f"  Other addresses on this machine: {', '.join(others)}")
        print("      If the phone can't connect, one of these may be the real Wi-Fi IP:")
        print("        python3 signature_bridge.py --ip <address>")
    if REQUIRE_PIN:
        print("  PIN lock ON — the computer page shows a 4-digit code the phone must enter.")
        print("               (disable with --no-pin)")
    else:
        print("  PIN lock OFF — anyone with the link can sign (--no-pin).")
    if SAVE_DIR:
        print(f"  Auto-saving signatures to: {SAVE_DIR}")
    else:
        print("  Auto-save OFF — save from the computer page instead.")
    print("  Quick test: browse to the QR link's base URL directly on the phone.")
    print("  Stop with Ctrl+C")
    print()

    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(local_url)).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        srv.shutdown()
        srv.server_close()
        if tunnel_proc is not None:
            tunnel_proc.terminate()

if __name__ == "__main__":
    main()
