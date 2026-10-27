"""Ephemeral QR page for the BankID step of the config flow.

The page is registered once, on an unguessable path that is only valid while a
login attempt is in progress. It serves exactly two things: a rotating BankID
QR code and a status label. No school data, token, cookie or device identifier
is ever part of the page — the path itself is the one-time capability, and it
stops working the moment the attempt ends.

The QR matrix is produced by the vendored ``qrcodegen`` module (MIT, Project
Nayuki) so the integration needs no runtime requirement.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .qrcodegen import QrCode

BASE_PATH = "/api/infomentor/bankid"
STORE_KEY = "infomentor_bankid_qr"
# 32 bytes of entropy: the path is the only credential.
TOKEN_BYTES = 32

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def encode_matrix(qr_data: str) -> list[list[bool]]:
    """Return the QR modules as rows of booleans (dark = True)."""
    code = QrCode.encode_text(qr_data, QrCode.Ecc.LOW)
    size = code.get_size()
    return [[code.get_module(x, y) for x in range(size)] for y in range(size)]


@dataclass
class QrEntry:
    """What the page may show for one attempt."""

    status: str = "starting"
    matrix: list[list[bool]] | None = None
    launch_url: str | None = None


@dataclass
class QrSession:
    """One live QR path, handed to the config flow while it is needed."""

    store: QrStore = field(repr=False)
    token: str = field(repr=False)
    url: str
    entry: QrEntry = field(default_factory=QrEntry, repr=False)

    def update(
        self, qr_data: str | None, status: str, launch_url: str | None = None
    ) -> None:
        """Publish a new QR payload and status for the page."""
        self.entry.status = status
        self.entry.launch_url = launch_url

        if qr_data is None:
            self.entry.matrix = None
            return
        try:
            self.entry.matrix = encode_matrix(qr_data)
        except ValueError:
            # A payload we cannot encode is not a reason to fail the login.
            self.entry.matrix = None

    def close(self) -> None:
        """Stop serving this path; every request is 404 from now on."""
        self.entry.matrix = None
        self.entry.launch_url = None
        self.store.drop(self.token)


class QrStore:
    """The single registered QR view plus the live paths it may serve."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.entries: dict[str, QrEntry] = {}
        hass.http.register_view(BankIdQrView(self))

    def open(self) -> QrSession:
        """Create a fresh, unguessable path and start serving it."""
        token = secrets.token_urlsafe(TOKEN_BYTES)
        entry = QrEntry()
        self.entries[token] = entry
        return QrSession(
            store=self,
            token=token,
            url=_base_url(self.hass) + f"{BASE_PATH}/{token}",
            entry=entry,
        )

    def drop(self, token: str) -> None:
        """Forget a path; the view returns 404 for it afterwards."""
        self.entries.pop(token, None)


def async_get_store(hass: HomeAssistant) -> QrStore:
    """Return the per-instance store, registering the view on first use."""
    store: QrStore | None = hass.data.get(STORE_KEY)
    if store is None:
        store = QrStore(hass)
        hass.data[STORE_KEY] = store
    return store


def _base_url(hass: HomeAssistant) -> str:
    """Return the address the phone should open.

    The external URL is preferred when one is configured: the QR page has to be
    reachable from the device that displays it, which is not always on the LAN.
    """
    try:
        return get_url(hass, prefer_external=True, allow_cloud=False).rstrip("/")
    except NoURLAvailableError:
        api = hass.config.api
        return f"http://{api.local_ip}:{api.port}"


class BankIdQrView(HomeAssistantView):
    """Serve the QR page and its polling frame on a one-time path."""

    name = "api:infomentor:bankid_qr"
    requires_auth = False
    url = BASE_PATH + "/{token}"
    extra_urls = (BASE_PATH + "/{token}/frame",)

    def __init__(self, store: QrStore) -> None:
        self._store = store

    async def get(self, request: web.Request, token: str) -> web.Response:
        """Return the page, or the current QR frame, for a live path."""
        entry = self._store.entries.get(token)
        if entry is None:
            raise web.HTTPNotFound()
        nonce = secrets.token_urlsafe(18)
        if request.path.endswith("/frame"):
            return web.json_response(
                {
                    "matrix": entry.matrix,
                    "status": entry.status,
                    "launch_url": entry.launch_url,
                },
                headers={
                    **SECURITY_HEADERS,
                    "Content-Security-Policy": "default-src 'none'",
                },
            )
        return web.Response(
            text=_page(nonce),
            content_type="text/html",
            charset="utf-8",
            headers={
                **SECURITY_HEADERS,
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; "
                    f"script-src 'nonce-{nonce}'; connect-src 'self'; "
                    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
                ),
            },
        )


def _page(nonce: str) -> str:
    """Return the whole page: a canvas, a status line and nothing else."""
    return f"""<!doctype html><html lang="sv"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>InfoMentor – logga in med BankID</title><style>
body{{font:18px/1.4 system-ui,sans-serif;text-align:center;margin:1.5rem;color:#111;background:#fff}}
h1{{font-size:1.3rem}} canvas{{width:min(88vw,420px);height:auto;image-rendering:pixelated;
background:#fff}} .btn{{display:inline-block;padding:.9rem 1.4rem;border-radius:10px;
background:#03a9f4;color:#fff;text-decoration:none;font-weight:600}} p.idle{{color:#555}}
</style>
<h1>Logga in med BankID</h1>
<p class="idle">Skanna QR-koden med BankID på en annan enhet. Sidan stängs när
inloggningen är klar.</p>
<canvas id="qr" width="600" height="600" aria-label="BankID QR-kod"></canvas>
<p id="status">Startar…</p><script nonce="{nonce}">
const labels={{starting:'Startar…',waiting:'Skanna QR-koden med BankID',
user_sign:'Godkänn i BankID',verifying:'Verifierar inloggningen…',
complete:'Klart – gå tillbaka till Home Assistant',cancelled:'Inloggningen avbröts',
timeout:'Tiden gick ut – försök igen i Home Assistant',failed:'Inloggningen misslyckades',
closed:'Sidan är stängd – gå tillbaka till Home Assistant'}};
const canvas=document.getElementById('qr'),ctx=canvas.getContext('2d'),
status=document.getElementById('status'),open=document.getElementById('open');
let link=null;
const QUIET=4;
function draw(m){{ctx.fillStyle='#fff';ctx.fillRect(0,0,600,600);
 if(!m)return; const n=m.length,s=600/(n+2*QUIET);ctx.fillStyle='#000';
 for(let y=0;y<n;y++)for(let x=0;x<n;x++)if(m[y][x])
  ctx.fillRect(Math.floor((x+QUIET)*s),Math.floor((y+QUIET)*s),Math.ceil(s),Math.ceil(s));}}
async function tick(){{try{{
 const r=await fetch('frame',{{cache:'no-store'}}); if(!r.ok)throw new Error();
 const d=await r.json(); status.textContent=labels[d.status]||'Väntar…';
 draw(d.matrix);
 
 if(!['complete','cancelled','timeout','failed'].includes(d.status))setTimeout(tick,700);
}}catch(e){{status.textContent=labels.closed;draw(null);}}}}tick();
</script></html>"""
