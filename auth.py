"""auth.py — Google sign-in restricted to an email allowlist.

Adapted from example-service/auth.py, minus the incremental Google Calendar
grant. Login is identity-only ("openid email profile"), so Google never shows
a sensitive-scope consent screen.

All allowlisted users share one single-tenant database. User rows identify
the currently signed-in user.
"""
from __future__ import annotations

import os
import secrets
from functools import wraps
from urllib.parse import urlencode

import requests
from flask import (Blueprint, current_app, g, redirect, render_template,
                   request, session, url_for)

import db

auth_bp = Blueprint("auth", __name__)

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO = "https://www.googleapis.com/oauth2/v2/userinfo"
SCOPES = "openid email profile"


def _conn():
    return current_app.config["GET_CONN"]()


def current_user():
    """The signed-in user's row (cached per request), or None."""
    uid = session.get("user_id")
    if uid is None:
        return None
    if not hasattr(g, "_cached_user"):
        g._cached_user = db.get_user(_conn(), uid)
    return g._cached_user


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if session.get("user_id") is None:
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def _safe_next(v: str | None) -> str | None:
    """Only a same-site relative path may ride through the login flow --
    anything else is an open-redirect vector."""
    if v and v.startswith("/") and not v.startswith("//"):
        return v
    return None


def allowed_emails() -> list[str]:
    raw = current_app.config.get("ALLOWED_EMAILS", "")
    return [e.strip().lower() for e in raw.split(",") if e.strip()]


def _allowed(email: str) -> bool:
    """An empty allowlist denies everyone. This app is single-owner, so
    'unconfigured' must fail closed, not open."""
    allow = allowed_emails()
    return bool(allow) and email.strip().lower() in allow


def _local_login_enabled() -> bool:
    """Dev-only door, used while OAuth credentials do not exist yet. Refuses
    to work the moment a real client id is configured, so it cannot linger
    on the deployed box."""
    return (os.environ.get("ACADTRACK_LOCAL_LOGIN") == "1"
            and not current_app.config.get("GOOGLE_CLIENT_ID"))


@auth_bp.route("/login")
def login():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return render_template(
        "login.html",
        error=request.args.get("error"),
        next=_safe_next(request.args.get("next")) or "",
        configured=bool(current_app.config.get("GOOGLE_CLIENT_ID")),
        local_login=_local_login_enabled())


@auth_bp.route("/auth/google")
def google_start():
    # `state` ties the callback to this browser session (blocks login CSRF).
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    nxt = _safe_next(request.args.get("next"))
    if nxt:
        session["after_login"] = nxt
    params = {
        "client_id": current_app.config.get("GOOGLE_CLIENT_ID", ""),
        "redirect_uri": current_app.config.get("GOOGLE_REDIRECT_URI", ""),
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        # The owner has two accounts on this allowlist; without this Google
        # silently reuses whichever one it saw last.
        "prompt": "select_account",
    }
    return redirect(GOOGLE_AUTH + "?" + urlencode(params))


@auth_bp.route("/auth/local", methods=["POST"])
def local_login():
    if not _local_login_enabled():
        return redirect(url_for("auth.login", error="Local sign-in is disabled"))
    allow = allowed_emails()
    email = allow[0] if allow else "local@localhost"
    user = db.get_or_create_user(_conn(), email, "Local dev", "")
    nxt = _safe_next(request.form.get("next"))
    session.clear()
    session["user_id"] = user["id"]
    session.permanent = True
    return redirect(nxt or url_for("dashboard"))


@auth_bp.route("/auth/callback")
def google_callback():
    expected_state = session.pop("oauth_state", None)
    if not expected_state or request.args.get("state") != expected_state:
        return redirect(url_for("auth.login", error="Sign-in expired, try again"))
    code = request.args.get("code")
    if not code:
        return redirect(url_for("auth.login", error="Authentication failed"))
    try:
        token = requests.post(GOOGLE_TOKEN, data={
            "code": code,
            "client_id": current_app.config.get("GOOGLE_CLIENT_ID", ""),
            "client_secret": current_app.config.get("GOOGLE_CLIENT_SECRET", ""),
            "redirect_uri": current_app.config.get("GOOGLE_REDIRECT_URI", ""),
            "grant_type": "authorization_code",
        }, timeout=15).json()
        access_token = token.get("access_token")
        if not access_token:
            return redirect(url_for("auth.login", error="Could not sign in"))
        info = requests.get(GOOGLE_USERINFO, headers={
            "Authorization": f"Bearer {access_token}"}, timeout=15).json()
    except requests.RequestException:
        return redirect(url_for("auth.login", error="Network error, try again"))

    email = info.get("email", "")
    if not email:
        return redirect(url_for("auth.login", error="No email from Google"))
    if not _allowed(email):
        return redirect(url_for("auth.login", error=f"{email} is not allowed here"))

    user = db.get_or_create_user(_conn(), email, info.get("name", ""),
                                 info.get("picture", ""))
    nxt = _safe_next(session.get("after_login"))
    session.clear()
    session["user_id"] = user["id"]
    session.permanent = True
    return redirect(nxt or url_for("dashboard"))


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
