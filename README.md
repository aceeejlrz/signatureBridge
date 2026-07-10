# Signature Bridge

Sign on your phone, receive the signature on your computer — no app, no account, no dependencies.

Run one Python file on your computer and it shows a **QR code**. Scan it with your phone, enter a
short PIN, and a signing pad opens in the phone's browser. Draw your signature, tap **Send**, and it
appears on the computer where you can save it as a PNG or copy it as a `data:` URL. Every signature is
also auto-saved to a folder.

It's a single standard-library Python file — the only optional extra is `cloudflared`, and only if you
need the tunnel mode described below.

---

## What it does

- **Phone → computer signing.** The phone is the pen pad; the computer is where the signature lands.
- **Pick your ink.** Choose colour (black / blue / red) and thickness (thin / medium / thick), even
  mid-signature.
- **Add a printed name and date.** Optionally bake a typed name and the date under the drawn signature.
- **Zoom & pan.** Pinch to zoom in for fine detail, drag with two fingers to pan; one finger draws.
- **PIN lock.** The computer shows a 4-digit code the phone must enter before it can sign — this keeps
  strangers off the link (important when using the public tunnel below). Locks after 8 wrong tries.
- **Auto-save.** Each received signature is written to a `signatures/` folder as a timestamped PNG.
- **Save options on the computer.** Transparent PNG, white-background PNG, `.txt` (base64 `data:` URL),
  or copy the base64 to the clipboard.
- **Redo & resend.** After sending, the phone can redo and send again; the computer preview updates live.

---

## Requirements

| Need | Details |
|------|---------|
| **Python 3.8+** | Standard library only — nothing to `pip install`. |
| **A phone with a browser** | iPhone/Android; scans the QR with the camera. |
| **Both devices reachable** | Same Wi‑Fi *(default)*, **or** use `--tunnel` if the Wi‑Fi blocks device‑to‑device traffic. |
| **cloudflared** *(optional)* | Only for `--tunnel`. Install with `winget install --id Cloudflare.cloudflared`. |

---

## How to run

```bash
python signature_bridge.py
```

1. A page opens on your computer showing a **QR code** and a **4‑digit PIN**.
2. Scan the QR with your phone, then **enter the PIN** — the signing pad opens.
3. Pick a colour/size, sign (pinch to zoom, two fingers to pan), optionally add a name/date, tap **Send**.
4. The signature appears on the computer. Save it, or grab it from the `signatures/` folder.

### If the phone can't open the link

If the phone just spins or says *"can't connect"* even though both devices are on the same Wi‑Fi, your
**router is blocking device‑to‑device connections** ("client isolation" — common on guest and public
Wi‑Fi). Route around it with the built‑in tunnel, which connects the phone to your computer over the
internet instead:

```bash
python signature_bridge.py --tunnel
```

(needs `cloudflared` — see Requirements.)

---

## Options

| Flag | Meaning |
|------|---------|
| `--port 8765` | Change the port. |
| `--tunnel` | Auto-start a `cloudflared` tunnel so the phone reaches your PC over the internet (bypasses router isolation). |
| `--public-url URL` | Use a tunnel URL you started yourself instead of `--tunnel`. |
| `--ip ADDRESS` | Force the IP put in the QR (use your Wi‑Fi adapter's IPv4 if auto‑detection picks a VPN/WSL/virtual adapter). |
| `--no-pin` | Don't require the phone to enter the PIN (the PIN keeps strangers off a public tunnel URL; on by default). |
| `--save-dir DIR` | Folder to auto-save signatures into (default: `./signatures`; use `''` to disable). |
| `--no-open` | Don't auto-open the browser on the computer. |

---

## Where signatures go

- **On the computer page:** save as transparent PNG, white PNG, `.txt`, or copy the base64 `data:` URL.
- **Auto-saved:** every received signature is written to `signatures/signature-<timestamp>-<id>.png`.
  Each **Send** (including each *Redo & resend*) writes a **new** file — an audit trail, so you'll see
  one file per send rather than one per session.

> The `signatures/` folder is git-ignored so your real signatures are never committed or published.

---

## Privacy & security notes

- **PIN lock** stops anyone who happens to have the link from injecting a signature into your session.
- **`--tunnel` sends the signature over the internet** through Cloudflare's tunnel to reach your PC. It
  isn't stored by the tunnel, but it does leave your local network. On a normal LAN (no `--tunnel`) the
  signature never leaves your Wi‑Fi.
- Sessions are kept in memory only and expire after an hour.

---

## Notes

- Works on Windows, macOS, and Linux. Console output is UTF‑8‑safe, including on Windows consoles.
- No frameworks, no build step, no accounts — just `python signature_bridge.py`.
