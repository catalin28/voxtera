# Client Deployment Guide

Step-by-step instructions to deploy Voxtera for a new hotel client.

---

## Prerequisites

- SSH access to a DigitalOcean droplet (Ubuntu 22.04+, 2GB RAM minimum)
- SSH alias configured in `~/.ssh/config` for the target host
- The Voxtera repo cloned locally with a working `.venv`
- API keys: OpenAI, Anthropic, Google Cloud (TTS/STT), Daily.co, Telegram bot token

---

## Step 1: Create the Hotel Configuration

Create a YAML config file at `config/hotels/<hotel_id>.yaml`:

```yaml
hotel_name: "Hotel Riviera"
official_language: "en"
telegram_channel_id: "-100XXXXXXXXXX"

allowed_categories:
  - Maintenance
  - Reservation
  - Concierge
  - Restaurant
  - Housekeeping
  - Lost & Found
  - Complaint
  - Emergency
  - Feedback
  - Other

system_prompt_addendum: |
  You are deployed at Hotel Riviera, a beachfront resort in Nice.
  The hotel has 120 rooms, a rooftop bar, infinity pool, and spa.
  Staff speaks English and French. File all tickets in English.
```

Replace `<hotel_id>` with a short slug (e.g. `riviera`, `marina`, `alps`).

---

## Step 2: Prepare Knowledge Base Content

Create markdown files with the hotel's information:

```
demo-hotel/            ← or a dedicated folder per client
├── menu.md            ← restaurant menu, hours, special dishes
├── spa.md             ← spa services, prices, hours
├── policies.md        ← checkout, cancellation, pets, noise
├── welcome-guide.md   ← wifi, room controls, TV, minibar
├── troubleshooting.md ← common issues (TV remote, AC, etc.)
└── room-service-ordering.md
```

These files are chunked, embedded, and stored in the RAG database.
The bot uses them to answer guest questions with hotel-specific knowledge.

---

## Step 3: Create the `.env` File

Copy `.env.example` and fill in the client's values:

```env
HOTEL_ID=riviera
INPUT_MODE=hybrid           # voice + text (or "text" for chat-only)
TRANSPORT_MODE=daily        # "daily" for WebRTC, "local" for dev

# LLM
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...

# Speech
GOOGLE_APPLICATION_CREDENTIALS=.secrets/google-service-account.json
GOOGLE_TTS_ENABLED=true
DAILY_API_KEY=...

# Notifications
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHANNEL_ID=-100XXXXXXXXXX

# Embedding sidecar (optional, speeds up cold starts)
VOXTERA_EMBEDDING_URL=http://127.0.0.1:9400
```

---

## Step 4: Provision the Droplet

1. Create a droplet on DigitalOcean:
   - **Image**: Ubuntu 22.04 LTS
   - **Size**: 2GB RAM / 1 vCPU ($12/mo) minimum
   - **Region**: closest to the hotel (e.g. `fra1` for Europe)
   - **SSH key**: your deploy key

2. Add an SSH alias in `~/.ssh/config`:
   ```
   Host voxtera-riviera
     HostName 167.99.x.x
     User root
     IdentityFile ~/.ssh/id_ed25519
   ```

3. Initial server setup (run once):
   ```bash
   ssh voxtera-riviera
   adduser voxtera --disabled-password
   mkdir -p /opt/voxtera/app /etc/voxtera
   chown voxtera:voxtera /opt/voxtera/app
   apt update && apt install -y python3.11 python3.11-venv rsync
   # Install uv
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

---

## Step 5: Deploy with the Deploy Script

From your local machine:

```bash
scripts/deploy-droplet.sh \
  --host voxtera-riviera \
  --hotel-id riviera \
  --content-dir /opt/voxtera/app/demo-hotel
```

This will:
1. Rsync project files to the droplet
2. Deploy the `.env` to `/etc/voxtera/voxtera.env`
3. Install/update Python dependencies via `uv sync`
4. Run RAG ingest (chunks + embeds the hotel's markdown docs)
5. Restart the systemd services

---

## Step 6: Set Up DNS

Point a subdomain to the droplet IP:

```
riviera.voxtera.io  →  A  →  167.99.x.x
```

SSL is handled via sslip.io for quick testing, or set up Let's Encrypt / Cloudflare for production.

---

## Step 7: Verify

```bash
# Health check
curl https://riviera.voxtera.io/health

