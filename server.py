#!/usr/bin/env python3
"""Reservation board for the Robin and Yoda Concert Operate demo environments: IBM Cloud Code Engine edition.

Uses only the Python standard library.
- Settings come from the RESERVE_CONFIG_JSON environment variable (a Code Engine secret), or config.json.
- Bookings live in a local SQLite file that is restored from IBM Cloud Object Storage at startup and
  saved back shortly after every change, so data survives Code Engine scaling the app to zero.
- Code Engine terminates HTTPS, so the app itself serves plain HTTP on $PORT.
- Optional sign-in with IBMid (works with any OpenID Connect provider) via the "auth" block.
"""
import base64
import hashlib
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import signal
import socket
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, quote, urlencode, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("RESERVE_CONFIG", os.path.join(BASE_DIR, "config.json"))
DB_PATH = os.environ.get("RESERVE_DB", os.path.join(BASE_DIR, "data", "reservations.db"))
STATIC_DIR = os.path.join(BASE_DIR, "static")

__version__ = "1.0.1"
EDITION = "code-engine"

if os.environ.get("RESERVE_CONFIG_JSON"):
    try:
        CONFIG = json.loads(os.environ["RESERVE_CONFIG_JSON"])
    except ValueError as e:
        sys.exit("RESERVE_CONFIG_JSON isn't valid JSON: %s" % e)
elif os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH) as fh:
        CONFIG = json.load(fh)
else:
    sys.exit("No settings found. Set RESERVE_CONFIG_JSON, or copy config.example.json to config.json.")


def truthy(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


ENVS = {e["id"]: e for e in CONFIG["environments"]}
ADMIN_KEY = os.environ.get("RESERVE_ADMIN_KEY") or CONFIG.get("admin_key") or ""
SLOT_MIN = int(CONFIG.get("slot_minutes", 30))
MAX_HOURS = float(CONFIG.get("max_booking_hours", 8))

AUTH = CONFIG.get("auth") or {}
AUTH_ON = truthy(os.environ.get("RESERVE_AUTH", AUTH.get("enabled", False)))
OIDC_CLIENT_ID = os.environ.get("RESERVE_OIDC_CLIENT_ID") or AUTH.get("client_id", "")
OIDC_CLIENT_SECRET = os.environ.get("RESERVE_OIDC_CLIENT_SECRET") or AUTH.get("client_secret", "")
OIDC_DISCOVERY = os.environ.get("RESERVE_OIDC_DISCOVERY_URL") or AUTH.get("discovery_url", "")
OIDC_REDIRECT = os.environ.get("RESERVE_OIDC_REDIRECT_URI") or AUTH.get("redirect_uri", "")
OIDC_SCOPES = AUTH.get("scopes", "openid profile email")
OIDC_PKCE = truthy(AUTH.get("pkce", True))
OIDC_TOKEN_AUTH = AUTH.get("token_auth_method", "client_secret_basic")
PROVIDER_NAME = AUTH.get("provider_name", "IBMid")
# IBM addresses (ibm.com and any subdomain such as us.ibm.com) can always sign in.
BUILTIN_DOMAINS = ["ibm.com"]
ALLOWED_DOMAINS = list(dict.fromkeys(BUILTIN_DOMAINS + [d.lower().strip().lstrip("@") for d in AUTH.get("allowed_email_domains", []) if d.strip()]))
ALLOWED_EMAILS = set(e.lower().strip() for e in AUTH.get("allowed_emails", []) if e.strip())
ADMIN_EMAILS = set(e.lower().strip() for e in AUTH.get("admin_emails", []) if e.strip())
# Adding one of these as a partner *domain* would let anyone with a free account in. Partners on these
# providers must be added by individual email address instead.
PUBLIC_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "yahoo.com",
    "ymail.com", "icloud.com", "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com", "gmx.com",
    "gmx.de", "mail.com", "zoho.com", "yandex.com", "qq.com", "163.com", "126.com", "rediffmail.com",
}
EMAIL_RE = re.compile(r"^[^@\s]+@[a-z0-9-]+(\.[a-z0-9-]+)+$")
DOMAIN_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")
SESSION_HOURS = float(AUTH.get("session_hours", 12))
COOKIE_SECURE = truthy(AUTH.get("cookie_secure", True))
SESSION_COOKIE = "demo_reserve_session"
TX_COOKIE = "demo_reserve_oidc"

ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?Z$")
WRITE_LOCK = threading.Lock()
SESSION_KEY = b""


# ---------------------------------------------------------------- time helpers
def parse_utc(value):
    m = ISO_RE.match(value or "")
    if not m:
        raise ValueError("Times must be UTC ISO 8601, for example 2026-11-02T14:00:00Z")
    y, mo, d, h, mi, s = m.groups()
    return datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0), tzinfo=timezone.utc)


def fmt_utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- database
class TrackedConnection(sqlite3.Connection):
    """A connection that tells the storage sync when a commit actually changed data."""

    def __init__(self, *args, **kwargs):
        sqlite3.Connection.__init__(self, *args, **kwargs)
        self._seen_changes = 0

    def commit(self):
        sqlite3.Connection.commit(self)
        if self.total_changes != self._seen_changes:
            self._seen_changes = self.total_changes
            STORAGE.mark_dirty()


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10, factory=TrackedConnection)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------- Object Storage sync
class StorageError(Exception):
    pass


