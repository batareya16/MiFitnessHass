"""
Config flow for Mi Fitness integration.

Two setup paths:
  Path A — manual tokens: user supplies pre-obtained account credentials
  Path B — password login: user enters Xiaomi account credentials,
            the integration logs in automatically (handles captcha + phone approval)

Re-auth flow: triggered when serviceToken expires (HTTP 401).
  - Path B users: try silent re-login first (no user interaction needed)
  - Path A users: show form to enter new tokens
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.exceptions import HomeAssistantError

from .api import MiFitnessApiError, MiFitnessAuthError, MiFitnessClient
from .auth import (
    LoginResult,
    XiaomiApprovalRequired,
    XiaomiCaptchaRequired,
    XiaomiInvalidCredentials,
    XiaomiLoginError,
    XiaomiLoginSession,
)
from .const import (
    AUTH_METHOD_PASSWORD,
    AUTH_METHOD_TOKENS,
    CONF_AUTH_METHOD,
    CONF_C_USER_ID,
    CONF_PASS_TOKEN,
    CONF_PASSWORD,
    CONF_PHONE_ID,
    CONF_REGION,
    CONF_SERVICE_TOKEN,
    CONF_SSECURITY,
    CONF_USER_ID,
    CONF_USERNAME,
    DEFAULT_REGION,
    DOMAIN,
    REGIONS,
)

_LOGGER = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _test_tokens(hass, data: dict) -> None:
    """Validate token credentials with a real API call. Raises on failure."""
    client = MiFitnessClient(
        user_id=data[CONF_USER_ID],
        c_user_id=data[CONF_C_USER_ID],
        ssecurity=data[CONF_SSECURITY],
        service_token=data[CONF_SERVICE_TOKEN],
        region=data.get(CONF_REGION, DEFAULT_REGION),
    )
    await hass.async_add_executor_job(client.get_health_data, 0, 1)


# ── Config flow ───────────────────────────────────────────────────────────────

class MiFitnessConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Mi Fitness."""

    VERSION = 1

    def __init__(self) -> None:
        self._login_session: XiaomiLoginSession | None = None
        self._region = DEFAULT_REGION
        self._username = ""
        self._password = ""
        self._captcha_url = ""
        self._notification_url = ""
        self._approval_task: asyncio.Task | None = None
        self._approval_result: LoginResult | None = None
        self._approval_error: Exception | None = None
        self._override_ssecurity: str = ""

    # ── Step 1: choose auth method ────────────────────────────────────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show choice: manual tokens vs username/password."""
        if user_input is not None:
            method = user_input[CONF_AUTH_METHOD]
            self._region = user_input.get(CONF_REGION, DEFAULT_REGION)
            if method == AUTH_METHOD_PASSWORD:
                return await self.async_step_password()
            return await self.async_step_tokens()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Optional(CONF_REGION, default=DEFAULT_REGION): vol.In(REGIONS),
                vol.Required(CONF_AUTH_METHOD, default=AUTH_METHOD_PASSWORD): vol.In({
                    AUTH_METHOD_PASSWORD: "Password — Xiaomi account login",
                    AUTH_METHOD_TOKENS:   "Manual tokens — advanced",
                }),
            }),
        )

    # ── Path A: manual tokens ─────────────────────────────────────────────────

    async def async_step_tokens(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Path A: enter pre-obtained account tokens."""
        errors: dict[str, str] = {}

        if user_input is not None:
            data = {**user_input, CONF_REGION: self._region}
            try:
                await _test_tokens(self.hass, data)
            except MiFitnessAuthError:
                errors["base"] = "invalid_auth"
            except MiFitnessApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error validating tokens")
                errors["base"] = "unknown"
            else:
                entry_data = {
                    CONF_AUTH_METHOD:   AUTH_METHOD_TOKENS,
                    CONF_USER_ID:       data[CONF_USER_ID],
                    CONF_C_USER_ID:     data[CONF_C_USER_ID],
                    CONF_SSECURITY:     data[CONF_SSECURITY],
                    CONF_SERVICE_TOKEN: data[CONF_SERVICE_TOKEN],
                    CONF_REGION:        self._region,
                    CONF_PHONE_ID:      data.get(CONF_PHONE_ID, ""),
                }
                if data.get(CONF_PASS_TOKEN):
                    entry_data[CONF_PASS_TOKEN] = data[CONF_PASS_TOKEN]
                uid = data[CONF_USER_ID]
                await self.async_set_unique_id(uid)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Mi Fitness ({uid})",
                    data=entry_data,
                )

        return self.async_show_form(
            step_id="tokens",
            data_schema=vol.Schema({
                vol.Required(CONF_USER_ID): str,
                vol.Required(CONF_C_USER_ID): str,
                vol.Required(CONF_SSECURITY): str,
                vol.Required(CONF_SERVICE_TOKEN): str,
                vol.Optional(CONF_PASS_TOKEN, default=""): str,
                vol.Optional(CONF_PHONE_ID, default=""): str,
            }),
            errors=errors,
            description_placeholders={"region": self._region},
        )

    # ── Path B: username / password ───────────────────────────────────────────

    async def async_step_password(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step B1: enter Xiaomi username + password."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]
            self._login_session = XiaomiLoginSession()

            try:
                result: LoginResult = await self.hass.async_add_executor_job(
                    self._login_session.start,
                    self._username,
                    self._password,
                )
            except XiaomiCaptchaRequired as exc:
                self._captcha_url = exc.captcha_url
                return await self.async_step_captcha()
            except XiaomiApprovalRequired as exc:
                self._notification_url = exc.notification_url
                # Trigger email send immediately via our session
                try:
                    await self.hass.async_add_executor_job(
                        self._login_session.start_email_verification
                    )
                except Exception as e:
                    _LOGGER.warning("start_email_verification failed: %s", e)
                return await self.async_step_approval()
            except XiaomiInvalidCredentials:
                errors["base"] = "invalid_auth"
            except XiaomiLoginError as exc:
                _LOGGER.warning("Login error: %s", exc)
                errors["base"] = "login_failed"
            except Exception:
                _LOGGER.exception("Unexpected login error")
                errors["base"] = "unknown"
            else:
                return self._create_entry_from_login(result)

        return self.async_show_form(
            step_id="password",
            data_schema=vol.Schema({
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
            }),
            errors=errors,
        )

    async def async_step_captcha(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step B2: user enters captcha text shown at captcha_url."""
        errors: dict[str, str] = {}

        if user_input is not None:
            code = user_input.get("captcha_code", "").strip()
            try:
                result: LoginResult = await self.hass.async_add_executor_job(
                    self._login_session.submit_captcha, code
                )
            except XiaomiCaptchaRequired as exc:
                self._captcha_url = exc.captcha_url
                errors["captcha_code"] = "captcha_invalid"
            except XiaomiApprovalRequired as exc:
                self._notification_url = exc.notification_url
                return await self.async_step_approval()
            except (XiaomiInvalidCredentials, XiaomiLoginError) as exc:
                _LOGGER.warning("Login after captcha: %s", exc)
                errors["base"] = "login_failed"
            else:
                return self._create_entry_from_login(result)

        return self.async_show_form(
            step_id="captcha",
            data_schema=vol.Schema({vol.Required("captcha_code"): str}),
            errors=errors,
            description_placeholders={"captcha_url": self._captcha_url},
        )

    async def async_step_approval(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """
        Step B3: Xiaomi identity verification via email OTP.
        User enters code from email HERE — we submit it via our session.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            otp = (user_input.get("email_code") or "").strip()
            try:
                result: LoginResult = await self.hass.async_add_executor_job(
                    self._login_session.verify_with_code, otp
                )
            except XiaomiCaptchaRequired as exc:
                self._captcha_url = exc.captcha_url
                return await self.async_step_captcha()
            except XiaomiApprovalRequired as exc:
                self._notification_url = exc.notification_url
                errors["base"] = "approval_not_detected"
            except XiaomiLoginError as exc:
                _LOGGER.warning("Approval step error: %s", exc)
                errors["base"] = "login_failed"
            else:
                return self._create_entry_from_login(result)

        return self.async_show_form(
            step_id="approval",
            data_schema=vol.Schema({
                vol.Required("email_code"): str,
            }),
            errors=errors,
            description_placeholders={"notification_url": self._notification_url},
        )

    def _create_entry_from_login(self, result: LoginResult) -> ConfigFlowResult:
        # Use manually provided ssecurity if given (overrides extension-pragma).
        override = getattr(self, "_override_ssecurity", "").strip()
        ssecurity = override if override else result.ssecurity
        if override:
            _LOGGER.warning(
                "Initial setup: using manually provided ssecurity (len=%d)", len(override)
            )
            self._override_ssecurity = ""

        entry_data = {
            CONF_AUTH_METHOD:   AUTH_METHOD_PASSWORD,
            CONF_USER_ID:       result.user_id,
            CONF_C_USER_ID:     result.c_user_id,
            CONF_SSECURITY:     ssecurity,
            CONF_SERVICE_TOKEN: result.service_token,
            CONF_REGION:        self._region,
            CONF_USERNAME:      self._username,
            CONF_PASSWORD:      self._password,
        }
        if getattr(result, "pass_token", ""):
            entry_data[CONF_PASS_TOKEN] = result.pass_token
        return self.async_create_entry(
            title=f"Mi Fitness ({result.user_id})",
            data=entry_data,
        )

    # ── Re-auth flow ──────────────────────────────────────────────────────────

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """
        Called by HA when coordinator signals auth failure.
        Path B: attempt silent re-login first.
        Path A: go straight to interactive token form.
        """
        self._reauth_entry = self._get_reauth_entry()
        method = self._reauth_entry.data.get(CONF_AUTH_METHOD, AUTH_METHOD_TOKENS)

        if method == AUTH_METHOD_PASSWORD:
            username = self._reauth_entry.data.get(CONF_USERNAME, "")
            password = self._reauth_entry.data.get(CONF_PASSWORD, "")
            if username and password:
                self._username = username
                self._password = password
                ls = XiaomiLoginSession()
                self._login_session = ls
                try:
                    result: LoginResult = await self.hass.async_add_executor_job(
                        ls.start, username, password
                    )
                    # No 2FA — update tokens silently, user sees nothing
                    return await self._update_entry_tokens(result, username, password)

                except XiaomiApprovalRequired as exc:
                    # 2FA email required — go straight to code entry, skip credentials form
                    self._notification_url = exc.notification_url
                    try:
                        await self.hass.async_add_executor_job(
                            ls.start_email_verification
                        )
                    except Exception as e:
                        _LOGGER.warning("start_email_verification (reauth): %s", e)
                    return await self.async_step_reauth_approval()

                except XiaomiCaptchaRequired as exc:
                    self._captcha_url = exc.captcha_url
                    return await self.async_step_reauth_captcha()

                except XiaomiLoginError:
                    pass  # Stored credentials invalid — fall through to manual form

        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Interactive re-auth form (Path A tokens, or Path B with bad password)."""
        errors: dict[str, str] = {}
        entry = self._reauth_entry
        method = entry.data.get(CONF_AUTH_METHOD, AUTH_METHOD_TOKENS)

        if user_input is not None:
            if method == AUTH_METHOD_TOKENS:
                new_data = {
                    **entry.data,
                    CONF_SSECURITY:     user_input[CONF_SSECURITY],
                    CONF_SERVICE_TOKEN: user_input[CONF_SERVICE_TOKEN],
                }
                try:
                    await _test_tokens(self.hass, new_data)
                except (MiFitnessAuthError, MiFitnessApiError):
                    errors["base"] = "invalid_auth"
                else:
                    self.hass.config_entries.async_update_entry(entry, data=new_data)
                    await self.hass.config_entries.async_reload(entry.entry_id)
                    return self.async_abort(reason="reauth_successful")

            else:
                self._username = user_input[CONF_USERNAME]
                self._password = user_input[CONF_PASSWORD]
                self._login_session = XiaomiLoginSession()
                try:
                    result: LoginResult = await self.hass.async_add_executor_job(
                        self._login_session.start, self._username, self._password
                    )
                except XiaomiCaptchaRequired as exc:
                    self._captcha_url = exc.captcha_url
                    return await self.async_step_reauth_captcha()
                except XiaomiApprovalRequired as exc:
                    self._notification_url = exc.notification_url
                    try:
                        await self.hass.async_add_executor_job(
                            self._login_session.start_email_verification
                        )
                    except Exception as e:
                        _LOGGER.warning("start_email_verification (reauth): %s", e)
                    return await self.async_step_reauth_approval()
                except XiaomiInvalidCredentials:
                    errors["base"] = "invalid_auth"
                except XiaomiLoginError:
                    errors["base"] = "login_failed"
                else:
                    return await self._update_entry_tokens(
                        result, self._username, self._password
                    )

        if method == AUTH_METHOD_TOKENS:
            schema = vol.Schema({
                vol.Required(CONF_SSECURITY): str,
                vol.Required(CONF_SERVICE_TOKEN): str,
            })
        else:
            schema = vol.Schema({
                vol.Required(CONF_USERNAME, default=entry.data.get(CONF_USERNAME, "")): str,
                vol.Required(CONF_PASSWORD): str,
            })

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_reauth_captcha(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                result: LoginResult = await self.hass.async_add_executor_job(
                    self._login_session.submit_captcha,
                    user_input.get("captcha_code", "").strip(),
                )
            except XiaomiCaptchaRequired as exc:
                self._captcha_url = exc.captcha_url
                errors["captcha_code"] = "captcha_invalid"
            except XiaomiLoginError:
                errors["base"] = "login_failed"
            else:
                return await self._update_entry_tokens(
                    result, self._username, self._password
                )

        return self.async_show_form(
            step_id="reauth_captcha",
            data_schema=vol.Schema({vol.Required("captcha_code"): str}),
            errors=errors,
            description_placeholders={"captcha_url": self._captcha_url},
        )

    async def async_step_reauth_approval(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-auth email OTP step — mirrors async_step_approval."""
        errors: dict[str, str] = {}
        if user_input is not None:
            otp = (user_input.get("email_code") or "").strip()
            try:
                result: LoginResult = await self.hass.async_add_executor_job(
                    self._login_session.verify_with_code, otp
                )
            except XiaomiApprovalRequired as exc:
                self._notification_url = exc.notification_url
                errors["base"] = "approval_not_detected"
            except XiaomiLoginError as exc:
                _LOGGER.warning("Reauth approval error: %s", exc)
                errors["base"] = "login_failed"
            else:
                return await self._update_entry_tokens(
                    result, self._username, self._password
                )

        return self.async_show_form(
            step_id="reauth_approval",
            data_schema=vol.Schema({vol.Required("email_code"): str}),
            errors=errors,
            description_placeholders={"notification_url": self._notification_url},
        )

    async def _update_entry_tokens(
        self,
        result: LoginResult,
        username: str,
        password: str,
    ) -> ConfigFlowResult:
        """Update existing entry with fresh tokens and reload."""
        entry = self._reauth_entry

        # Priority order for ssecurity:
        #  1. _override_ssecurity — user explicitly pasted it in (most trusted)
        #  2. result.ssecurity when ssecurity_reliable=True — direct serviceLoginAuth2 success
        #  3. existing stored ssecurity — when result is from extension-pragma (unreliable)
        #  4. result.ssecurity as last resort — no other option
        override = getattr(self, "_override_ssecurity", "").strip()
        if override:
            ssecurity_to_store = override
            _LOGGER.warning(
                "Using manually provided ssecurity (len=%d) — overrides login result",
                len(override),
            )
            self._override_ssecurity = ""  # consume once
        elif not getattr(result, "ssecurity_reliable", True):
            stored_ssecurity = entry.data.get(CONF_SSECURITY, "")
            if stored_ssecurity:
                _LOGGER.warning(
                    "2FA re-auth: ssecurity unreliable (extension-pragma, len=%d); "
                    "keeping stored ssecurity (len=%d) — only updating serviceToken",
                    len(result.ssecurity), len(stored_ssecurity),
                )
                ssecurity_to_store = stored_ssecurity
            else:
                _LOGGER.warning(
                    "2FA re-auth: ssecurity unreliable but no stored ssecurity to fall back to "
                    "(len=%d) — using extension-pragma value; API may return 401",
                    len(result.ssecurity),
                )
                ssecurity_to_store = result.ssecurity
        else:
            ssecurity_to_store = result.ssecurity

        # Store passToken when present — used for silent serviceToken refresh
        # without requiring 2FA (see coordinator.py + auth.try_silent_token_refresh).
        new_pass_token = getattr(result, "pass_token", "")

        new_data = {
            **entry.data,
            CONF_SSECURITY:     ssecurity_to_store,
            CONF_SERVICE_TOKEN: result.service_token,
            CONF_USER_ID:       result.user_id,
            CONF_C_USER_ID:     result.c_user_id,
            CONF_USERNAME:      username,
            CONF_PASSWORD:      password,
        }
        if new_pass_token:
            new_data[CONF_PASS_TOKEN] = new_pass_token
        self.hass.config_entries.async_update_entry(entry, data=new_data)
        await self.hass.config_entries.async_reload(entry.entry_id)
        return self.async_abort(reason="reauth_successful")
