"""Administrator authentication and session verification."""

import hashlib
import hmac
import secrets
from threading import BoundedSemaphore

from fastapi import APIRouter, HTTPException, Request, Response
from pwdlib import PasswordHash
from pydantic import BaseModel, Field

from gpu_console import storage

COOKIE = "gpu_console_session"
SESSION_TTL = 12 * 3600
HASHER = PasswordHash.recommended()
LOGIN_SLOTS = BoundedSemaphore(2)
router = APIRouter(prefix="/api")


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def check_session(request: Request) -> dict:
    token = request.cookies.get(COOKIE)
    with storage.connect(request.app.state.config.db) as conn:
        initialized = conn.execute("SELECT 1 FROM administrators").fetchone() is not None
        row = None
        if token and len(token) <= 128:
            row = conn.execute(
                "SELECT csrf_token FROM sessions WHERE token_hash=? AND expires_at>?",
                (digest(token), storage.now()),
            ).fetchone()
    result = {"initialized": initialized, "authenticated": row is not None}
    if row is not None:
        result["csrf_token"] = row["csrf_token"]
    return result


def validate_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    config = request.app.state.config
    scheme = "https" if config.cookie_secure else request.url.scheme
    if origin and origin != f"{scheme}://{request.url.netloc}":
        raise HTTPException(403, "请求来源不匹配")


def require_session(request: Request) -> None:
    session = check_session(request)
    if not session["authenticated"]:
        raise HTTPException(401, "请登录管理台")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        validate_origin(request)
        csrf = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(csrf.encode(), session["csrf_token"].encode()):
            raise HTTPException(403, "登录校验已过期，请刷新页面")


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


@router.get("/session")
def session(request: Request) -> dict:
    return check_session(request)


@router.post("/login")
def login(body: Credentials, request: Request, response: Response) -> dict:
    validate_origin(request)
    config = request.app.state.config
    timestamp = storage.now()
    client = request.client.host if request.client else "local"
    with storage.connect(config.db) as conn, storage.transaction(conn):
        admin = conn.execute("SELECT username,password_hash FROM administrators").fetchone()
        if admin is None:
            raise HTTPException(409, "请先用 gpu-admin init 初始化管理员")
        conn.execute("DELETE FROM login_attempts WHERE window_started<=?", (timestamp - 60,))
        attempt = conn.execute("SELECT attempts FROM login_attempts WHERE client=?", (client,)).fetchone()
        if attempt and attempt["attempts"] >= 8:
            raise HTTPException(429, "登录尝试过多，请稍后再试")
        conn.execute(
            """INSERT INTO login_attempts VALUES (?, ?, 1)
               ON CONFLICT(client) DO UPDATE SET attempts=attempts+1""",
            (client, timestamp),
        )
    if not LOGIN_SLOTS.acquire(blocking=False):
        raise HTTPException(429, "登录尝试过多，请稍后再试")
    try:
        valid = HASHER.verify(body.password, admin["password_hash"])
    finally:
        LOGIN_SLOTS.release()
    if not valid or not hmac.compare_digest(body.username.encode(), admin["username"].encode()):
        raise HTTPException(401, "用户名或密码错误")
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    with storage.connect(config.db) as conn, storage.transaction(conn):
        conn.execute("DELETE FROM sessions WHERE expires_at<=?", (timestamp,))
        previous = request.cookies.get(COOKIE)
        if previous and len(previous) <= 128:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (digest(previous),))
        conn.execute("DELETE FROM login_attempts WHERE client=?", (client,))
        conn.execute("INSERT INTO sessions VALUES (?,?,?)", (digest(token), csrf, timestamp + SESSION_TTL))
    response.set_cookie(
        COOKIE, token, httponly=True, secure=config.cookie_secure, samesite="strict", max_age=SESSION_TTL
    )
    return {"initialized": True, "authenticated": True, "csrf_token": csrf}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    require_session(request)
    with storage.connect(request.app.state.config.db) as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (digest(request.cookies[COOKIE]),))
    response.delete_cookie(COOKIE)
    return {"initialized": True, "authenticated": False}
