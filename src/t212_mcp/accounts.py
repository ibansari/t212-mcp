"""Who is calling, and which Trading 212 account they use.

Over HTTP the caller is the WorkOS AuthKit user behind the MCP access token (claim `sub`). Their email, looked up
once through the WorkOS API, decides whether they are the owner (T212_OWNER_EMAIL) or an admin (T212_ADMIN_EMAILS).
In stdio, CLI and scheduled jobs the caller is the owner. The owner's rows use the id "owner" and the key in
settings; everyone else connects their own key on the account page, and it is stored in WorkOS Vault.
"""

import json
import time
from dataclasses import dataclass
from functools import cache

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from pydantic import ValidationError
from sqlalchemy import delete, update
from workos import AsyncWorkOSClient
from workos._errors import ConflictError, NotFoundError

from . import db
from .client import Credentials, T212Client
from .config import Settings
from .db import models as m

OWNER_ID = "owner"
CREDENTIALS_CACHE_S = 300


@dataclass(frozen=True)
class User:
    id: str  # WorkOS user id, or "owner"
    email: str | None
    is_owner: bool
    is_admin: bool


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        try:
            _settings = Settings()
        except ValidationError as e:
            raise ToolError(f"Invalid server configuration: {e.errors()[0]['msg']}") from e
    return _settings


@cache
def _workos(api_key: str, client_id: str | None) -> AsyncWorkOSClient:
    return AsyncWorkOSClient(api_key=api_key, client_id=client_id)


def workos(settings: Settings) -> AsyncWorkOSClient:
    if settings.workos_api_key is None:
        raise ToolError("The server is missing T212_WORKOS_API_KEY.")
    return _workos(settings.workos_api_key.get_secret_value(), settings.workos_client_id)


def make_user(settings: Settings, workos_id: str, email: str | None) -> User:
    address = (email or "").lower()
    is_owner = bool(settings.owner_email) and address == settings.owner_email.lower()
    return User(OWNER_ID if is_owner else workos_id, email, is_owner,
                is_owner or (bool(address) and address in settings.admin_email_set))


def owner(settings: Settings) -> User:
    return User(OWNER_ID, settings.owner_email, True, True)


_emails: dict[str, str | None] = {}  # WorkOS user id -> email, for this process


async def email_for(settings: Settings, workos_id: str) -> str | None:
    if workos_id not in _emails:
        user = await workos(settings).user_management.get_user(workos_id)
        _emails[workos_id] = user.email
    return _emails[workos_id]


_last_seen: dict[str, float] = {}


def touch(settings: Settings, user: User) -> None:
    """Record the user (at most every 10 minutes per process)."""
    if time.monotonic() - _last_seen.get(user.id, -1e9) < 600:
        return
    _last_seen[user.id] = time.monotonic()
    now = db.utcnow()
    with db.sessions(settings.database_url).begin() as s:
        row = s.get(m.User, user.id) or m.User(id=user.id, created_at=now)
        row.email, row.last_seen_at = user.email, now
        s.add(row)


async def current_user() -> User:
    settings = get_settings()
    token = get_access_token()
    sub = str((token.claims or {}).get("sub") or "") if token else ""
    if not sub.startswith("user_"):  # stdio, CLI, local HTTP or the static bearer token: the owner
        return owner(settings)
    user = make_user(settings, sub, await email_for(settings, sub))
    touch(settings, user)
    return user


# ---------------------------------------------------------------- Trading 212 keys in WorkOS Vault

_credentials: dict[str, tuple[float, Credentials | None]] = {}


def vault_name(user_id: str) -> str:
    return f"t212-mcp-trading212-key-{user_id}"