# Test chat
curl -X POST https://riviera.voxtera.io/api/chat \
  -H "Content-Type: application/json" \
  -d '{"text": "What time is breakfast?", "language": "en"}'
```

---

## Step 8: Give the Client Their Access

- **Full demo page**: `https://riviera.voxtera.io/demo.html`
- **Widget embed code**: see "Chat Widget Only" section below
- **Access codes**: add to `demo-hotel/demo_codes.txt` on their server

---

---

# Chat Widget Only (No Full Application)

For clients who only need the floating chatbot widget embedded on their own website — no voice, no microphone, no Daily.co rooms.

## What They Get

- A floating "Need help?" pill in the bottom-right corner
- Expands into a chat panel with text input
- Multilingual: guest types in any language, bot replies in the same one
- Powered by the same RAG + LLM backend, just without voice

## Server Configuration

Set these in the `.env`:

```env
HOTEL_ID=riviera
INPUT_MODE=text              # no mic/STT processing
GOOGLE_TTS_ENABLED=false     # no spoken replies
TRANSPORT_MODE=local         # no Daily.co rooms needed
```

This keeps the deployment lean — no STT, no TTS, no WebRTC. Just the HTTP chat endpoint.

## What the Client Embeds on Their Website

Give the client this snippet to paste before `</body>` on any page:

```html
<!-- Voxtera Chat Widget -->
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500&family=Instrument+Serif:ital@0;1&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">

<div class="voxtera-widget" id="voxtera-widget">
  <div class="vx-launcher" id="vx-launcher" role="button" tabindex="0" aria-label="Open chat">
    <div class="vx-orb"></div>
    <span class="vx-launcher-label">Need help?</span>
  </div>
  <div class="vx-panel" id="vx-panel" role="dialog" aria-hidden="true">
    <div class="vx-panel-header">
      <div class="vx-brand">
        <div class="vx-brand-mark"></div>
        <div>
          <div class="vx-brand-text">Voxtera</div>
          <div class="vx-brand-status">Online · 99 languages</div>
        </div>
      </div>
      <button class="vx-close" id="vx-close" type="button" aria-label="Close">
        <svg viewBox="0 0 24 24"><path d="M6 6 L18 18 M18 6 L6 18"/></svg>
      </button>
    </div>
    <div class="vx-panel-body" id="vx-messages">
      <div class="vx-orb-wrap"><div class="vx-orb lg"></div></div>
      <div class="vx-helper">Type a message in any language —<br/>Voxtera replies in the same one.</div>
    </div>
    <div class="vx-chat-input">
      <textarea class="vx-input-field" id="vx-input" rows="1" placeholder="Send a message…"></textarea>
      <button class="vx-send-btn" id="vx-send" type="button" aria-label="Send" disabled>
        <svg viewBox="0 0 24 24"><path d="M3 12 L21 4 L13 22 L11 13 Z"/></svg>
      </button>
    </div>
  </div>
</div>

<script>
(function(){
  // ── CONFIG — set this to the client's Voxtera backend URL ──
  const VOXTERA_API = "https://riviera.voxtera.io/api/chat";
  const HOTEL_ID = "riviera";

  const launcher = document.getElementById('vx-launcher');
  const panel = document.getElementById('vx-panel');
  const closeBtn = document.getElementById('vx-close');
  const input = document.getElementById('vx-input');
  const sendBtn = document.getElementById('vx-send');
  const messages = document.getElementById('vx-messages');
  let sessionId = null;

  function open(){ panel.classList.add('open'); panel.setAttribute('aria-hidden','false'); launcher.classList.add('hidden'); setTimeout(()=>input.focus(),350); }
  function close(){ panel.classList.remove('open'); panel.setAttribute('aria-hidden','true'); launcher.classList.remove('hidden'); }

  launcher.addEventListener('click', open);
  closeBtn.addEventListener('click', close);
  document.addEventListener('keydown', e=>{ if(e.key==='Escape' && panel.classList.contains('open')) close(); });

  input.addEventListener('input', ()=>{
    input.style.height='auto';
    input.style.height=Math.min(input.scrollHeight,120)+'px';
    sendBtn.disabled=input.value.trim().length===0;
  });
  input.addEventListener('keydown', e=>{
    if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); if(!sendBtn.disabled) sendBtn.click(); }
  });

  function addBubble(role, text){
    // Clear the orb placeholder on first message
    const orb = messages.querySelector('.vx-orb-wrap');
    const helper = messages.querySelector('.vx-helper');
    if(orb) orb.remove();
    if(helper) helper.remove();
    messages.style.cssText='flex:1;overflow-y:auto;padding:1rem;display:flex;flex-direction:column;gap:0.6rem;';
    const div = document.createElement('div');
    div.style.cssText = role==='user'
      ? 'align-self:flex-end;background:#f0ebe3;padding:0.6rem 0.9rem;border-radius:14px;max-width:80%;font-size:0.9rem;'
      : 'align-self:flex-start;background:#e8f0f8;padding:0.6rem 0.9rem;border-radius:14px;max-width:80%;font-size:0.9rem;';
    div.textContent = text;
    messages.appendChild(div);
    messages.scrollTop = messages.scrollHeight;
    return div;
  }

  sendBtn.addEventListener('click', async ()=>{
    const text = input.value.trim();
    if(!text) return;
    addBubble('user', text);
    input.value=''; input.style.height='auto'; sendBtn.disabled=true;

    const botBubble = addBubble('bot', '…');
    try {
      const resp = await fetch(VOXTERA_API, {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ text, session_id: sessionId, hotel_id: HOTEL_ID })
      });
      let fullText = '';
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      while(true){
        const {done, value} = await reader.read();
        if(done) break;
        const lines = decoder.decode(value,{stream:true}).split('\n');
        for(const line of lines){
          if(!line.trim()) continue;
          try {
            const obj = JSON.parse(line);
            if(obj.type==='text') fullText += obj.chunk;
            if(obj.type==='done'){ sessionId = obj.session_id; fullText = obj.text || fullText; }
          } catch(e){}
        }
        botBubble.textContent = fullText || '…';
      }
      botBubble.textContent = fullText || '(no response)';
    } catch(err){
      botBubble.textContent = 'Connection error. Please try again.';
    }
  });
})();
</script>

<!-- Widget CSS (include in <head> or inline) — see voxtera-widget.html for the full stylesheet -->
<link rel="stylesheet" href="https://riviera.voxtera.io/static/voxtera-widget.css">
```

## Checklist: Widget-Only Client

| # | Task | Done |
|---|------|------|
| 1 | Create `config/hotels/<hotel_id>.yaml` | ☐ |
| 2 | Write hotel knowledge markdown files | ☐ |
| 3 | Set `.env` with `INPUT_MODE=text`, `GOOGLE_TTS_ENABLED=false` | ☐ |
| 4 | Deploy to droplet with `--hotel-id <hotel_id>` | ☐ |
| 5 | Verify `/api/chat` responds correctly | ☐ |
| 6 | Set up CORS on serve.py to allow the client's domain | ☐ |
| 7 | Give client the embed snippet with their `VOXTERA_API` URL | ☐ |
| 8 | Client pastes snippet on their website | ☐ |

## CORS Note

The client's website will make cross-origin requests to your server.
Ensure `serve.py` allows their domain in the CORS headers. Currently it
sends `Access-Control-Allow-Origin: *` which works for development.
For production, restrict to the client's domain(s).

---

## Cost Summary

| Deployment type | Monthly cost | What you get |
|-----------------|-------------|--------------|
| Widget-only (text, no voice) | ~$6-12/mo (1GB droplet) | Chat endpoint + RAG |
| Full voice + chat | ~$12-24/mo (2-4GB droplet) | Voice pipeline + Daily.co + STT/TTS + chat |
| Docker multi-tenant (future) | ~$24-48/mo (one box, many hotels) | Shared infra, lower per-client cost |

API costs (OpenAI/Anthropic/Google) are additional and scale with usage.
