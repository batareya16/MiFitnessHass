"""
Xiaomi account login — extracts ssecurity + serviceToken automatically.
Ported from mi_get_creds.py, adapted for async HA config flow.

Login is a 3-step dance:
  1. GET  https://account.xiaomi.com/pass/serviceLogin?sid=xiaomiio  → _sign
  2. POST https://account.xiaomi.com/pass/serviceLoginAuth2          → ssecurity + location
     (may loop: captcha code needed, or identity verification needed)
  3. GET  {location}  (→ sts.api.io.mi.com/sts)                      → serviceToken cookie

Email 2FA flow (when notificationUrl returned):
  1. GET  notificationUrl                               → HTML page (authStart)
  2. GET  /identity/list?sid=xiaomiio&context=<ctx>     → sets identity_session cookie
  3. POST /identity/auth/sendEmailTicket                → email sent
  4. POST /identity/auth/verifyEmail (ticket=<otp>)     → location or fallback
  5. GET  /identity/result/check                        → 302 → Auth2/end URL
  6. GET  Auth2/end (no redirect)                       → extension-pragma header → ssecurity
  7. GET  STS URL                                       → serviceToken cookie
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import random
import re
import string
import time
from urllib.parse import parse_qs, urlparse

import requests

_LOGGER = logging.getLogger(__name__)


# ── Exception hierarchy ───────────────────────────────────────────────────────

class XiaomiLoginError(Exception):
    """Base login error."""

class XiaomiInvalidCredentials(XiaomiLoginError):
    """Wrong username or password."""

class XiaomiCaptchaRequired(XiaomiLoginError):
    """Captcha challenge. Attributes: captcha_url (str), captcha_sign (str)."""
    def __init__(self, captcha_url: str, captcha_sign: str):
        super().__init__("Captcha required")
        self.captcha_url  = captcha_url
        self.captcha_sign = captcha_sign

class XiaomiApprovalRequired(XiaomiLoginError):
    """Phone/email approval required. Attributes: notification_url, approval_sign."""
    def __init__(self, notification_url: str, approval_sign: str):
        super().__init__("Phone approval required")
        self.notification_url = notification_url
        self.approval_sign    = approval_sign


# ── Session builder ───────────────────────────────────────────────────────────

def _make_login_session() -> requests.Session:
    # Native-app Dalvik UA. The server issues a different ssecurity based on UA —
    # Dalvik/APP/xiaomi.wearable yields the ssecurity that hlth.io.mi.com expects.
    dev_hex = "".join(random.choices("0123456789abcdef", k=32))
    ua = (
        "Dalvik/2.1.0 (Linux; U; Android 14; SM-F721B Build/UP1A.231005.007) "
        "APP/xiaomi.wearable APPV/355000 MK/U00tRjcyMUI= "
        "SDKV/5.3.0.release.79 PassportSDK/5.3.0.release.79 "
        "XiaomiAccountSSO/5.3.0.release.79 passport-ui/5.3.0.release.79 "
        "CPN/com.xiaomi.wearable DEVT/UGhvbmU= BRA/c2Ftc3VuZw== DEVS/QW5kcm9pZA=="
    )
    s = requests.Session()
    s.headers.update({
        "User-Agent":   ua,
        "Content-Type": "application/x-www-form-urlencoded",
    })
    s.cookies.update({"sdkVersion": "3.8.6", "deviceId": f"an_{dev_hex}"})
    return s


# ── Step helpers ─────────────────────────────────────────────────────────────

def _get_sign(session: requests.Session, username: str) -> str:
    """Step 1: fetch _sign cookie."""
    session.cookies.set("userId", username)
    r = session.get(
        "https://account.xiaomi.com/pass/serviceLogin",
        params={"sid": "miothealth", "_json": "true"},
        timeout=15,
    )
    d = json.loads(r.text.replace("&&&START&&&", ""))
    return d.get("_sign", "")


def _post_credentials(
    session: requests.Session,
    username: str,
    password: str,
    sign: str,
    captcha_code: str = "",
) -> dict:
    """Step 2: POST login form. Returns raw response dict."""
    post: dict = {
        "sid":      "miothealth",
        "hash":     hashlib.md5(password.encode()).hexdigest().upper(),
        "callback": "https://sts-hlth.io.mi.com/healthapp/sts",
        "qs":       "%3F_json%3Dtrue%26sid%3Dmiothealth%26_locale%3Den_US",
        "user":     username,
        "_json":    "true",
    }
    if sign:
        post["_sign"] = sign
    if captcha_code:
        post["captCode"] = captcha_code

    r = session.post(
        "https://account.xiaomi.com/pass/serviceLoginAuth2",
        data=post,
        timeout=15,
    )
    return json.loads(r.text.replace("&&&START&&&", ""))


def _get_service_token(session: requests.Session, location: str) -> str:
    """Step 3: follow redirect → grab serviceToken from Set-Cookie."""
    session.headers.update({"content-type": "application/x-www-form-urlencoded"})
    r = session.get(location, timeout=15)
    _LOGGER.warning(
        "STS response: url=%s status=%d cookies=%s",
        r.url, r.status_code,
        [c.name for c in r.cookies] + [c.name for c in session.cookies
                                        if c.name not in [rc.name for rc in r.cookies]],
    )

    token = r.cookies.get("serviceToken")
    if not token:
        for c in session.cookies:
            if c.name == "serviceToken":
                return c.value
    if not token:
        raise XiaomiLoginError("serviceToken not found in STS cookies")
    return token


def _extract_service_token_info(data: dict, label: str) -> dict:
    """
    Extract ssecurity + serviceToken from a response dict that may contain:
      - direct fields: ssecurity, serviceToken  (passtoken/refresh style)
      - nested:        serviceTokens.xiaomiio   (userprofile style, value is JSON string or dict)
    Returns dict with ssecurity, service_token, user_id, c_user_id on success, else {}.
    """
    # Direct fields (passtoken/refresh response)
    ssecurity     = data.get("ssecurity", "")
    service_token = data.get("serviceToken", "")
    user_id_out   = str(data.get("userId", ""))
    c_user_id_out = data.get("cUserId", "")

    if ssecurity and service_token:
        _LOGGER.warning("%s: direct ssecurity+serviceToken found", label)
        return {
            "ssecurity":     ssecurity,
            "service_token": service_token,
            "user_id":       user_id_out,
            "c_user_id":     c_user_id_out,
        }

    # Nested serviceTokens (userprofile response)
    tokens = data.get("serviceTokens", {})
    if tokens:
        token_raw = tokens.get("xiaomiio") or (next(iter(tokens.values()), None) if tokens else None)
        if token_raw is not None:
            if isinstance(token_raw, str):
                try:
                    token_info: dict = json.loads(token_raw)
                except Exception:
                    _LOGGER.warning("%s: cannot parse token_raw=%.100s", label, token_raw)
                    return {}
            else:
                token_info = token_raw  # type: ignore[assignment]
            ssecurity     = token_info.get("ssecurity", "")
            service_token = token_info.get("serviceToken", "")
            if ssecurity and service_token:
                return {
                    "ssecurity":     ssecurity,
                    "service_token": service_token,
                    "user_id":       str(data.get("userId", "")),
                    "c_user_id":     token_info.get("cUserId", ""),
                }
            _LOGGER.warning(
                "%s: serviceTokens.xiaomiio missing fields "
                "(ssecurity=%s serviceToken=%s)",
                label, bool(ssecurity), bool(service_token),
            )
            return {}

    _LOGGER.warning(
        "%s: no usable credentials — keys=%s",
        label, list(data.keys()),
    )
    return {}


def _exchange_pass_token(
    session: requests.Session,
    user_id: str,
    pass_token: str,
    sid: str = "miothealth",
) -> dict:
    """
    Exchange passToken → ssecurity + serviceToken via GET /pass/serviceLogin.

    Silently refreshes credentials: passToken is sent as a cookie and the
    response carries ssecurity + a location to fetch serviceToken from.
    No 2FA is triggered because passToken already proves a completed auth.

    Returns dict with ssecurity, service_token, user_id, c_user_id, pass_token.
    Empty dict on failure.
    """
    # Set passToken as a cookie — the endpoint reads it from cookies, not params
    session.cookies.set("passToken", pass_token, domain="account.xiaomi.com")
    session.cookies.set("userId",    user_id,    domain="account.xiaomi.com")

    try:
        # sid=miothealth with the sts-hlth.io.mi.com callback yields the
        # health-specific ssecurity and serviceToken.
        sids_to_try = [sid] if sid == "miothealth" else [sid, "miothealth"]
        data: dict = {}
        ssec = ""
        for try_sid in sids_to_try:
            r = session.get(
                "https://account.xiaomi.com/pass/serviceLogin",
                params={
                    "_json":   "true",
                    "sid":     try_sid,
                    "_locale": "en_US",
                },
                timeout=15,
            )
            _LOGGER.debug("exchange_pass_token: HTTP=%d sid=%s", r.status_code, try_sid)
            data = json.loads(r.text.replace("&&&START&&&", "").strip())
            code = data.get("code", -1)
            ssec = data.get("ssecurity", "")
            if code == 0 and ssec:
                _LOGGER.warning("exchange_pass_token: got ssecurity with sid=%s", try_sid)
                break
            _LOGGER.warning(
                "exchange_pass_token sid=%s: code=%s ssec_present=%s loc_present=%s",
                try_sid, code, bool(ssec), bool(data.get("location")),
            )

        location = data.get("location", "")
        base_result = {
            "ssecurity":     ssec,
            "service_token": "",
            "user_id":       str(data.get("userId", user_id)),
            "c_user_id":     data.get("cUserId", ""),
            "pass_token":    data.get("passToken", pass_token),
        }

        if ssec and location:
            _LOGGER.warning("exchange_pass_token location: %s", location[:200])
            try:
                service_token = _get_service_token(session, location)
                base_result["service_token"] = service_token
                _LOGGER.warning(
                    "exchange_pass_token SUCCESS: ssec_len=%d tok_len=%d",
                    len(ssec), len(service_token),
                )
            except Exception as sts_exc:
                # STS didn't return serviceToken (e.g. deviceinfo → api.device.xiaomi.net).
                # Return ssecurity anyway so callers can use it with a separate serviceToken.
                _LOGGER.warning(
                    "exchange_pass_token: STS failed (%s) — returning ssecurity only (len=%d)",
                    sts_exc, len(ssec),
                )
            return base_result

        if ssec:
            _LOGGER.warning("exchange_pass_token: ssecurity but no location")
            return base_result

        _LOGGER.warning("exchange_pass_token: failed all sids — no ssecurity obtained")
    except Exception as exc:
        _LOGGER.warning("exchange_pass_token exception: %s", exc)

    return {}


def try_silent_token_refresh(pass_token: str, user_id: str) -> dict:
    """
    Silently obtain fresh ssecurity + xiaomiio serviceToken using stored passToken.

    Step 1: GET /pass/serviceLogin with passToken cookie → ssecurity
            (Mi Fitness app uses sid=passportapi — falls back to it if xiaomiio fails)
    Step 2: GET /pass/serviceLoginAuth2 with passToken cookie → xiaomiio location → serviceToken

    Returns dict with ssecurity, service_token on success, or {} on failure.
    """
    if not pass_token or not user_id:
        return {}
    session = _make_login_session()

    # miothealth exchange: gives health-specific ssecurity + serviceToken (sts-hlth.io.mi.com).
    creds = _exchange_pass_token(session, user_id, pass_token, sid="miothealth")
    ssecurity     = creds.get("ssecurity", "")
    service_token = creds.get("service_token", "")

    if ssecurity and service_token:
        _LOGGER.warning(
            "try_silent_token_refresh SUCCESS: ssec_len=%d tok_len=%d",
            len(ssecurity), len(service_token),
        )
        return {
            "ssecurity":     ssecurity,
            "service_token": service_token,
            "user_id":       creds.get("user_id", user_id),
            "c_user_id":     creds.get("c_user_id", ""),
            "pass_token":    pass_token,  # keep original, don't rotate
        }

    _LOGGER.warning(
        "try_silent_token_refresh: failed — ssec=%s tok=%s",
        bool(ssecurity), bool(service_token),
    )
    return {}


# ── Public API ────────────────────────────────────────────────────────────────

class XiaomiLoginSession:
    """
    Stateful login helper — used across multiple config-flow steps.

    Typical usage in a non-captcha / non-2FA case:
        ls = XiaomiLoginSession()
        result = ls.start(username, password)   # raises or returns LoginResult

    Captcha case:
        try:
            ls.start(username, password)
        except XiaomiCaptchaRequired as e:
            # show e.captcha_url to user, ask for code
            result = ls.submit_captcha(code)

    Email 2FA case:
        try:
            ls.start(username, password)
        except XiaomiApprovalRequired:
            ls.start_email_verification()       # sends email
            result = ls.verify_with_code(otp)   # submit code
    """

    def __init__(self) -> None:
        self._session               = _make_login_session()
        self._username              = ""
        self._password              = ""
        self._last_sign             = ""
        self._user_id               = ""
        self._pass_token            = ""   # from serviceLoginAuth2 Set-Cookie or JSON
        self._notification_url      = ""
        self._context               = ""   # parsed from notificationUrl query string
        self._verify_referer        = ""   # final URL after GET notificationUrl
        self._ssecurity_2fa         = ""   # ssecurity from extension-pragma
        self._psecurity_2fa         = ""   # psecurity from extension-pragma (try as signing key)
        self._ssecurity_from_login  = ""   # ssecurity from serviceLoginAuth2 when 2FA triggered
        self._c_user_id_from_login  = ""

    def start(self, username: str, password: str) -> "LoginResult":
        """Begin login. May raise XiaomiCaptchaRequired or XiaomiApprovalRequired."""
        self._username = username
        self._password = password
        self._last_sign = _get_sign(self._session, username)
        return self._attempt(captcha_code="")

    def submit_captcha(self, captcha_code: str) -> "LoginResult":
        """Continue after user entered captcha text."""
        return self._attempt(captcha_code=captcha_code)

    def inject_service_token(self, service_token: str) -> None:
        """
        Inject a serviceToken from the user's browser into our session cookies.
        After visiting notificationUrl, Xiaomi sets serviceToken on .mi.com domain.
        Injecting it here lets the server link the verified browser session with
        our API session on the next POST attempt.
        """
        for domain in ("account.xiaomi.com", ".xiaomi.com", "xiaomi.com"):
            self._session.cookies.set("serviceToken", service_token, domain=domain)
        _LOGGER.debug("Injected browser serviceToken into login session")

    # ── Email 2FA flow ────────────────────────────────────────────────────────

    def start_email_verification(self) -> None:
        """
        Initiate email OTP flow.  Must be called right after XiaomiApprovalRequired.

        Steps:
          1. GET notificationUrl          → HTML authStart page (sets session cookies)
          2. GET /identity/list           → sets identity_session cookie (critical!)
          3. POST sendEmailTicket         → Xiaomi sends OTP email
        """
        if not self._notification_url:
            raise XiaomiLoginError("No notificationUrl — call start() first")
        if not self._context:
            raise XiaomiLoginError("No context parameter in notificationUrl")

        context = self._context

        # 1) GET notificationUrl (authStart) — must use our authenticated session
        _LOGGER.debug("GET notificationUrl (authStart): %s", self._notification_url)
        r = self._session.get(
            self._notification_url,
            timeout=15,
            allow_redirects=True,
        )
        self._verify_referer = r.url
        _LOGGER.debug("authStart: HTTP=%d  final_url=%s  cookies=%s",
                      r.status_code, r.url, list(self._session.cookies.keys()))

        # 2) GET /identity/list — this call sets identity_session cookie
        _LOGGER.debug("GET /identity/list  context=%s", context)
        r = self._session.get(
            "https://account.xiaomi.com/identity/list",
            params={"sid": "miothealth", "context": context, "_locale": "en_US"},
            timeout=15,
        )
        _LOGGER.debug("identity/list: HTTP=%d  cookies=%s",
                      r.status_code, list(self._session.cookies.keys()))

        # 3) POST sendEmailTicket
        dc = int(time.time() * 1000)
        ick = self._session.cookies.get("ick", "")
        r = self._session.post(
            "https://account.xiaomi.com/identity/auth/sendEmailTicket",
            params={
                "_dc":     str(dc),
                "sid":     "miothealth",
                "context": context,
                "mask":    "0",
                "_locale": "en_US",
            },
            data={
                "retry":  "0",
                "icode":  "",
                "_json":  "true",
                "ick":    ick,
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=15,
        )
        _LOGGER.warning("sendEmailTicket: HTTP=%d  body=%s",
                        r.status_code, r.text[:300])
        try:
            jr = r.json()
            code = jr.get("code", -1)
            if code not in (0,) and jr.get("result") != "ok":
                _LOGGER.warning("sendEmailTicket non-OK: code=%s msg=%s",
                                code, jr.get("message", ""))
        except Exception:
            pass

    def verify_with_code(self, otp_code: str) -> "LoginResult":
        """
        Submit OTP from email, then complete the verification chain:
          1. POST /identity/auth/verifyEmail
          2. GET  /identity/result/check  (fallback if no location returned)
          3. GET  Auth2/end (no redirect) → extension-pragma → ssecurity
          4. GET  STS URL                 → serviceToken cookie
        """
        if not self._context:
            raise XiaomiLoginError("No context — call start_email_verification() first")

        context = self._context
        dc = int(time.time() * 1000)
        ick = self._session.cookies.get("ick", "")

        # 1) POST verifyEmail
        r = self._session.post(
            "https://account.xiaomi.com/identity/auth/verifyEmail",
            params={
                "_flag":   "8",
                "_json":   "true",
                "sid":     "miothealth",
                "context": context,
                "mask":    "0",
                "_locale": "en_US",
            },
            data={
                "_flag":  "8",
                "ticket": otp_code.strip(),
                "trust":  "false",
                "_json":  "true",
                "ick":    ick,
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=15,
        )
        _LOGGER.warning("verifyEmail: HTTP=%d  body=%s",
                        r.status_code, r.text[:400])

        # Extract finish location from response (body may have &&&START&&& prefix)
        finish_loc: str | None = None
        numeric_user_id: str = ""
        try:
            resp_json = json.loads(r.text.replace("&&&START&&&", ""))
            code = resp_json.get("code", -1)
            result_str = resp_json.get("result", "")
            _LOGGER.debug("verifyEmail json: code=%s result=%s", code, result_str)
            finish_loc = resp_json.get("location")
            # Extract numeric userId from location query params
            if finish_loc:
                try:
                    loc_qs = parse_qs(urlparse(finish_loc).query)
                    numeric_user_id = loc_qs.get("userId", [""])[0]
                except Exception:
                    pass
            if not finish_loc and code not in (0,) and result_str != "ok":
                _LOGGER.warning("verifyEmail code=%s, trying fallback", code)
        except Exception:
            pass

        # 2) Fallback: GET /identity/result/check
        if not finish_loc:
            _LOGGER.debug("verifyEmail: no location, fallback to /identity/result/check")
            r0 = self._session.get(
                "https://account.xiaomi.com/identity/result/check",
                params={"sid": "miothealth", "context": context, "_locale": "en_US"},
                allow_redirects=False,
                timeout=15,
            )
            _LOGGER.debug("result/check (fallback): HTTP=%d  Location=%s",
                          r0.status_code, r0.headers.get("Location"))
            if r0.status_code in (301, 302):
                finish_loc = r0.headers.get("Location")

        if not finish_loc:
            raise XiaomiLoginError(
                "Email verification failed: could not determine finish location"
            )

        # 3a) If finish_loc is result/check, follow it one hop to get Auth2/end URL
        if "identity/result/check" in finish_loc:
            r = self._session.get(finish_loc, allow_redirects=False, timeout=15)
            _LOGGER.debug("result/check: HTTP=%d  Location=%s",
                          r.status_code, r.headers.get("Location"))
            end_url = r.headers.get("Location")
        else:
            end_url = finish_loc

        if not end_url:
            raise XiaomiLoginError("Auth2/end URL not found in verification chain")

        # 3b) GET Auth2/end WITHOUT following redirects — grab extension-pragma header
        r = self._session.get(end_url, allow_redirects=False, timeout=15)
        _LOGGER.debug("Auth2/end: HTTP=%d  headers=%s",
                      r.status_code, dict(r.headers))
        # Some servers return 200 "Tips" page first, then 302 on repeat
        if r.status_code == 200 and "Tips" in r.text:
            r = self._session.get(end_url, allow_redirects=False, timeout=15)
            _LOGGER.debug("Auth2/end (2nd): HTTP=%d", r.status_code)

        # Extract ssecurity from extension-pragma
        ext_prag = r.headers.get("extension-pragma")
        _LOGGER.warning("Auth2/end: HTTP=%d  extension-pragma=%s",
                        r.status_code, ext_prag[:200] if ext_prag else "MISSING")
        if ext_prag:
            try:
                ep = json.loads(ext_prag)
                ssec  = ep.get("ssecurity", "")
                psec  = ep.get("psecurity", "")
                # Store both — we'll try psecurity as signing key since ssecurity gave 401
                self._ssecurity_2fa  = ssec
                self._psecurity_2fa  = psec
                _LOGGER.warning(
                    "extension-pragma: ssecurity_len=%d prefix=%s  psecurity_len=%d prefix=%s",
                    len(ssec), ssec[:8] if ssec else "-",
                    len(psec), psec[:8] if psec else "-",
                )
            except Exception as exc:
                _LOGGER.warning("Failed to parse extension-pragma: %s  raw=%s",
                                exc, ext_prag[:100])

        # 4) Find STS URL
        sts_url: str | None = r.headers.get("Location")
        if not sts_url and r.text:
            idx = r.text.find("https://sts.api.io.mi.com/sts")
            if idx != -1:
                end = r.text.find('"', idx)
                sts_url = r.text[idx: end if end != -1 else idx + 300]

        if not sts_url:
            raise XiaomiLoginError("STS URL not found after Auth2/end")

        # 5) GET STS → serviceToken in cookies
        r = self._session.get(sts_url, allow_redirects=True, timeout=15)
        _LOGGER.warning("STS: HTTP=%d  final_url=%s  body=%s",
                        r.status_code, r.url, r.text[:100])

        service_token: str = r.cookies.get("serviceToken") or ""
        if not service_token:
            for c in self._session.cookies:
                if c.name == "serviceToken":
                    service_token = c.value
                    break
        _LOGGER.warning("STS: serviceToken_len=%d  all_cookie_names=%s",
                        len(service_token),
                        [c.name for c in self._session.cookies])
        if not service_token:
            raise XiaomiLoginError("serviceToken not found after STS redirect")

        # Collect userId / cUserId
        # Priority: numeric ID from verifyEmail response URL > stored > cookies
        user_id = (
            numeric_user_id
            or self._user_id
            or next((c.value for c in self._session.cookies if c.name == "userId"), "")
        )
        c_user_id = next(
            (c.value for c in self._session.cookies if c.name == "cUserId"), ""
        )
        ssecurity = self._ssecurity_2fa

        _LOGGER.warning(
            "2FA chain: extension-pragma ssecurity_present=%s  token_len=%d  user_id=%s  "
            "ssecurity_from_login_present=%s (len=%d)",
            bool(ssecurity), len(service_token), user_id,
            bool(self._ssecurity_from_login), len(self._ssecurity_from_login),
        )

        # ── Best: use ssecurity saved from initial serviceLoginAuth2 (most reliable)
        # serviceLoginAuth2 returns ssecurity even when notificationUrl/2FA is required.
        # This is the canonical account-level ssecurity, identical to what a normal
        # login would return. All post-2FA endpoints (loginStep2, extension-pragma)
        # give a different or less reliable ssecurity.
        pass_token_for_result = (
            next((c.value for c in self._session.cookies if c.name == "passToken"), "")
            or self._pass_token
        )

        if self._ssecurity_from_login:
            _LOGGER.warning(
                "Using ssecurity from initial serviceLoginAuth2 (len=%d) — "
                "skipping post-2FA token exchange",
                len(self._ssecurity_from_login),
            )
            return LoginResult(
                user_id=user_id,
                c_user_id=self._c_user_id_from_login or c_user_id,
                ssecurity=self._ssecurity_from_login,
                service_token=service_token,
                pass_token=pass_token_for_result,
            )

        # ── Primary: /pass/login/passtoken/app/userprofile
        # Modern replacement for the dead loginStep2 endpoint.
        # Takes userId + passToken → returns JSON with serviceTokens.xiaomiio
        # which contains the CORRECT ssecurity + serviceToken for API signing.
        # Prefer session-cookie passToken (may be refreshed by STS) over pre-2FA token
        pass_token_cookie = (
            next((c.value for c in self._session.cookies if c.name == "passToken"), "")
            or self._pass_token
        )
        _LOGGER.warning(
            "post-2FA credential fetch: passToken_len=%d  user_id=%s  "
            "extension-pragma_ssecurity_len=%d  "
            "all_cookie_names=%s",
            len(pass_token_cookie), user_id,
            len(ssecurity) if ssecurity else 0,
            [c.name for c in self._session.cookies],
        )

        # ── Primary: passportapi ssecurity (fresh session) + 2FA STS token ─────
        # Use BOTH ssecurity and serviceToken from the passportapi exchange (same session).
        # passportapi serviceToken is len~320 vs xiaomiio/2FA STS len~192 — different tokens.
        # All other combinations (xiaomiio ssec, extension-pragma ssec, mixed tokens)
        # have been tested and give 401.
        # Primary: deviceinfo ssecurity (Dalvik UA) + 2FA STS serviceToken.
        # deviceinfo leads to api.device.xiaomi.net which has no serviceToken cookie,
        # so _exchange_pass_token returns ssecurity="" service_token="".
        # Take ssecurity only; keep service_token from 2FA STS (already set above).
        # miothealth exchange: health-specific ssecurity + serviceToken from sts-hlth.io.mi.com.
        up_success = False
        if pass_token_cookie and user_id:
            fresh = _make_login_session()
            creds = _exchange_pass_token(fresh, user_id, pass_token_cookie, sid="miothealth")
            if creds.get("ssecurity") and creds.get("service_token"):
                ssecurity     = creds["ssecurity"]
                service_token = creds["service_token"]
                user_id       = creds.get("user_id", user_id)
                c_user_id     = creds.get("c_user_id", "") or c_user_id
                _LOGGER.warning(
                    "post-2FA miothealth: ssec_len=%d tok_len=%d",
                    len(ssecurity), len(service_token),
                )
                up_success = True

        # Fallback: extension-pragma ssecurity + 2FA STS serviceToken
        if not up_success and self._ssecurity_2fa and service_token:
            ssecurity  = self._ssecurity_2fa
            up_success = True
            _LOGGER.warning(
                "post-2FA fallback extension-pragma: ssec_len=%d tok_len=%d",
                len(ssecurity), len(service_token),
            )

        ssecurity_reliable = up_success
        if not up_success:
            ssecurity = self._ssecurity_2fa
            _LOGGER.warning(
                "post-2FA: all paths failed — last resort extension-pragma "
                "(len=%d), ssecurity_reliable=False",
                len(ssecurity) if ssecurity else 0,
            )

        if not ssecurity:
            raise XiaomiLoginError(
                "Could not obtain ssecurity after 2FA — "
                "userprofile, loginStep2, and extension-pragma all failed"
            )

        return LoginResult(
            user_id=user_id,
            c_user_id=c_user_id,
            ssecurity=ssecurity,
            service_token=service_token,
            ssecurity_reliable=ssecurity_reliable,
            pass_token=pass_token_for_result,
        )

    # ── internal ──────────────────────────────────────────────────────────────

    def _attempt(self, captcha_code: str) -> "LoginResult":
        data = _post_credentials(
            self._session,
            self._username,
            self._password,
            self._last_sign,
            captcha_code=captcha_code,
        )
        code   = data.get("code", -1)
        result = data.get("result", "")

        _LOGGER.debug("Login attempt: code=%s result=%s", code, result)

        # ── captcha ──────────────────────────────────────────────────────────
        if code == 87001 and data.get("captchaUrl"):
            captcha_url = "https://account.xiaomi.com" + data["captchaUrl"]
            # MUST fetch the captcha image so the server sets captchaIck cookie
            # in this session.  Without this GET the POST with captCode is
            # rejected as "Wrong captcha" even when the text is correct.
            try:
                self._session.get(captcha_url, timeout=10)
                _LOGGER.debug("Fetched captcha image — captchaIck cookie set")
            except Exception as fetch_exc:
                _LOGGER.warning("Could not prefetch captcha image: %s", fetch_exc)
            # Do NOT call _get_sign here — captcha challenge is tied to the
            # current session + sign that triggered it.  Calling serviceLogin
            # again would start a new server session, invalidating captchaIck
            # and causing every captcha submission to be rejected as "wrong".
            raise XiaomiCaptchaRequired(captcha_url, self._last_sign)

        # ── identity verification required ───────────────────────────────────
        if data.get("notificationUrl") and not data.get("location"):
            self._user_id          = str(data.get("userId", self._username))
            self._notification_url = data["notificationUrl"]
            # passToken: try JSON body first, fall back to Set-Cookie header
            self._pass_token = data.get("passToken", "") or next(
                (c.value for c in self._session.cookies if c.name == "passToken"), ""
            )
            # ssecurity may be included even when 2FA is required — save it as the
            # canonical account ssecurity (more reliable than extension-pragma).
            self._ssecurity_from_login = data.get("ssecurity", "")
            self._c_user_id_from_login = data.get("cUserId", "") or next(
                (c.value for c in self._session.cookies if c.name == "cUserId"), ""
            )
            _LOGGER.warning(
                "2FA triggered: ssecurity_from_login_present=%s  "
                "ssecurity_len=%d  passToken_len=%d  userId=%s  cUserId=%s",
                bool(self._ssecurity_from_login),
                len(self._ssecurity_from_login),
                len(self._pass_token),
                self._user_id,
                self._c_user_id_from_login,
            )
            # Parse context parameter — required for /identity/* endpoints
            try:
                qs = parse_qs(urlparse(self._notification_url).query)
                self._context = qs.get("context", [""])[0]
            except Exception:
                self._context = ""
            _LOGGER.debug(
                "notificationUrl received. context_present=%s  userId=%s",
                bool(self._context), self._user_id,
            )
            raise XiaomiApprovalRequired(
                data["notificationUrl"],
                self._last_sign,
            )

        # ── wrong credentials ─────────────────────────────────────────────────
        if result != "ok" or not data.get("location"):
            if code in (70016, 70017, 82001, 87001):
                raise XiaomiInvalidCredentials(f"Login rejected: code={code}")
            raise XiaomiLoginError(f"Login failed: code={code} result={result}")

        # ── success ───────────────────────────────────────────────────────────
        ssecurity     = data["ssecurity"]
        user_id       = str(data["userId"])
        c_user_id     = data.get("cUserId", "")
        service_token = _get_service_token(self._session, data["location"])

        return LoginResult(
            user_id=user_id,
            c_user_id=c_user_id,
            ssecurity=ssecurity,
            service_token=service_token,
        )


class LoginResult:
    """Successful login credentials."""

    def __init__(
        self,
        user_id: str,
        c_user_id: str,
        ssecurity: str,
        service_token: str,
        ssecurity_reliable: bool = True,
        pass_token: str = "",
    ) -> None:
        self.user_id            = user_id
        self.c_user_id          = c_user_id
        self.ssecurity          = ssecurity
        self.service_token      = service_token
        # False when ssecurity came from extension-pragma (session-specific,
        # not the stable account-level key). In re-auth, the existing stored
        # ssecurity should be kept instead of overwriting with this value.
        self.ssecurity_reliable = ssecurity_reliable
        # passToken — longer-lived than serviceToken, used for silent re-auth
        # (GET serviceLoginAuth2 with passToken bypasses 2FA since token was
        # already issued after a completed 2FA session).
        self.pass_token         = pass_token

    def to_dict(self) -> dict:
        return {
            "user_id":       self.user_id,
            "c_user_id":     self.c_user_id,
            "ssecurity":     self.ssecurity,
            "service_token": self.service_token,
        }
