"""The account page (/account): sign in with WorkOS AuthKit and connect your Trading 212 key.

Sign-in, the session cookie (sealed by the WorkOS SDK, refreshed when its access token expires) and key storage
(WorkOS Vault, via accounts.py) are all WorkOS's. This module is the one form on top. Forms are same-origin only
(SameSite=Lax session cookie plus an Origin check).
"""

import html
import secrets
import time

from fastmcp.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from workos.session import seal_session_from_auth_response

from . import accounts
from .client import Credentials, T212Client
from .config import Settings

SESSION_COOKIE = "wos_session"
STATE_COOKIE = "t212_auth_state"
CALLBACK_PATH = "/account/callback"
SESSION_MAX_AGE_S = 30 * 86400
VALIDATIONS_PER_HOUR = 5

_attempts: dict[str, list[float]] = {}


def _configured(settings: Settings) -> str | None:
    missing = [n for n, v in (("T212_AUTHKIT_DOMAIN", settings.authkit_domain),
                              ("T212_WORKOS_API_KEY", settings.workos_api_key),
                              ("T212_WORKOS_CLIENT_ID", settings.workos_client_id),
                              ("T212_WORKOS_COOKIE_PASSWORD", settings.workos_cookie_password),
                              ("T212_PUBLIC_URL", settings.public_base_url)) if not v]
    return f"The account page isn't configured yet (missing {', '.join(missing)})." if missing else None


def _secure(settings: Settings) -> bool:
    return (settings.public_base_url or "").startswith("https://")


def _set_session(settings: Settings, resp: Response, sealed: str) -> None:
    resp.set_cookie(SESSION_COOKIE, sealed, max_age=SESSION_MAX_AGE_S, httponly=True, secure=_secure(settings),
                    samesite="lax")


async def session_user(settings: Settings, request: Request) -> tuple[accounts.User | None, str | None]:
    """The signed-in user and, when the session had to be refreshed, the new sealed cookie to set."""
    sealed = request.cookies.get(SESSION_COOKIE)
    if not sealed:
        return None, None
    session = accounts.workos(settings).user_management.load_sealed_session(
        session_data=sealed, cookie_password=settings.workos_cookie_password.get_secret_value())
    auth, refreshed = session.authenticate(), None
    if not auth.authenticated:
        auth = await session.refresh()
        if not auth.authenticated:
            return None, None
        refreshed = auth.sealed_session
    user = auth.user or {}
    return accounts.make_user(settings, user["id"], user.get("email")), refreshed


def _same_origin(settings: Settings, request: Request) -> bool:
    origin = request.headers.get("origin")
    return origin is None or origin.rstrip("/") == (settings.public_base_url or "").rstrip("/")


# ---------------------------------------------------------------- page


def _when(iso: str | None) -> str:
    from datetime import datetime

    return datetime.fromisoformat(iso).strftime("%-d %b %Y, %H:%M UTC") if iso else "recently"

INK, MUTED, ACCENT, ERR = "#0b0b0b", "#52514e", "#2a78d6", "#b42318"