class CosSync:
    """Keeps data/reservations.db in an IBM Cloud Object Storage bucket.

    - restore(): at startup, download the latest copy (refuse to start if the bucket can't be read,
      so an empty database never overwrites good data).
    - mark_dirty(): called after every change. A background thread waits COS_SYNC_DELAY_SECONDS so a
      burst of changes becomes one upload (the free tier allows 2,000 uploads a month).
    - flush(): upload right away. Called when Code Engine stops the app.
    - Once a day the first upload also writes backups/reservations-YYYY-MM-DD.db.
    """

    def __init__(self):
        self.bucket = os.environ.get("COS_BUCKET", "").strip()
        self.endpoint = os.environ.get("COS_ENDPOINT", "").strip().rstrip("/")
        self.apikey = os.environ.get("COS_APIKEY", "").strip()
        self.object_key = os.environ.get("COS_OBJECT_KEY", "reservations.db").strip()
        self.iam_url = os.environ.get("IAM_TOKEN_URL", "https://iam.cloud.ibm.com/identity/token")
        self.delay = float(os.environ.get("COS_SYNC_DELAY_SECONDS", "15"))
        self.enabled = bool(self.bucket and self.endpoint and self.apikey)
        self.cond = threading.Condition()
        self.dirty_since = None
        self.upload_lock = threading.Lock()
        self.token, self.token_exp = None, 0.0
        self.last_sync, self.last_error, self.uploads = "", "", 0
        self.backup_day = ""

    # ---- HTTP helpers
    def _token(self):
        if self.token and time.time() < self.token_exp - 120:
            return self.token
        body = urlencode({"grant_type": "urn:ibm:params:oauth:grant-type:apikey", "apikey": self.apikey}).encode()
        req = urllib.request.Request(self.iam_url, data=body, headers={
            "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise StorageError("IBM Cloud IAM rejected the COS API key (HTTP %d)" % e.code)
        except Exception as e:
            raise StorageError("Couldn't reach IBM Cloud IAM: %s" % e)
        self.token = data["access_token"]
        self.token_exp = time.time() + float(data.get("expires_in", 3600))
        return self.token

    def _request(self, method, key, data=None):
        url = "%s/%s/%s" % (self.endpoint, quote(self.bucket, safe=""), quote(key))
        headers = {"Authorization": "Bearer " + self._token()}
        if data is not None:
            headers["Content-Type"] = "application/octet-stream"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:
            raise StorageError("Couldn't reach Cloud Object Storage: %s" % e)

    # ---- lifecycle
    def restore(self):
        if not self.enabled:
            print("Storage: local file only (COS_BUCKET / COS_ENDPOINT / COS_APIKEY not set). "
                  "Data is lost when the container restarts.", flush=True)
            return
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        last = None
        for attempt in range(1, 6):
            try:
                status, body = self._request("GET", self.object_key)
                if status == 200:
                    tmp = DB_PATH + ".download"
                    with open(tmp, "wb") as fh:
                        fh.write(body)
                    os.replace(tmp, DB_PATH)
                    self.last_sync = fmt_utc(now_utc())
                    print("Storage: restored %s from bucket %s (%d bytes)" % (self.object_key, self.bucket, len(body)), flush=True)
                    return
                if status == 404:
                    print("Storage: no saved data in bucket %s yet, starting fresh" % self.bucket, flush=True)
                    return
                last = "HTTP %d: %s" % (status, body[:200].decode("utf-8", "replace"))
            except StorageError as e:
                last = str(e)
            print("Storage: restore attempt %d failed (%s), retrying" % (attempt, last), flush=True)
            time.sleep(min(2 * attempt, 8))
        # Refuse to run on an empty database: it would overwrite the real data on the next save.
        sys.exit("Storage: couldn't read bucket %s (%s). Not starting, to protect saved data." % (self.bucket, last))

    def start(self):
        if self.enabled:
            threading.Thread(target=self._loop, name="cos-sync", daemon=True).start()

    def mark_dirty(self):
        if not self.enabled:
            return
        with self.cond:
            if self.dirty_since is None:
                self.dirty_since = time.time()
            self.cond.notify()

    def _loop(self):
        while True:
            with self.cond:
                while self.dirty_since is None:
                    self.cond.wait()
                wait = self.dirty_since + self.delay - time.time()
            if wait > 0:
                time.sleep(wait)
                continue
            try:
                self.upload()
            except StorageError as e:
                self.last_error = str(e)
                print("Storage: save failed (%s), will retry in 30 seconds" % e, flush=True)
                time.sleep(30)

    def _snapshot(self):
        """A consistent copy of the live database, taken with SQLite's online backup."""
        tmp = DB_PATH + ".snapshot"
        src = sqlite3.connect(DB_PATH, timeout=10)
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        with open(tmp, "rb") as fh:
            data = fh.read()
        os.remove(tmp)
        return data

    def upload(self):
        with self.upload_lock:
            with self.cond:
                if self.dirty_since is None:
                    return
                self.dirty_since = None
            try:
                data = self._snapshot()
                status, body = self._request("PUT", self.object_key, data)
                if status not in (200, 201):
                    raise StorageError("HTTP %d: %s" % (status, body[:200].decode("utf-8", "replace")))
                self.uploads += 1
                self.last_sync = fmt_utc(now_utc())
                self.last_error = ""
                day = now_utc().strftime("%Y-%m-%d")
                if day != self.backup_day:
                    backup_key = "backups/reservations-%s.db" % day
                    head_status, _ = self._request("HEAD", backup_key)
                    if head_status == 404:
                        self._request("PUT", backup_key, data)
                    self.backup_day = day
            except Exception:
                self.mark_dirty()   # keep the change queued for the next attempt
                raise

    def flush(self):
        if not self.enabled:
            return
        for attempt in range(3):
            try:
                self.upload()
                if self.dirty_since is None:
                    print("Storage: all changes saved", flush=True)
                return
            except StorageError as e:
                print("Storage: final save attempt %d failed: %s" % (attempt + 1, e), flush=True)
                time.sleep(1)

    def status(self):
        return {"mode": "cloud-object-storage" if self.enabled else "local",
                "last_saved": self.last_sync,
                "pending": self.dirty_since is not None,
                "healthy": not self.last_error}


STORAGE = CosSync()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = connect()
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                env TEXT NOT NULL,
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                name TEXT NOT NULL,
                email TEXT,
                purpose TEXT,
                token TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        cols = [r[1] for r in conn.execute("PRAGMA table_info(bookings)").fetchall()]
        for col in ("owner", "cancelled_at", "cancelled_by"):
            if col not in cols:
                conn.execute("ALTER TABLE bookings ADD COLUMN %s TEXT" % col)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_env_time ON bookings(env, start_at, end_at)")
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS env_settings (
                env TEXT PRIMARY KEY,          -- environment id from config.json (robin, yoda)
                url TEXT,
                description TEXT,
                notes TEXT,
                updated_by TEXT,
                updated_at TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS env_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                env TEXT NOT NULL,
                field TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                changed_by TEXT,
                changed_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS partners (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,            -- 'email' or 'domain'
                value TEXT NOT NULL UNIQUE,    -- lower-case email address or domain
                company TEXT,
                note TEXT,
                added_by TEXT,
                added_at TEXT NOT NULL
            )"""
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- access list
def domain_matches(domain, allowed):
    return any(domain == d or domain.endswith("." + d) for d in allowed)


def access_for(email):
    """Return how this email is allowed in ('admin', 'ibm', 'config', 'partner') or None."""
    low = (email or "").strip().lower()
    if "@" not in low:
        return None
    dom = low.rsplit("@", 1)[1]
    if low in ADMIN_EMAILS:
        return "admin"
    if domain_matches(dom, BUILTIN_DOMAINS):
        return "ibm"
    if low in ALLOWED_EMAILS or domain_matches(dom, ALLOWED_DOMAINS):
        return "config"
    conn = connect()
    try:
        rows = conn.execute("SELECT kind, value FROM partners").fetchall()
    finally:
        conn.close()
    for r in rows:
        if (r["kind"] == "email" and r["value"] == low) or (r["kind"] == "domain" and domain_matches(dom, [r["value"]])):
            return "partner"
    return None


def parse_partner(raw):
    """Turn user input into ('email'|'domain', value) or raise ValueError with a friendly message."""
    value = (raw or "").strip().lower()
    for prefix in ("mailto:", "*.", "@"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    if not value:
        raise ValueError("Enter a partner email address or company domain")
    if "@" in value:
        if not EMAIL_RE.match(value):
            raise ValueError("That doesn't look like a valid email address")
        return "email", value
    if not DOMAIN_RE.match(value):
        raise ValueError("That doesn't look like a valid domain. Use something like partnerco.com")
    if domain_matches(value, BUILTIN_DOMAINS):
        raise ValueError("IBM addresses are always allowed, no need to add them")
    if value in PUBLIC_EMAIL_DOMAINS:
        raise ValueError("%s is a public email provider, so allowing the whole domain would let anyone in. "
                         "Add this partner by their individual email address instead." % value)
    return "domain", value


def partner_row(r):
    return {k: r[k] or "" for k in ("id", "kind", "value", "company", "note", "added_by", "added_at")}


def public_booking(row, user=None):
    owner = (row["owner"] or "").lower()
    return {
        "id": row["id"],
        "env": row["env"],
        "start": row["start_at"],
        "end": row["end_at"],
        "name": row["name"],
        "email": row["email"] or "",
        "purpose": row["purpose"] or "",
        "created": row["created_at"],
        "mine": bool(user and owner and owner == user["e"].lower()),
    }


def booking_status(row, now):
    if row["cancelled_at"]:
        return "cancelled"
    if row["end_at"] <= now:
        return "completed"
    if row["start_at"] <= now:
        return "in progress"
    return "upcoming"


def history_row(row, now):
    start, end = parse_utc(row["start_at"]), parse_utc(row["end_at"])
    return {
        "id": row["id"],
        "env": row["env"],
        "env_name": ENVS.get(row["env"], {}).get("name", row["env"]),
        "start": row["start_at"],
        "end": row["end_at"],
        "hours": round((end - start).total_seconds() / 3600, 2),
        "name": row["name"],
        "email": row["email"] or "",
        "purpose": row["purpose"] or "",
        "status": booking_status(row, now),
        "booked_at": row["created_at"],
        "cancelled_at": row["cancelled_at"] or "",
        "cancelled_by": row["cancelled_by"] or "",
    }


EDITABLE_ENV_FIELDS = ("url", "description", "notes")


def environments():
    """Environments from config.json, with any changes admins made on the page applied on top."""
    conn = connect()
    try:
        overrides = {r["env"]: r for r in conn.execute("SELECT * FROM env_settings").fetchall()}
    finally:
        conn.close()
    out = []
    for e in CONFIG["environments"]:
        item = {k: e.get(k, "") for k in ("id", "name", "description", "url", "notes", "color")}
        o = overrides.get(e["id"])
        if o:
            for f in EDITABLE_ENV_FIELDS:
                if o[f] is not None:
                    item[f] = o[f]
            item["updated_by"] = o["updated_by"] or ""
            item["updated_at"] = o["updated_at"] or ""
        out.append(item)
    return out


def public_config():
    keys = ("title", "subtitle", "slot_minutes", "day_start_hour", "day_end_hour", "max_booking_hours",
            "refresh_seconds", "idle_pause_minutes")
    cfg = {k: CONFIG.get(k) for k in keys}
    cfg["environments"] = environments()
    return cfg


URL_RE = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.IGNORECASE)


def clean_text(value, limit):
    value = value if isinstance(value, str) else ""
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value.strip())
    return value[:limit]


# ---------------------------------------------------------------- signed cookies
def b64e(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64d(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def load_session_key():
    """Signing key for sign-in cookies. Kept in the database (and so in Object Storage), so people stay
    signed in when Code Engine restarts or scales the app."""
    env_key = os.environ.get("RESERVE_SESSION_KEY")
    if env_key:
        return env_key.encode()
    conn = connect()
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'session_key'").fetchone()
        if row:
            return row["value"].encode()
        key = secrets.token_hex(32)
        conn.execute("INSERT INTO meta (key, value) VALUES ('session_key', ?)", (key,))
        conn.commit()
        return key.encode()
    finally:
        conn.close()


def sign(obj):
    payload = b64e(json.dumps(obj, separators=(",", ":")).encode())
    sig = b64e(hmac.new(SESSION_KEY, payload.encode(), hashlib.sha256).digest())
    return payload + "." + sig


def unsign(value):
    try:
        payload, sig = value.rsplit(".", 1)
        good = b64e(hmac.new(SESSION_KEY, payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return None
        obj = json.loads(b64d(payload).decode())
        if float(obj.get("exp", 0)) < time.time():
            return None
        return obj
    except Exception:
        return None


# ---------------------------------------------------------------- OpenID Connect
class AuthError(Exception):
    pass


class AccessDenied(AuthError):
    pass


_discovery = {"data": None, "at": 0.0}


def http_json(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def discovery():
    if _discovery["data"] and time.time() - _discovery["at"] < 3600:
        return _discovery["data"]
    if not OIDC_DISCOVERY:
        raise AuthError("Sign-in is enabled but auth.discovery_url is not set in config.json")
    try:
        data = http_json(OIDC_DISCOVERY)
    except Exception as e:
        raise AuthError("Couldn't reach the %s sign-in service (%s)" % (PROVIDER_NAME, e))
    _discovery.update(data=data, at=time.time())
    return data


def first(value):
    if isinstance(value, list):
        return value[0] if value else ""
    return value or ""


def exchange_code(code, verifier, redirect_uri):
    d = discovery()
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
    if verifier:
        form["code_verifier"] = verifier

    def attempt(method):
        body = dict(form)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if method == "client_secret_basic":
            cred = "%s:%s" % (quote(OIDC_CLIENT_ID, safe=""), quote(OIDC_CLIENT_SECRET, safe=""))
            headers["Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()
        else:
            body["client_id"] = OIDC_CLIENT_ID
            body["client_secret"] = OIDC_CLIENT_SECRET
        return http_json(d["token_endpoint"], urlencode(body).encode(), headers)

    try:
        return attempt(OIDC_TOKEN_AUTH)
    except urllib.error.HTTPError as e:
        if e.code not in (400, 401):
            raise AuthError("The %s token service returned HTTP %d" % (PROVIDER_NAME, e.code))
        # Some providers only accept the other client authentication style. Try it once.
        other = "client_secret_post" if OIDC_TOKEN_AUTH == "client_secret_basic" else "client_secret_basic"
        try:
            return attempt(other)
        except urllib.error.HTTPError as e2:
            detail = ""
            try:
                detail = json.loads(e2.read().decode()).get("error_description", "")
            except Exception:
                pass
            raise AuthError("%s rejected the sign-in (HTTP %d). %s" % (PROVIDER_NAME, e2.code, detail))


def user_from_tokens(tokens, nonce):
    """Validate the ID token and build the session user.

    The ID token comes straight from the provider's token endpoint over verified TLS, so per
    OpenID Connect Core 3.1.3.7 the TLS check stands in for the signature check. Issuer,
    audience, expiry and nonce are still checked.
    """
    d = discovery()
    id_token = tokens.get("id_token")
    if not id_token:
        raise AuthError("%s didn't return an ID token" % PROVIDER_NAME)
    try:
        claims = json.loads(b64d(id_token.split(".")[1]).decode())
    except Exception:
        raise AuthError("Couldn't read the ID token from %s" % PROVIDER_NAME)
    if d.get("issuer") and claims.get("iss") != d["issuer"]:
        raise AuthError("ID token issuer doesn't match the configured provider")
    aud = claims.get("aud")
    if OIDC_CLIENT_ID not in (aud if isinstance(aud, list) else [aud]):
        raise AuthError("ID token was issued for a different application")
    if float(claims.get("exp", 0)) < time.time() - 120:
        raise AuthError("ID token has expired, please sign in again")
    if claims.get("nonce") != nonce:
        raise AuthError("Sign-in check failed (nonce mismatch), please try again")

    info = dict(claims)
    if not first(info.get("email")) and d.get("userinfo_endpoint") and tokens.get("access_token"):
        try:
            info.update(http_json(d["userinfo_endpoint"], headers={"Authorization": "Bearer " + tokens["access_token"]}))
        except Exception:
            pass
    email = first(info.get("email")) or first(info.get("emailAddress"))
    if not email and "@" in first(info.get("preferred_username")):
        email = first(info.get("preferred_username"))
    if not email:
        raise AuthError("%s didn't share an email address. Make sure the 'email' scope is allowed for this app." % PROVIDER_NAME)
    name = first(info.get("name")) or first(info.get("displayName"))
    if not name:
        name = " ".join(x for x in (first(info.get("given_name")), first(info.get("family_name"))) if x)
    name = clean_text(name or email.split("@")[0], 80)
    email = clean_text(email, 120)
    if not access_for(email):
        contact = sorted(ADMIN_EMAILS)[0] if ADMIN_EMAILS else "the page owner"
        raise AccessDenied("The account %s isn't on the access list for this page. IBM addresses get in automatically; "
                        "partners need to be added first. Ask %s to add you." % (email, contact))
    return {"e": email, "n": name}


# ---------------------------------------------------------------- pages
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>body{{font-family:"IBM Plex Sans",system-ui,sans-serif;background:#f4f4f4;color:#161616;display:flex;
min-height:100vh;align-items:center;justify-content:center;margin:0}}.box{{background:#fff;border-radius:8px;
padding:32px 36px;max-width:460px;box-shadow:0 1px 2px rgba(0,0,0,.08)}}h1{{font-size:22px;margin:0 0 10px}}
p{{color:#525252;line-height:1.5}}a{{display:inline-block;margin-top:8px;background:#0f62fe;color:#fff;
padding:10px 18px;border-radius:4px;text-decoration:none;font-weight:600}}</style></head>
<body><div class="box"><h1>{title}</h1><p>{message}</p><a href="{href}">{link}</a></div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "DemoReserve/" + __version__
    timeout = 30

    def setup(self):
        if isinstance(self.request, ssl.SSLSocket):
            self.request.settimeout(self.timeout)
            self.request.do_handshake()
        BaseHTTPRequestHandler.setup(self)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    # ---------- response helpers ----------
    def cookie_header(self, name, value, max_age):
        parts = ["%s=%s" % (name, value), "Path=/", "HttpOnly", "SameSite=Lax", "Max-Age=%d" % max_age]
        if COOKIE_SECURE:
            parts.append("Secure")
        return "; ".join(parts)

    def send_body(self, code, body, ctype, cookies=(), extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        for c in cookies:
            self.send_header("Set-Cookie", c)
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, code, obj, cookies=()):
        self.send_body(code, json.dumps(obj).encode("utf-8"), "application/json", cookies)

    def send_page(self, code, title, message, href="/", link="Back to reservations", cookies=()):
        body = PAGE.format(title=html.escape(title), message=html.escape(message), href=html.escape(href), link=html.escape(link))
        self.send_body(code, body.encode("utf-8"), "text/html; charset=utf-8", cookies)

    def redirect(self, location, cookies=()):
        self.send_body(302, b"", "text/plain", cookies, extra=[("Location", location)])

    def send_static(self, name, ctype):
        with open(os.path.join(STATIC_DIR, name), "rb") as fh:
            body = fh.read()
        self.send_body(200, body, ctype)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 10000:
            raise ValueError("Request too large")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object")
        return data

    def cookies(self):
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie") or "")
        except Exception:
            pass
        return {k: m.value for k, m in jar.items()}

    def current_user(self):
        if not AUTH_ON:
            return None
        sess = unsign(self.cookies().get(SESSION_COOKIE, ""))
        if not sess or not sess.get("e"):
            return None
        # Re-check the access list on every request so removing a partner takes effect right away.
        if not access_for(sess["e"]):
            return None
        return {"e": sess["e"], "n": sess.get("n") or sess["e"]}

    def is_admin(self, user):
        hdr = self.headers.get("X-Admin-Key") or ""
        if ADMIN_KEY and hdr and secrets.compare_digest(hdr, ADMIN_KEY):
            return True
        return bool(user and user["e"].lower() in ADMIN_EMAILS)

    # ---------- partner access list (admins only) ----------
    def partners_list(self):
        conn = connect()
        try:
            rows = conn.execute("SELECT * FROM partners ORDER BY lower(coalesce(company, '')), value").fetchall()
        finally:
            conn.close()
        return self.send_json(200, {
            "always_allowed": ["Any @%s address (including subdomains such as us.ibm.com)" % d for d in BUILTIN_DOMAINS]
            + ["Any @%s address (from config.json)" % d for d in ALLOWED_DOMAINS if d not in BUILTIN_DOMAINS]
            + ["%s (from config.json)" % e for e in sorted(ALLOWED_EMAILS)]
            + ["%s (admin)" % e for e in sorted(ADMIN_EMAILS)],
            "partners": [partner_row(r) for r in rows],
        })

    # ---------- environment settings (admins only) ----------
    def env_update(self, user, env_id):
        if env_id not in ENVS:
            return self.send_json(404, {"error": "Unknown environment"})
        try:
            data = self.read_json()
            new = {}
            if "url" in data:
                url = clean_text(data.get("url"), 500)
                if url and not URL_RE.match(url):
                    raise ValueError("The URL must start with https:// (or http://) and contain no spaces")
                new["url"] = url
            if "description" in data:
                new["description"] = clean_text(data.get("description"), 120)
            if "notes" in data:
                new["notes"] = clean_text(data.get("notes"), 300)
            if not new:
                raise ValueError("Nothing to change")
        except (ValueError, json.JSONDecodeError) as e:
            return self.send_json(400, {"error": str(e)})
        by = user["e"] if user else "admin key"
        when = fmt_utc(now_utc())
        current = {e["id"]: e for e in environments()}[env_id]
        changed = [f for f in new if (current.get(f) or "") != new[f]]
        if not changed:
            return self.send_json(200, {"environment": current, "changed": []})
        with WRITE_LOCK:
            conn = connect()
            try:
                row = conn.execute("SELECT * FROM env_settings WHERE env = ?", (env_id,)).fetchone()
                merged = {f: (row[f] if row and row[f] is not None else current.get(f, "")) for f in EDITABLE_ENV_FIELDS}
                merged.update(new)
                values = (merged["url"], merged["description"], merged["notes"], by, when, env_id)
                if row:
                    conn.execute("UPDATE env_settings SET url = ?, description = ?, notes = ?, updated_by = ?, "
                                 "updated_at = ? WHERE env = ?", values)
                else:
                    conn.execute("INSERT INTO env_settings (url, description, notes, updated_by, updated_at, env) "
                                 "VALUES (?, ?, ?, ?, ?, ?)", values)
                for f in changed:
                    conn.execute(
                        "INSERT INTO env_changes (env, field, old_value, new_value, changed_by, changed_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (env_id, f, current.get(f, ""), new[f], by, when),
                    )
                conn.commit()
            finally:
                conn.close()
        for f in changed:
            self.log_message("%s changed %s %s: %r -> %r", by, env_id, f, current.get(f, ""), new[f])
        updated = {e["id"]: e for e in environments()}[env_id]
        return self.send_json(200, {"environment": updated, "changed": changed})

    def env_changes(self, env_id):
        if env_id not in ENVS:
            return self.send_json(404, {"error": "Unknown environment"})
        conn = connect()
        try:
            rows = conn.execute("SELECT * FROM env_changes WHERE env = ? ORDER BY id DESC LIMIT 100", (env_id,)).fetchall()
        finally:
            conn.close()
        return self.send_json(200, {"changes": [
            {k: r[k] or "" for k in ("field", "old_value", "new_value", "changed_by", "changed_at")} for r in rows]})

    def partners_add(self, user):
        try:
            data = self.read_json()
            kind, value = parse_partner(data.get("value"))
            company = clean_text(data.get("company"), 80)
            note = clean_text(data.get("note"), 200)
        except (ValueError, json.JSONDecodeError) as e:
            return self.send_json(400, {"error": str(e)})
        added_by = user["e"] if user else "admin key"
        with WRITE_LOCK:
            conn = connect()
            try:
                if conn.execute("SELECT 1 FROM partners WHERE value = ?", (value,)).fetchone():
                    return self.send_json(409, {"error": "%s is already on the list" % value})
                cur = conn.execute(
                    "INSERT INTO partners (kind, value, company, note, added_by, added_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (kind, value, company, note, added_by, fmt_utc(now_utc())),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM partners WHERE id = ?", (cur.lastrowid,)).fetchone()
            finally:
                conn.close()
        self.log_message("%s added partner %s %s", added_by, kind, value)
        return self.send_json(201, {"partner": partner_row(row)})

    def partners_delete(self, user, pid):
        with WRITE_LOCK:
            conn = connect()
            try:
                row = conn.execute("SELECT * FROM partners WHERE id = ?", (pid,)).fetchone()
                if not row:
                    return self.send_json(404, {"error": "Not on the list"})
                conn.execute("DELETE FROM partners WHERE id = ?", (pid,))
                conn.commit()
            finally:
                conn.close()
        self.log_message("%s removed partner %s", user["e"] if user else "admin key", row["value"])
        return self.send_json(200, {"deleted": pid})

    def same_origin(self):
        origin = self.headers.get("Origin")
        return not origin or urlparse(origin).netloc == (self.headers.get("Host") or "")

    def redirect_uri(self):
        return OIDC_REDIRECT or "https://%s/auth/callback" % (self.headers.get("Host") or "localhost")

    # ---------- auth routes ----------
    def auth_login(self, query):
        nxt = query.get("next", ["/"])[0]
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        try:
            d = discovery()
        except AuthError as e:
            return self.send_page(502, "Sign-in unavailable", str(e), "/auth/login", "Try again")
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48) if OIDC_PKCE else ""
        params = {
            "response_type": "code",
            "client_id": OIDC_CLIENT_ID,
            "redirect_uri": self.redirect_uri(),
            "scope": OIDC_SCOPES,
            "state": state,
            "nonce": nonce,
        }
        if verifier:
            params["code_challenge"] = b64e(hashlib.sha256(verifier.encode()).digest())
            params["code_challenge_method"] = "S256"
        tx = sign({"s": state, "n": nonce, "v": verifier, "next": nxt, "exp": time.time() + 900})
        sep = "&" if "?" in d["authorization_endpoint"] else "?"
        self.redirect(d["authorization_endpoint"] + sep + urlencode(params), [self.cookie_header(TX_COOKIE, tx, 900)])

    def auth_callback(self, query):
        clear_tx = self.cookie_header(TX_COOKIE, "", 0)
        if query.get("error"):
            msg = query.get("error_description", query["error"])[0]
            return self.send_page(401, "Sign-in didn't complete", msg, "/auth/login", "Try again", [clear_tx])
        tx = unsign(self.cookies().get(TX_COOKIE, ""))
        state = query.get("state", [""])[0]
        if not tx or not state or not hmac.compare_digest(state, tx.get("s", "")):
            return self.send_page(400, "Sign-in expired", "Your sign-in attempt timed out or was started in another tab. Please try again.",
                                  "/auth/login", "Sign in", [clear_tx])
        code = query.get("code", [""])[0]
        if not code:
            return self.send_page(400, "Sign-in didn't complete", "No authorization code was returned.", "/auth/login", "Try again", [clear_tx])
        try:
            tokens = exchange_code(code, tx.get("v", ""), self.redirect_uri())
            user = user_from_tokens(tokens, tx.get("n"))
        except AccessDenied as e:
            self.log_message("access denied: %s", e)
            return self.send_page(403, "Access needed", str(e), "/auth/login", "Try a different account", [clear_tx])
        except AuthError as e:
            return self.send_page(403, "Couldn't sign you in", str(e), "/auth/login", "Try again", [clear_tx])
        except Exception as e:
            self.log_message("sign-in error: %r", e)
            return self.send_page(502, "Couldn't sign you in", "Something went wrong talking to %s. Please try again." % PROVIDER_NAME,
                                  "/auth/login", "Try again", [clear_tx])
        sess = sign({"e": user["e"], "n": user["n"], "exp": time.time() + SESSION_HOURS * 3600})
        self.log_message("signed in: %s", user["e"])
        self.redirect(tx.get("next") or "/", [clear_tx, self.cookie_header(SESSION_COOKIE, sess, int(SESSION_HOURS * 3600))])

    def auth_logout(self):
        self.send_page(200, "Signed out", "You're signed out of the reservation page. Your %s session may still be active, "
                       "so signing in again can be instant." % PROVIDER_NAME, "/auth/login", "Sign in again",
                       [self.cookie_header(SESSION_COOKIE, "", 0)])

    # ---------- routes ----------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)
        if path == "/healthz":
            return self.send_json(200, {"ok": True, "version": __version__, "edition": EDITION, "storage": STORAGE.status()})
        if AUTH_ON:
            if path == "/auth/login":
                return self.auth_login(query)
            if path == "/auth/callback":
                return self.auth_callback(query)
            if path == "/auth/logout":
                return self.auth_logout()
        user = self.current_user()
        if path == "/api/partners":
            if not self.is_admin(user):
                return self.send_json(403 if user else 401, {"error": "Admins only"})
            return self.partners_list()
        em = re.match(r"^/api/environments/([A-Za-z0-9_-]+)/changes$", path)
        if em:
            if not self.is_admin(user):
                return self.send_json(403 if user else 401, {"error": "Admins only"})
            return self.env_changes(em.group(1))
        if path == "/api/history" and not user and self.is_admin(None):
            return self.history(None, query)
        if AUTH_ON and not user:
            if path.startswith("/api/"):
                return self.send_json(401, {"error": "Please sign in"})
            return self.redirect("/auth/login?" + urlencode({"next": self.path}))

        if path in ("/", "/index.html"):
            return self.send_static("index.html", "text/html; charset=utf-8")
        if path == "/api/config":
            return self.send_json(200, public_config())
        if path == "/api/history":
            return self.history(user, query)
        if path == "/api/me":
            return self.send_json(200, {
                "auth": AUTH_ON,
                "provider": PROVIDER_NAME if AUTH_ON else "",
                "email": user["e"] if user else "",
                "name": user["n"] if user else "",
                "admin": bool(user and user["e"].lower() in ADMIN_EMAILS),
            })
        if path == "/api/bookings":
            try:
                rows = self.bookings_between(query.get("from", [""])[0], query.get("to", [""])[0])
            except ValueError as e:
                return self.send_json(400, {"error": str(e)})
            return self.send_json(200, {"bookings": [public_booking(r, user) for r in rows], "now": fmt_utc(now_utc())})
        if path == "/api/snapshot":
            # Everything the page needs in one request (settings, the shown day, the next 14 days),
            # to stay well inside Code Engine's free request allowance.
            q1 = lambda k: query.get(k, [""])[0]
            try:
                day = self.bookings_between(q1("day_from"), q1("day_to"))
                upcoming = self.bookings_between(q1("up_from"), q1("up_to"))
            except ValueError as e:
                return self.send_json(400, {"error": str(e)})
            return self.send_json(200, {
                "config": public_config(),
                "day": [public_booking(r, user) for r in day],
                "upcoming": [public_booking(r, user) for r in upcoming],
                "now": fmt_utc(now_utc()),
            })
        return self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/partners":
            user = self.current_user()
            if not self.same_origin():
                return self.send_json(403, {"error": "Cross-site request blocked"})
            if not self.is_admin(user):
                return self.send_json(403 if user else 401, {"error": "Admins only"})
            return self.partners_add(user)
        if path != "/api/bookings":
            return self.send_json(404, {"error": "Not found"})
        user = self.current_user()
        if AUTH_ON and not user:
            return self.send_json(401, {"error": "Please sign in"})
        if not self.same_origin():
            return self.send_json(403, {"error": "Cross-site request blocked"})
        try:
            data = self.read_json()
            env = data.get("env")
            if env not in ENVS:
                raise ValueError("Unknown environment")
            start = parse_utc(data.get("start"))
            end = parse_utc(data.get("end"))
            name = clean_text(data.get("name"), 80) or (user["n"] if user else "")
            email = user["e"] if user else clean_text(data.get("email"), 120)
            purpose = clean_text(data.get("purpose"), 200)
            if not name:
                raise ValueError("Please enter your name")
            if end <= start:
                raise ValueError("End time must be after start time")
            if end - start < timedelta(minutes=15):
                raise ValueError("Reservations must be at least 15 minutes")
            if end - start > timedelta(hours=MAX_HOURS):
                raise ValueError("Reservations are limited to %g hours" % MAX_HOURS)
            if start < now_utc() - timedelta(minutes=SLOT_MIN):
                raise ValueError("That time has already passed")
        except (ValueError, json.JSONDecodeError) as e:
            return self.send_json(400, {"error": str(e)})

        token = secrets.token_urlsafe(24)
        owner = user["e"].lower() if user else None
        with WRITE_LOCK:
            conn = connect()
            try:
                clash = conn.execute(
                    "SELECT * FROM bookings WHERE env = ? AND cancelled_at IS NULL AND start_at < ? AND end_at > ? "
                    "ORDER BY start_at LIMIT 1",
                    (env, fmt_utc(end), fmt_utc(start)),
                ).fetchone()
                if clash:
                    return self.send_json(409, {
                        "error": "%s is already reserved by %s during part of that time" % (ENVS[env]["name"], clash["name"]),
                        "conflict": public_booking(clash, user),
                    })
                cur = conn.execute(
                    "INSERT INTO bookings (env, start_at, end_at, name, email, purpose, token, created_at, owner) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (env, fmt_utc(start), fmt_utc(end), name, email, purpose, token, fmt_utc(now_utc()), owner),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM bookings WHERE id = ?", (cur.lastrowid,)).fetchone()
            finally:
                conn.close()
        return self.send_json(201, {"booking": public_booking(row, user), "token": token})

    def do_PUT(self):
        m = re.match(r"^/api/environments/([A-Za-z0-9_-]+)$", urlparse(self.path).path)
        if not m:
            return self.send_json(404, {"error": "Not found"})
        user = self.current_user()
        if not self.same_origin():
            return self.send_json(403, {"error": "Cross-site request blocked"})
        if not self.is_admin(user):
            return self.send_json(403 if user else 401, {"error": "Admins only"})
        return self.env_update(user, m.group(1))

    def do_DELETE(self):
        path = urlparse(self.path).path
        pm = re.match(r"^/api/partners/(\d+)$", path)
        if pm:
            user = self.current_user()
            if not self.same_origin():
                return self.send_json(403, {"error": "Cross-site request blocked"})
            if not self.is_admin(user):
                return self.send_json(403 if user else 401, {"error": "Admins only"})
            return self.partners_delete(user, int(pm.group(1)))
        m = re.match(r"^/api/bookings/(\d+)$", path)
        if not m:
            return self.send_json(404, {"error": "Not found"})
        user = self.current_user()
        admin_header = self.headers.get("X-Admin-Key") or ""
        is_key_admin = bool(ADMIN_KEY) and bool(admin_header) and secrets.compare_digest(admin_header, ADMIN_KEY)
        if AUTH_ON and not user and not is_key_admin:
            return self.send_json(401, {"error": "Please sign in"})
        if not self.same_origin():
            return self.send_json(403, {"error": "Cross-site request blocked"})
        booking_id = int(m.group(1))
        token = self.headers.get("X-Booking-Token") or ""
        with WRITE_LOCK:
            conn = connect()
            try:
                row = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
                if not row or row["cancelled_at"]:
                    return self.send_json(404, {"error": "Reservation not found or already cancelled"})
                is_owner = (bool(token) and secrets.compare_digest(token, row["token"])) or \
                    bool(user and row["owner"] and row["owner"].lower() == user["e"].lower())
                is_admin = is_key_admin or bool(user and user["e"].lower() in ADMIN_EMAILS)
                if not (is_owner or is_admin):
                    return self.send_json(403, {"error": "Only the person who made this reservation can cancel it"})
                if user:
                    by = user["e"]
                elif is_owner:
                    by = "%s (booking owner)" % row["name"]
                else:
                    by = "admin key"
                # Keep the record for history: mark it cancelled instead of deleting it.
                conn.execute("UPDATE bookings SET cancelled_at = ?, cancelled_by = ? WHERE id = ?",
                             (fmt_utc(now_utc()), by, booking_id))
                conn.commit()
            finally:
                conn.close()
        self.log_message("%s cancelled reservation %d", by, booking_id)
        return self.send_json(200, {"cancelled": booking_id})

    @staticmethod
    def bookings_between(frm_text, to_text):
        frm, to = parse_utc(frm_text), parse_utc(to_text)
        if to <= frm or to - frm > timedelta(days=62):
            raise ValueError("Range must be positive and at most 62 days")
        conn = connect()
        try:
            return conn.execute(
                "SELECT * FROM bookings WHERE cancelled_at IS NULL AND start_at < ? AND end_at > ? ORDER BY start_at",
                (fmt_utc(to), fmt_utc(frm)),
            ).fetchall()
        finally:
            conn.close()

    # ---------- history ----------
    def history(self, user, query):
        """All reservations, including past and cancelled ones. Admins (and everyone when sign-in is off) see
        everything; other signed-in people see their own reservations."""
        see_all = (not AUTH_ON) or self.is_admin(user)
        where, args = [], []
        if not see_all:
            where.append("lower(owner) = ?")
            args.append(user["e"].lower())
        q1 = lambda k: (query.get(k, [""])[0] or "").strip()
        try:
            if q1("from"):
                where.append("start_at >= ?")
                args.append(fmt_utc(parse_utc(q1("from"))))
            if q1("to"):
                where.append("start_at < ?")
                args.append(fmt_utc(parse_utc(q1("to"))))
        except ValueError as e:
            return self.send_json(400, {"error": str(e)})
        if q1("env") in ENVS:
            where.append("env = ?")
            args.append(q1("env"))
        if q1("q"):
            like = "%" + q1("q").lower().replace("\\", "").replace("%", "").replace("_", "") + "%"
            where.append("(lower(name) LIKE ? OR lower(coalesce(email,'')) LIKE ? OR lower(coalesce(purpose,'')) LIKE ?)")
            args += [like, like, like]
        now = fmt_utc(now_utc())
        status = q1("status")
        if status == "cancelled":
            where.append("cancelled_at IS NOT NULL")
        elif status == "active":
            where.append("cancelled_at IS NULL")
        elif status == "completed":
            where.append("cancelled_at IS NULL AND end_at <= ?")
            args.append(now)
        elif status == "upcoming":
            where.append("cancelled_at IS NULL AND end_at > ?")
            args.append(now)
        try:
            limit = max(1, min(5000, int(q1("limit") or 1000)))
        except ValueError:
            limit = 1000
        sql = "SELECT * FROM bookings" + (" WHERE " + " AND ".join(where) if where else "")
        conn = connect()
        try:
            total = conn.execute(sql.replace("SELECT *", "SELECT count(*)", 1), args).fetchone()[0]
            rows = conn.execute(sql + " ORDER BY start_at DESC LIMIT ?", args + [limit]).fetchall()
            sums = conn.execute(
                sql.replace("SELECT *", "SELECT env, count(*) AS n, "
                            "sum((julianday(end_at) - julianday(start_at)) * 24) AS hours", 1)
                + (" AND " if where else " WHERE ") + "cancelled_at IS NULL GROUP BY env", args).fetchall()
        finally:
            conn.close()
        return self.send_json(200, {
            "scope": "all" if see_all else "mine",
            "total": total,
            "returned": len(rows),
            "summary": [{"env": r["env"], "env_name": ENVS.get(r["env"], {}).get("name", r["env"]),
                         "reservations": r["n"], "hours": round(r["hours"] or 0, 1)} for r in sums],
            "reservations": [history_row(r, now) for r in rows],
        })


# ---------------------------------------------------------------- server and TLS
class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # Browsers that reject a self-signed cert, port scanners and dropped connections
        # raise TLS/connection errors. Log one line instead of a traceback.
        exc = sys.exc_info()[1]
        if isinstance(exc, (ssl.SSLError, ConnectionError, socket.timeout, OSError)):
            sys.stderr.write("%s connection error: %s\n" % (client_address[0], exc))
            return
        HTTPServer.handle_error(self, request, client_address)


def resolve(path):
    return path if os.path.isabs(path) else os.path.join(BASE_DIR, path)


def generate_self_signed(cert_path, key_path):
    """Create a self-signed certificate with the openssl CLI (present on RHEL and Ubuntu)."""
    os.makedirs(os.path.dirname(cert_path), exist_ok=True)
    os.makedirs(os.path.dirname(key_path), exist_ok=True)
    fqdn = socket.getfqdn() or "localhost"
    names = [fqdn, "localhost", "127.0.0.1"] + list(CONFIG.get("tls_extra_names", []))
    sans = []
    for n in dict.fromkeys(n.strip() for n in names if n and n.strip()):
        try:
            ipaddress.ip_address(n)
            sans.append("IP:" + n)
        except ValueError:
            sans.append("DNS:" + n)
    base = ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "825",
            "-keyout", key_path, "-out", cert_path, "-subj", "/CN=" + fqdn[:64]]
    try:
        subprocess.run(base + ["-addext", "subjectAltName=" + ",".join(sans)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError:
        # Very old openssl (1.0.x) has no -addext. A cert without SANs still works after the browser warning.
        subprocess.run(base, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    os.chmod(key_path, 0o600)
    print("Generated self-signed certificate for: %s" % ", ".join(sans), flush=True)


def tls_context():
    if not truthy(os.environ.get("RESERVE_TLS", CONFIG.get("tls", False))):
        return None
    cert = resolve(os.environ.get("RESERVE_TLS_CERT") or CONFIG.get("tls_cert", "certs/server.crt"))
    key = resolve(os.environ.get("RESERVE_TLS_KEY") or CONFIG.get("tls_key", "certs/server.key"))
    if not (os.path.exists(cert) and os.path.exists(key)):
        print("No certificate at %s, creating a self-signed one" % cert, flush=True)
        generate_self_signed(cert, key)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    if hasattr(ssl, "TLSVersion"):
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx


def handle_sigterm(signum, frame):
    # Code Engine sends SIGTERM when it scales the app down or replaces it. Leave serve_forever()
    # so main() can save pending changes before the container stops.
    raise SystemExit(0)


def main():
    global SESSION_KEY
    STORAGE.restore()
    init_db()
    SESSION_KEY = load_session_key()
    STORAGE.start()
    signal.signal(signal.SIGTERM, handle_sigterm)
    if AUTH_ON:
        missing = [k for k, v in (("client_id", OIDC_CLIENT_ID), ("client_secret", OIDC_CLIENT_SECRET),
                                  ("discovery_url", OIDC_DISCOVERY)) if not v]
        if missing:
            sys.exit("auth.enabled is true but these auth settings are empty: %s" % ", ".join(missing))
        if not ADMIN_EMAILS:
            print("WARNING: auth.admin_emails is empty, so nobody can manage the partner list from the page", flush=True)
        print("Sign-in with %s is on (always allowed: %s; partners are managed on the page by admins)"
              % (PROVIDER_NAME, ", ".join("*@" + d for d in ALLOWED_DOMAINS)), flush=True)
    host = os.environ.get("RESERVE_HOST") or CONFIG.get("host", "0.0.0.0")
    port = int(os.environ.get("RESERVE_PORT") or os.environ.get("PORT") or CONFIG.get("port", 8080))
    ctx = tls_context()
    server = ThreadedServer((host, port), Handler)
    scheme = "http"
    if ctx:
        # Handshake happens in each request thread, so one slow client can't stall the listener.
        server.socket = ctx.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
        scheme = "https"
    print("Demo reservation board v%s (%s) running on %s://%s:%d" % (__version__, EDITION, scheme, host, port), flush=True)
    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        print("Shutting down, saving any pending changes", flush=True)
        STORAGE.flush()


if __name__ == "__main__":
    main()
