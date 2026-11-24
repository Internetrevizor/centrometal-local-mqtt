"""cloud_proxy with a portal account: reads and commands through a local imitation of web-boiler.com, on loopback only.
Run with: python -B -m unittest discover -s tests -v"""
import asyncio
import http.server
import json
from pathlib import Path
import sys
import threading
import unittest
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components/centrometal_local"))
sys.path.insert(0, str(ROOT / "centrometal_local_mqtt"))
sys.path.insert(0, str(ROOT / "tests"))
import gateway as g  # noqa: E402
import portal as p  # noqa: E402
import test_gateway as tg  # noqa: E402  (the module, not its classes: their tests must not run twice)
from test_gateway import connect, next_frame, up  # noqa: E402

EMAIL = "owner@example.com"
PASSWORD = "portal-pass"
LOGIN_PAGE = ('<html><form action="/login_check" method="post"><input type="hidden" name="_csrf_token" value="csrf-1">'
              '</form></html>')


class FakePortal:
    """The few pages of web-boiler.com the app uses, with a session cookie as the real one."""

    def __init__(self, installations=None):
        self.installations = installations if installations is not None else [
            {"value": 1001, "label": "OTHER1", "type": "biopl"}, {"value": 1002, "label": "BOXID", "type": "peltec2"}]
        self.requests = []
        self.logins = 0
        self.expire_next = False
        portal = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self, text, cookie=None, content_type="text/html"):
                body = text.encode()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._answer(LOGIN_PAGE, "PHPSESSID=anonymous; Path=/")

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path == "/login_check":
                    form = urllib.parse.parse_qs(body.decode())
                    portal.logins += 1
                    if (form.get("_csrf_token") == ["csrf-1"] and form.get("_username") == [EMAIL]
                            and form.get("_password") == [PASSWORD]):
                        self._answer('<div id="id-loading-screen-blackout"></div>', "PHPSESSID=good; Path=/")
                    else:
                        self._answer(LOGIN_PAGE)
                    return
                if "PHPSESSID=good" not in (self.headers.get("Cookie") or "") or portal.expire_next:
                    portal.expire_next = False
                    self._answer(LOGIN_PAGE, "PHPSESSID=anonymous; Path=/")
                    return
                if self.path == "/data/autocomplete/installation":
                    self._answer(json.dumps({"installations": portal.installations}), content_type="application/json")
                    return
                portal.requests.append((self.path, json.loads(body)))
                self._answer('{"status":"success"}', content_type="application/json")

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.webroot = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def names(self):
        """The request name of each call: REFRESH, CMD, PWR 67, PRD 67..."""
        result = []
        for _, body in self.requests:
            if "cmd-name" in body:
                result.append(body["cmd-name"])
            else:
                inner = body.get("parameters") or next(iter(body["messages"].values()))
                result.extend(inner)
        return result


class PortalClientTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakePortal()
        self.addCleanup(self.fake.close)
        self.portal = p.Portal(EMAIL, PASSWORD, self.fake.webroot)

    def test_each_request_is_the_portals_own(self):
        for message in ({"REFRESH": 0}, {"RSTAT": "ALL"}, {"CMD": 1}, {"PWR 67": 66}, {"PWR 272": 0}, {"PRD 67": "VAL"}):
            self.assertTrue(p.accepted(self.portal.request("BOXID", message)), message)
        self.assertEqual(self.fake.requests, [
            ("/api/inst/control/multiple", {"messages": {"1002": {"REFRESH": 0}}}),
            ("/api/inst/control/multiple", {"messages": {"1002": {"RSTAT": "ALL"}}}),
            ("/api/inst/control/1002", {"cmd-name": "CMD", "cmd-value": 1}),
            ("/api/inst/control/1002", {"cmd-name": "PWR 67", "cmd-value": 66}),
            ("/api/inst/control/multiple", {"messages": {"1002": {"PWR 272": 0}}}),
            ("/api/inst/control/advanced/1002", {"parameters": {"PRD 67": "VAL"}}),
        ])
        self.assertEqual(self.fake.logins, 1)

    def test_an_expired_session_is_renewed_once(self):
        self.portal.request("BOXID", {"REFRESH": 0})
        self.fake.expire_next = True
        self.portal.request("BOXID", {"REFRESH": 0})
        self.assertEqual((self.fake.logins, len(self.fake.requests)), (2, 2))

    def test_a_wrong_password_is_an_auth_error(self):
        with self.assertRaises(p.PortalAuthError):
            p.Portal(EMAIL, "wrong", self.fake.webroot).request("BOXID", {"REFRESH": 0})
        self.assertEqual(self.fake.requests, [])

    def test_the_boiler_is_found_by_serial_or_as_the_only_one(self):
        with self.assertRaises(p.PortalError):
            self.portal.request("NOTHERE", {"REFRESH": 0})
        self.fake.installations = [{"value": 7, "label": "ANY"}]
        self.portal.request("NOTHERE", {"REFRESH": 0})
        self.assertEqual(self.fake.requests[-1][1], {"messages": {"7": {"REFRESH": 0}}})

    def test_nothing_else_can_be_sent(self):
        for message in ({"SCCMD": 1}, {"PWR": 1}, {"_sync_ACK": "ok"}):
            with self.assertRaises(ValueError):
                self.portal.request("BOXID", message)
        self.assertNotIn(EMAIL, repr(self.portal))
        self.assertNotIn(PASSWORD, repr(self.portal))

    def test_an_unreachable_portal_is_a_portal_error(self):
        self.fake.close()
        with self.assertRaises(p.PortalError):
            p.Portal(EMAIL, PASSWORD, self.fake.webroot, timeout=1).request("BOXID", {"REFRESH": 0})
        self.fake = FakePortal()

    def test_the_answer_that_means_accepted(self):
        self.assertTrue(p.accepted({"status": "success"}))
        self.assertTrue(p.accepted(True))
        for answer in ({"status": "error"}, {"success": False, "status": "ok"}, None, []):
            self.assertFalse(p.accepted(answer), answer)


