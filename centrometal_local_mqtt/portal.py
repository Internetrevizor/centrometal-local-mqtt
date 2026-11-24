"""The Centrometal portal (web-boiler.com), for cloud_proxy: read requests and commands go through the owner's account, the
same requests the portal's own web application sends. The portal passes them to the module, signed, over the connection
this app proxies, so the module's answers arrive here as in local mode.

Standard library only, and blocking: the gateway calls it from a thread. Never logs the e-mail, the password, the
installation or the serial.
"""
from __future__ import annotations

import http.cookiejar
import json
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

WEBROOT = "https://www.web-boiler.com"
TIMEOUT_SECONDS = 20
LOGIN_FORM_MARKER = '<form action="/login_check"'  # the portal answers an expired session with its login page
LOADING_DIV_ID = "id-loading-screen-blackout"  # rendered only after a successful login
CIRCUIT_SLOTS = frozenset((272,))  # a heating circuit is switched with the portal's circuit request, not the settings one


class PortalError(Exception):
    """The portal could not be reached, or answered something unexpected."""


class PortalAuthError(PortalError):
    """The portal refused the e-mail and password."""


class _Expired(Exception):
    pass


class _LoginPage(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.csrf: str | None = None
        self.logged_in = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "input" and values.get("name") == "_csrf_token" and values.get("value") and self.csrf is None:
            self.csrf = values["value"]
        elif tag == "div" and values.get("id") == LOADING_DIV_ID:
            self.logged_in = True


def _parse(page: str) -> _LoginPage:
    parser = _LoginPage()
    parser.feed(page)
    parser.close()
    return parser


def accepted(answer: Any) -> bool:
    """The portal's way of saying it took a command."""
    if answer is True:
        return True
    if not isinstance(answer, dict) or answer.get("success") is False or answer.get("ok") is False:
        return False
    status = answer.get("status")
    if isinstance(status, str):
        return status.lower() in ("success", "ok", "done")
    return answer.get("success") is True or answer.get("ok") is True


class Portal:
    def __init__(self, email: str, password: str, webroot: str = WEBROOT, timeout: float = TIMEOUT_SECONDS) -> None:
        self._email = email
        self._password = password
        self.webroot = webroot.rstrip("/")
        self.timeout = timeout
        self.installation: Any = None
        self._lock = threading.Lock()  # one request at a time: a cancelled caller leaves its thread running
        self._new_session()

    def _new_session(self) -> None:
        self.logged_in = False
        handlers: list[urllib.request.BaseHandler] = [urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())]
        if self.webroot.startswith("https:"):
            handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        self._opener = urllib.request.build_opener(*handlers)

    def __repr__(self) -> str:
        return "Portal(<account>)"  # nothing about the account in a traceback

    def _send(self, path: str, data: bytes | None = None, content_type: str | None = None) -> str:
        headers = {"Origin": self.webroot, "Referer": self.webroot + "/"}
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(self.webroot + path, data=data, headers=headers, method="POST" if data is not None else "GET")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise PortalError(f"HTTP {response.status}")
                return response.read(1_000_000).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise PortalError(f"HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise PortalError(type(exc).__name__) from None

    def login(self) -> None:
        self._new_session()
        csrf = _parse(self._send("/login")).csrf
        if not csrf:
            raise PortalError("no login form")
        form = urllib.parse.urlencode({"_csrf_token": csrf, "_username": self._email, "_password": self._password,
                                       "submit": "Log In"}).encode()
        page = self._send("/login_check", form, "application/x-www-form-urlencoded")
        if _parse(page).logged_in:
            self.logged_in = True
            return
        if LOGIN_FORM_MARKER in page:
            raise PortalAuthError("login refused")
        raise PortalError("unexpected login answer")

    def _json(self, path: str, body: dict[str, Any]) -> Any:
        text = self._send(path, json.dumps(body).encode(), "application/json;charset=UTF-8")
        if LOGIN_FORM_MARKER in text:
            raise _Expired
        try:
            return json.loads(text)
        except ValueError:
            raise PortalError("not JSON") from None

    def _find_installation(self, serial: str | None) -> None:
        listed = self._json("/data/autocomplete/installation", {})
        installations = listed.get("installations") if isinstance(listed, dict) else None
        if not isinstance(installations, list):
            raise PortalError("no installation list")
        found = [i for i in installations if isinstance(i, dict) and serial is not None and str(i.get("label")) == serial]
        if not found and len(installations) == 1 and isinstance(installations[0], dict):
            found = installations  # one boiler on the account: that is the one
        if not found or found[0].get("value") is None:
            raise PortalError("boiler not found on the account")
        self.installation = found[0]["value"]

    def _call(self, serial: str | None, path: str, body: Any) -> Any:
        """Logs in and finds the boiler first if needed. An expired session is renewed once."""
        for attempt in range(2):
            try:
                if not self.logged_in:
                    self.login()
                if self.installation is None:
                    self._find_installation(serial)
                return self._json(path.format(id=self.installation), body(self.installation))
            except _Expired:
                self.logged_in = False
                if attempt:
                    raise PortalError("session expired again right after login") from None
        return None

    def request(self, serial: str | None, message: dict[str, Any]) -> Any:
        """One message as LocalSession would publish it to the module: REFRESH, RSTAT, PRD, CMD or PWR."""
        with self._lock:
            return self._request(serial, message)

    def _request(self, serial: str | None, message: dict[str, Any]) -> Any:
        (name, value), = message.items()
        if name in ("REFRESH", "RSTAT"):
            return self._call(serial, "/api/inst/control/multiple", lambda i: {"messages": {str(i): {name: value}}})
        if name.startswith("PRD "):
            return self._call(serial, "/api/inst/control/advanced/{id}", lambda i: {"parameters": {name: value}})
        if name != "CMD" and not name.startswith("PWR "):
            raise ValueError("not a portal request")
        if name.startswith("PWR ") and int(name[4:]) in CIRCUIT_SLOTS:
            return self._call(serial, "/api/inst/control/multiple", lambda i: {"messages": {str(i): {name: value}}})
        return self._call(serial, "/api/inst/control/{id}", lambda i: {"cmd-name": name, "cmd-value": value})