async def save_credentials(settings: Settings, user: User, creds: Credentials) -> None:
    vault = workos(settings).vault
    value = json.dumps({"api_key": creds.api_key, "api_secret": creds.api_secret, "env": creds.env})
    with db.sessions(settings.database_url)() as s:
        row = s.get(m.UserCredentials, user.id)
        object_id = row.vault_object_id if row else None
    if object_id:
        await vault.update_kv(object_id, value=value)
    else:
        try:
            object_id = (await vault.create_kv(name=vault_name(user.id), value=value,
                                               key_context={"user": user.id})).id
        except ConflictError:  # left over from an earlier attempt
            object_id = (await vault.get_name(vault_name(user.id))).id
            await vault.update_kv(object_id, value=value)
    now = db.utcnow()
    with db.sessions(settings.database_url).begin() as s:
        u = s.get(m.User, user.id) or m.User(id=user.id, created_at=now)
        u.email, u.last_seen_at = user.email, now
        s.add(u)
        s.flush()
        row = s.get(m.UserCredentials, user.id) or m.UserCredentials(user_id=user.id)
        row.vault_object_id, row.env, row.verified_at = object_id, creds.env, now
        s.add(row)
    _forget(user.id)


async def load_credentials(settings: Settings, user_id: str) -> Credentials | None:
    cached = _credentials.get(user_id)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    with db.sessions(settings.database_url)() as s:
        row = s.get(m.UserCredentials, user_id)
        object_id = row.vault_object_id if row else None
    creds = None
    if object_id:
        try:
            data = json.loads((await workos(settings).vault.get_kv(object_id)).value)
            creds = Credentials(data["api_key"], data.get("api_secret"), data["env"])
        except NotFoundError:
            creds = None
    _credentials[user_id] = (time.monotonic() + CREDENTIALS_CACHE_S, creds)
    return creds


def connection(settings: Settings, user_id: str) -> dict | None:
    """What the account page may show: environment and when the key was checked, never the key."""
    with db.sessions(settings.database_url)() as s:
        row = s.get(m.UserCredentials, user_id)
        return {"env": row.env, "verified_at": db.iso(row.verified_at)} if row else None


async def delete_credentials(settings: Settings, user_id: str) -> None:
    with db.sessions(settings.database_url)() as s:
        row = s.get(m.UserCredentials, user_id)
        object_id = row.vault_object_id if row else None
    if object_id:
        try:
            await workos(settings).vault.delete_kv(object_id)
        except NotFoundError:
            pass
    with db.sessions(settings.database_url).begin() as s:
        s.execute(delete(m.UserCredentials).where(m.UserCredentials.user_id == user_id))
    _forget(user_id)


async def delete_user_data(settings: Settings, user_id: str) -> None:
    """Remove a user's key and personal history. Shared fund data stays."""
    if user_id == OWNER_ID:
        raise ValueError("the owner's data can't be deleted from the account page")
    await delete_credentials(settings, user_id)
    with db.sessions(settings.database_url).begin() as s:
        for model in (m.PortfolioSnapshot, m.ExposureSnapshot, m.DigestBaseline):
            s.execute(delete(model).where(model.user_id == user_id))
        s.execute(update(m.AgentRun).where(m.AgentRun.user_id == user_id).values(user_id=None))
        s.execute(delete(m.User).where(m.User.id == user_id))


def _forget(user_id: str) -> None:
    _credentials.pop(user_id, None)
    _clients.pop(user_id, None)


# ---------------------------------------------------------------- per-user clients

_clients: dict[str, tuple[Credentials, T212Client]] = {}


async def client_for(user: User) -> T212Client | None:
    """The user's Trading 212 client: the owner uses the key in settings, everyone else their Vault key."""
    settings = get_settings()
    creds = Credentials.from_settings(settings) if user.is_owner and settings.api_key else None
    creds = creds or await load_credentials(settings, user.id)
    if creds is None:
        return None
    cached = _clients.get(user.id)
    if cached and cached[0] == creds:
        return cached[1]
    c = T212Client(settings, creds=creds)
    _clients[user.id] = (creds, c)
    return c


def account_url(settings: Settings) -> str:
    return f"{settings.public_base_url or ''}/account"


async def require_client(user: User | None = None) -> T212Client:
    c = await client_for(user or await current_user())
    if c is None:
        raise ToolError(f"No Trading 212 account is connected for you yet. Connect a read-only API key at "
                        f"{account_url(get_settings())}")
    return c