class ProxyThroughPortalTests(tg.Base):
    api = tg.CommandTests.api
    post = tg.CommandTests.post

    async def proxy(self, password=PASSWORD, product="peltec2"):
        self.fake = FakePortal()
        self.addCleanup(self.fake.close)
        _, cloud_port = await self.cloud()
        gateway, port = await self.start(connection_mode="cloud_proxy", upstream_port=cloud_port, first_poll_delay_seconds=0,
                                         portal_email=EMAIL, portal_password=password, portal_webroot=self.fake.webroot)
        reader, writer = await self.module(port)
        writer.write(connect())
        await writer.drain()
        await next_frame(reader)
        writer.write(g.pub_frame(f"cm.inst.{product}.BOXID", g.sign_obj({"B_CMD": 0, "B_Tak1_1": 55, "clMsgId": 1}, tg.KEY)))
        await writer.drain()
        return gateway, writer

    async def until(self, check, seconds=5):
        for _ in range(int(seconds / .05)):
            if check():
                return
            await asyncio.sleep(.05)
        self.fail("condition not met in time")

    async def test_readings_are_asked_for_through_the_portal(self):
        gateway, _ = await self.proxy()
        await self.until(lambda: "REFRESH" in self.fake.names())
        await self.until(lambda: "RSTAT" in self.fake.names() and "PRD 67" in self.fake.names())
        status = gateway.status()
        self.assertEqual((status["portal"], status["commands_available"]), ("ok", True))
        self.assertEqual(status["connection_mode"], "cloud_proxy")

    async def test_a_command_goes_through_the_portal_and_the_boiler_confirms_it(self):
        gateway, writer = await self.proxy()
        await self.until(lambda: gateway.status()["commands_available"])
        self.assertIn(b"200 OK", await self.api(gateway, self.post(b'{"CMD":1}')))
        await self.until(lambda: "CMD" in self.fake.names())
        self.assertEqual(gateway.commands.summary()["power"]["state"], "pending")
        writer.write(up({"B_CMD": 1, "clMsgId": 2}))
        await writer.drain()
        await self.until(lambda: gateway.commands.summary()["power"]["state"] == "confirmed")
        self.assertEqual(self.fake.names().count("CMD"), 1)

    async def test_a_setting_is_written_through_the_portal_and_read_back(self):
        gateway, writer = await self.proxy()
        await self.until(lambda: gateway.status()["commands_available"])
        body = b'{"slot":67,"value":66}'
        self.assertIn(b"200 OK", await self.api(gateway, b"POST /setting HTTP/1.1\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body)))
        await self.until(lambda: "PWR 67" in self.fake.names())
        await self.until(lambda: self.fake.names()[self.fake.names().index("PWR 67"):].count("PRD 67") >= 1)
        writer.write(up({"PVAL_67_0": 66, "clMsgId": 2}))
        await writer.drain()
        await self.until(lambda: gateway.commands.summary()["setting_67"]["state"] == "confirmed")

    async def test_a_refused_login_takes_commands_away_and_is_not_repeated(self):
        gateway, _ = await self.proxy(password="wrong")
        await self.until(lambda: (gateway.status()["portal"] or "").startswith("login refused"))
        self.assertFalse(gateway.status()["commands_available"])
        self.assertIn(b"409", await self.api(gateway, self.post(b'{"CMD":1}')))
        logins = self.fake.logins
        await asyncio.sleep(1.5)
        self.assertEqual(self.fake.logins, logins)
        self.assertEqual(self.fake.requests, [])

    async def test_without_an_account_the_proxy_sends_nothing(self):
        _, cloud_port = await self.cloud()
        gateway, port = await self.start(connection_mode="cloud_proxy", upstream_port=cloud_port, first_poll_delay_seconds=0)
        reader, writer = await self.module(port)
        writer.write(connect() + up({"B_CMD": 0, "clMsgId": 1}))
        await writer.drain()
        await next_frame(reader)
        await asyncio.sleep(.5)
        status = gateway.status()
        self.assertEqual((status["portal"], status["commands_available"]), (None, False))
        self.assertEqual(gateway.state.status_requests_sent, 0)

    async def test_a_boiler_other_than_the_peltec_ii_is_only_read(self):
        gateway, _ = await self.proxy(product="biopl")
        await self.until(lambda: "REFRESH" in self.fake.names())
        await asyncio.sleep(.5)
        self.assertNotIn("PRD 67", self.fake.names())  # the settings table is the PelTec II's
        self.assertFalse(gateway.status()["commands_available"])
        self.assertIn(b"409", await self.api(gateway, self.post(b'{"CMD":1}')))


class ConfigTests(unittest.TestCase):
    def load(self, **options):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({"module_ip": "192.0.2.10", **options}, handle)
        try:
            return g.load_config(handle.name)
        finally:
            Path(handle.name).unlink()

    def test_the_account_is_both_or_neither(self):
        self.assertEqual(self.load()["portal_email"], "")
        self.assertEqual(self.load(portal_email=" a@b.c ", portal_password="x")["portal_email"], "a@b.c")
        for options in ({"portal_email": "a@b.c"}, {"portal_password": "x"}):
            with self.assertRaises(ValueError):
                self.load(**options)

    def test_local_mode_never_uses_the_portal(self):
        cfg = self.load(portal_email="a@b.c", portal_password="x")
        cfg["api_token"] = "t"
        self.assertIsNone(g.Gateway(cfg).portal)
        cfg["connection_mode"] = "cloud_proxy"
        self.assertIsNotNone(g.Gateway(cfg).portal)


class IntegrationFlagTests(unittest.TestCase):
    def test_the_app_decides_and_an_older_app_keeps_the_old_rule(self):
        import parameters
        fresh = {"data_fresh": True, "product": "peltec2"}
        self.assertTrue(parameters.commands_available({**fresh, "connection_mode": "cloud_proxy", "commands_available": True}))
        self.assertFalse(parameters.commands_available({**fresh, "connection_mode": "cloud_proxy", "commands_available": False}))
        self.assertFalse(parameters.commands_available({**fresh, "connection_mode": "cloud_proxy"}))
        self.assertTrue(parameters.commands_available({**fresh, "connection_mode": "local"}))
        self.assertFalse(parameters.commands_available({"data_fresh": False, "product": "peltec2", "commands_available": True}))


if __name__ == "__main__":
    unittest.main()