def _page(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Trading 212 MCP · Account</title>
<style>
body{{margin:0;background:#fff;color:{INK};font:15px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}}
main{{max-width:620px;margin:0 auto;padding:28px 16px}} h1{{font-size:24px;margin:0 0 4px}} h2{{font-size:17px;margin:28px 0 8px}}
.muted{{color:{MUTED};font-size:13px}} .card{{border:1px solid #e7e6e2;border-radius:10px;padding:16px;margin:12px 0}}
label{{display:block;font-size:13px;color:{MUTED};margin:10px 0 4px}} input,select{{width:100%;box-sizing:border-box;padding:9px 10px;
border:1px solid #cfcdc7;border-radius:8px;font:inherit}} button,.btn{{display:inline-block;margin-top:14px;padding:9px 14px;border-radius:8px;
border:0;background:{ACCENT};color:#fff;font:inherit;cursor:pointer;text-decoration:none}} .secondary{{background:#f0efec;color:{INK}}}
.danger{{background:#fff;color:{ERR};border:1px solid {ERR}}} .err{{color:{ERR}}} .ok{{color:#0a7a0a}}
code{{background:#f5f4f1;padding:2px 5px;border-radius:5px;word-break:break-all}}
</style></head><body><main>{body}</main></body></html>""", status_code=status)


def render(settings: Settings, user: accounts.User | None, message: str = "", error: str = "") -> HTMLResponse:
    e = html.escape
    intro = ("<h1>Trading 212 MCP</h1><p class='muted'>Ask Claude about your own Trading 212 portfolio: holdings, P&amp;L, "
             "dividends, and what you really own across your ETFs. Read-only: it can never place orders.</p>")
    notice = (f"<p class='ok'>{e(message)}</p>" if message else "") + (f"<p class='err'>{e(error)}</p>" if error else "")
    if problem := _configured(settings):
        return _page(intro + f"<p class='err'>{e(problem)}</p>", 503)
    if user is None:
        return _page(intro + notice + "<div class='card'><p>Sign in to connect your Trading 212 account.</p>"
                     "<a class='btn' href='/account/login'>Sign in</a></div>")
    conn = accounts.connection(settings, user.id)
    owner_key = user.is_owner and settings.api_key is not None
    if owner_key:
        status = "<p class='ok'>Connected (server owner, using the server's key).</p>"
    elif conn:
        status = (f"<p class='ok'>Connected: {e(conn['env'])} account, key checked {e(_when(conn['verified_at']))}.</p>"
                  "<form method='post' action='/account/disconnect'><button class='secondary'>Disconnect</button></form>")
    else:
        status = "<p>No Trading 212 account connected yet.</p>"
    form = "" if owner_key else f"""<h2>{'Replace your' if conn else 'Connect your'} Trading 212 key</h2>
<p class='muted'>In Trading 212: <b>Settings → API (Beta) → Generate API key</b>. Grant <b>read scopes only</b>
(account data, portfolio, history, metadata) and no order permissions. The key is checked, stored encrypted in WorkOS Vault,
and never shown again.</p>
<form method='post' action='/account/credentials'>
<label for='k'>API key</label><input id='k' name='api_key' autocomplete='off' required>
<label for='s'>API secret (if your key has one)</label><input id='s' name='api_secret' type='password' autocomplete='off'>
<label for='env'>Account</label><select id='env' name='env'><option value='live'>Live (real money)</option><option value='demo'>Demo</option></select>
<button>Check and save</button></form>"""
    url = f"{settings.public_base_url}/mcp"
    howto = f"""<h2>Use it in Claude</h2><ol>
<li><b>Claude Desktop / claude.ai:</b> Settings → Connectors → Add custom connector → URL <code>{e(url)}</code>, then Connect and sign in.</li>
<li><b>Claude Code:</b> <code>claude mcp add --transport http trading212 {e(url)}</code>, then <code>/mcp</code> → Authenticate.</li></ol>
<p class='muted'>Then ask things like "how's my portfolio doing?" or "what do I really own across my ETFs?".</p>"""
    danger = "" if user.is_owner else """<h2>Delete my data</h2><p class='muted'>Removes your key and your personal history
(snapshots, look-through history). Shared fund data stays.</p>
<form method='post' action='/account/delete' onsubmit="return confirm('Delete your key and history?')">
<button class='danger'>Delete my data</button></form>"""
    who = (f"<form method='post' action='/account/logout' class='muted' style='margin:0 0 8px'>Signed in as "
           f"<b>{e(user.email or user.id)}</b> · <button class='secondary' style='margin:0;padding:3px 8px'>Sign out</button></form>")
    return _page(intro + who + notice + f"<div class='card'>{status}{form}</div>" + howto + danger)


# ---------------------------------------------------------------- routes


async def account(request: Request) -> Response:
    settings = accounts.get_settings()
    user, refreshed = (await session_user(settings, request)) if not _configured(settings) else (None, None)
    resp = render(settings, user, request.query_params.get("message", ""), request.query_params.get("error", ""))
    if refreshed:
        _set_session(settings, resp, refreshed)
    return resp


async def login(request: Request) -> Response:
    settings = accounts.get_settings()
    if problem := _configured(settings):
        return render(settings, None, error=problem)
    state = secrets.token_urlsafe(24)
    url = accounts.workos(settings).user_management.get_authorization_url(
        provider="authkit", redirect_uri=f"{settings.public_base_url}{CALLBACK_PATH}",
        client_id=settings.workos_client_id, state=state)
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True, secure=_secure(settings), samesite="lax")
    return resp


async def callback(request: Request) -> Response:
    settings = accounts.get_settings()
    state, code = request.query_params.get("state", ""), request.query_params.get("code", "")
    if not code or not state or not secrets.compare_digest(state, request.cookies.get(STATE_COOKIE, "")):
        return render(settings, None, error="Sign-in expired or was tampered with. Please try again.")
    auth = await accounts.workos(settings).user_management.authenticate_with_code(code=code)
    sealed = seal_session_from_auth_response(
        access_token=auth.access_token, refresh_token=auth.refresh_token, user=auth.user.to_dict(),
        cookie_password=settings.workos_cookie_password.get_secret_value())
    accounts.touch(settings, accounts.make_user(settings, auth.user.id, auth.user.email))
    resp = RedirectResponse("/account", status_code=303)
    _set_session(settings, resp, sealed)
    resp.delete_cookie(STATE_COOKIE)
    return resp


async def _signed_in_post(request: Request) -> tuple[Settings, accounts.User, dict, str | None] | Response:
    settings = accounts.get_settings()
    if not _same_origin(settings, request):
        return render(settings, None, error="That request came from another site and was ignored.")
    user, refreshed = await session_user(settings, request)
    if user is None:
        return RedirectResponse("/account?error=Please+sign+in+again.", status_code=303)
    return settings, user, dict(await request.form()), refreshed


def _done(settings: Settings, location: str, refreshed: str | None) -> Response:
    resp = RedirectResponse(location, status_code=303)
    if refreshed:
        _set_session(settings, resp, refreshed)
    return resp


async def save_key(request: Request) -> Response:
    checked = await _signed_in_post(request)
    if isinstance(checked, Response):
        return checked
    settings, user, form, refreshed = checked
    if user.is_owner and settings.api_key is not None:
        return render(settings, user, error="The server owner uses the server's own key.")
    now = time.time()
    recent = [t for t in _attempts.get(user.id, []) if now - t < 3600]
    if len(recent) >= VALIDATIONS_PER_HOUR:
        return render(settings, user, error="Too many attempts. Please wait an hour and try again.")
    _attempts[user.id] = recent + [now]
    key, secret, env = str(form.get("api_key", "")).strip(), str(form.get("api_secret", "")).strip(), str(form.get("env", "live"))
    if not key or env not in ("live", "demo"):
        return render(settings, user, error="Enter your API key and choose live or demo.")
    creds = Credentials(key, secret or None, env)
    client = T212Client(settings, creds=creds)
    try:
        await client.get("/equity/account/summary")
    except ToolError as e:
        return render(settings, user, error=f"Trading 212 didn't accept that key: {e}")
    finally:
        await client.aclose()
    await accounts.save_credentials(settings, user, creds)
    return _done(settings, "/account?message=Connected.+You+can+now+use+the+connector+in+Claude.", refreshed)


async def disconnect(request: Request) -> Response:
    checked = await _signed_in_post(request)
    if isinstance(checked, Response):
        return checked
    settings, user, _, refreshed = checked
    await accounts.delete_credentials(settings, user.id)
    return _done(settings, "/account?message=Disconnected.+Your+key+was+deleted.", refreshed)


async def delete_data(request: Request) -> Response:
    checked = await _signed_in_post(request)
    if isinstance(checked, Response):
        return checked
    settings, user, _, _ = checked
    if user.is_owner:
        return render(settings, user, error="The server owner's data can't be deleted here.")
    await accounts.delete_user_data(settings, user.id)
    resp = RedirectResponse("/account?message=Your+key+and+history+were+deleted.", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


async def logout(request: Request) -> Response:
    settings = accounts.get_settings()
    if not _same_origin(settings, request):
        return render(settings, None, error="That request came from another site and was ignored.")
    sealed = request.cookies.get(SESSION_COOKIE)
    location = "/account"
    if sealed:
        try:
            session = accounts.workos(settings).user_management.load_sealed_session(
                session_data=sealed, cookie_password=settings.workos_cookie_password.get_secret_value())
            location = await session.get_logout_url(return_to=f"{settings.public_base_url}/account")
        except Exception:  # an expired or unreadable session still signs out locally
            pass
    resp = RedirectResponse(location, status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


def register(mcp) -> None:
    for path, methods, handler in (("/account", ["GET"], account), ("/account/login", ["GET"], login),
                                   (CALLBACK_PATH, ["GET"], callback), ("/account/credentials", ["POST"], save_key),
                                   ("/account/disconnect", ["POST"], disconnect), ("/account/delete", ["POST"], delete_data),
                                   ("/account/logout", ["POST"], logout)):
        mcp.custom_route(path, methods=methods)(handler)
