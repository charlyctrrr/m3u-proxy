import os, re, time, threading, asyncio
from urllib.parse import urlparse, parse_qs, urljoin, quote
from datetime import datetime, timezone

import requests
from flask import Flask, Response, request
from playwright.async_api import async_playwright

# ====== CONFIG ======
START_URL = os.getenv("START_URL", "https://www.tvplusgratis2.com/espn-en-vivo.html")
EXPIRY_GUARD_S = int(os.getenv("EXPIRY_GUARD_S", "45"))
PRINT_HEARTBEAT = os.getenv("PRINT_HEARTBEAT", "1") == "1"
# ====================

state = {"m3u8": None, "headers": {}, "expires": None, "last_seen": 0.0}
M3U8_RE = re.compile(r"https?://[^\s'\"<>]+?\.m3u8(?:\?[^\s'\"<>]*)?", re.IGNORECASE)

def _origin_from(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.hostname}/"

def _parse_expires(url: str):
    try:
        q = parse_qs(urlparse(url).query)
        if "expires" in q:
            return int(q["expires"][0])
    except Exception:
        pass
    return None

def _normalize_headers(h: dict, m3u8_url: str) -> dict:
    out = {k: v for k, v in h.items()}
    o = _origin_from(m3u8_url)
    out["Referer"] = o
    out["Origin"]  = o.rstrip("/")
    out["User-Agent"] = out.get("User-Agent") or out.get("user-agent") or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    out.setdefault("Accept", "*/*")
    out.setdefault("Accept-Language", "es-ES,es;q=0.9,en;q=0.8")
    return out

async def _run_sniffer():
    """Abre la página, detecta .m3u8 y renueva cuando el token está por expirar."""
    while True:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                context = await browser.new_context(ignore_https_errors=True)
                page = await context.new_page()

                def _maybe_update(url: str, headers: dict):
                    m = M3U8_RE.search(url)
                    if not m:
                        return
                    u = m.group(0)
                    exp = _parse_expires(u)
                    hdrs = _normalize_headers(headers or {}, u)
                    changed = (state["m3u8"] != u) or (state["expires"] != exp)
                    if changed:
                        state["m3u8"] = u
                        state["headers"] = hdrs
                        state["expires"] = exp
                        state["last_seen"] = time.time()
                        print("🎯 Nuevo M3U8:", u)
                        if exp:
                            print("   Expira (UTC):", datetime.fromtimestamp(exp, tz=timezone.utc))

                page.on("request", lambda req: _maybe_update(req.url, req.headers))
                page.on("response", lambda res: _maybe_update(res.url, {}))

                try:
                    await page.goto(START_URL, wait_until="networkidle", timeout=45000)
                except Exception:
                    # Algunas páginas no llegan a "networkidle"; continuar
                    pass

                # Intento de "play"
                try:
                    for sel in ["button[aria-label='Play']", ".jw-icon-play", ".vjs-big-play-button", "button.play"]:
                        if await page.locator(sel).count():
                            await page.locator(sel).first.click(timeout=2000)
                            break
                except Exception:
                    pass

                while True:
                    await page.wait_for_timeout(1000)
                    if state["m3u8"] and state["expires"]:
                        now = int(time.time())
                        if now >= (state["expires"] - EXPIRY_GUARD_S):
                            try:
                                await page.reload(wait_until="domcontentloaded", timeout=15000)
                            except Exception:
                                pass
                    elif PRINT_HEARTBEAT and (time.time() - state["last_seen"] > 5):
                        print("⏳ Esperando .m3u8…")
                        state["last_seen"] = time.time()
        except Exception as e:
            print("⚠️ Sniffer reinicia por error:", repr(e))
            await asyncio.sleep(3)

def _rewrite_playlist_to_proxy(text: str, base_upstream: str, public_base: str) -> str:
    lines = text.splitlines()
    out = []
    for line in lines:
        if not line or line.startswith("#"):
            out.append(line)
            continue
        abs_url = line if line.startswith(("http://", "https://")) else urljoin(base_upstream, line)
        prox = f"{public_base}hls?u={quote(abs_url, safe='')}"
        out.append(prox)
    return "\\n".join(out) + ("\\n" if not out or out[-1] != "" else "")

# ------------ Flask ------------
app = Flask(__name__)

@app.get("/health")
def health():
    return {
        "ok": True,
        "haveUpstream": bool(state["m3u8"]),
        "expires": state["expires"],
        "m3u8": state["m3u8"],
    }

@app.get("/espn.m3u8")
def espn_playlist():
    if not state["m3u8"]:
        return "# Esperando m3u8...", 503
    try:
        r = requests.get(state["m3u8"], headers=state["headers"], timeout=15)
        r.raise_for_status()
        body = r.text
        base = request.url_root  # p. ej. https://m3u-proxy.onrender.com/
        rewritten = _rewrite_playlist_to_proxy(body, state["m3u8"], base)
        return Response(rewritten, content_type="application/vnd.apple.mpegurl")
    except Exception as e:
        return Response(f"# Error upstream: {e}\\n", status=502, mimetype="text/plain")

@app.get("/hls")
def hls_proxy():
    u = request.args.get("u")
    if not u:
        return "Falta parámetro u", 400
    try:
        rr = requests.get(u, headers=state["headers"], stream=True, timeout=15)
        ct = rr.headers.get("content-type", "")
        # Si es otra playlist, reescribir recursivamente
        if "mpegurl" in ct or u.endswith(".m3u8"):
            txt = rr.text
            base = request.url_root
            rewritten = _rewrite_playlist_to_proxy(txt, u, base)
            return Response(rewritten, content_type="application/vnd.apple.mpegurl")
        # Segmento binario
        return Response(rr.iter_content(8192), content_type=ct or "video/MP2T")
    except Exception as e:
        return Response(f"Upstream error: {e}", status=502, mimetype="text/plain")

# Arranca el sniffer en segundo plano
def _start_background_sniffer():
    loop = asyncio.new_event_loop()
    threading.Thread(
        target=lambda: (asyncio.set_event_loop(loop), loop.run_until_complete(_run_sniffer())),
        daemon=True
    ).start()

_start_background_sniffer()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
