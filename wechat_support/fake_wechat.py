"""A stand-in for WeChat, for the tests and the local run. Two halves:

FakeWeChatAPI answers the api.weixin.qq.com calls this service makes, with WeChat's JSON
shapes and error codes:
  /cgi-bin/token           classic token: a new one each call; the old one keeps working for
                           5 minutes; 2,000 fetches a day (200 on a test account), then 45009
  /cgi-bin/stable_token    stable token: the same token until 5 minutes before expiry;
                           force_refresh makes a new one and kills the old (20 a day, 30 s apart)
  /cgi-bin/message/custom/send    customer-service message: 40001/40014/42001 for a bad or
                           expired token, 40003 unknown openid, 45015 outside 48 hours or
                           unfollowed, 45047 over the per-message budget (5), 45002 too long,
                           44002 empty body, 47001 bad JSON
  /cgi-bin/message/custom/typing, /cgi-bin/get_api_domain_ip
Errors can be injected per path (for example -1 "system busy" twice, then success).

FakeWeChatPush plays WeChat's push servers: it builds the XML a follower's message turns into,
signs it (and encrypts it in safe or compatible mode), POSTs it to the callback with a
5-second timeout, retries on timeout (three attempts in all), and checks the reply the way
WeChat does: "success" or empty means no reply; anything else must be valid XML addressed to
that follower (encrypted and correctly signed in safe mode), or the follower would see
WeChat's "this account cannot provide service" notice. Every reply that reaches the follower
lands in their chat, in order, with how it arrived.

Where WeChat's documentation does not say (the exact error for a too-long text, and whether
the classic overlap is exactly 5 minutes), the choice made here is marked "assumed"."""
import asyncio
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .crypto import MessageCrypto, sha1_signature
from .xmlmsg import build_message, parse_xml

DAY = 86400


@dataclass
class Follower:
    openid: str
    subscribed: bool = True
    last_inbound: float = 0.0
    cs_since_inbound: int = 0
    chat: List[dict] = field(default_factory=list)   # what the follower sees, in order


class FakeWeChatAPI:
    def __init__(self, appid: str, secret: str, original_id: str = "gh_fake0a1b2c3d", test_account: bool = True,
                 clock: Callable[[], float] = time.time, cs_per_window: int = 5, max_text_bytes: int = 2048):
        self.appid, self.secret, self.original_id, self.clock = appid, secret, original_id, clock
        self.token_daily_limit = 200 if test_account else 2000
        self.cs_per_window, self.max_text_bytes = cs_per_window, max_text_bytes
        self.followers: Dict[str, Follower] = {}
        self.classic: Dict[str, float] = {}       # token -> expires_at
        self.stable: Optional[tuple] = None        # (token, expires_at)
        self.token_calls = 0
        self.force_calls: List[float] = []
        self.inject: Dict[str, List[int]] = {}    # path -> errcodes to return first
        self.requests: List[dict] = []            # every API call made, for the tests
        self.send_delay = 0.0                     # slow WeChat down (seconds per send)
        self.app = self._build_app()

    # ---- followers ----------------------------------------------------------------
    def follower(self, openid: str) -> Follower:
        if openid not in self.followers:
            self.followers[openid] = Follower(openid)
        return self.followers[openid]

    def new_openid(self) -> str:
        return "o" + secrets.token_urlsafe(20)[:27]

    # ---- tokens -------------------------------------------------------------------
    def _new_token(self) -> str:
        return "FAKE_" + secrets.token_urlsafe(48)

    def token_status(self, token: str) -> int:
        now = self.clock()
        if self.stable and token == self.stable[0]:
            return 0 if now < self.stable[1] else 42001
        if token in self.classic:
            return 0 if now < self.classic[token] else 42001
        return 40001

    def expire_all_tokens(self) -> None:
        """Make every token WeChat has handed out expire now (as two hours passing would)."""
        now = self.clock()
        self.classic = {t: now - 1 for t in self.classic}
        if self.stable:
            self.stable = (self.stable[0], now - 1)

    def _check_app(self, appid: str, secret: str) -> Optional[dict]:
        if appid != self.appid:
            return {"errcode": 40013, "errmsg": "invalid appid"}
        if secret != self.secret:
            return {"errcode": 40125, "errmsg": "invalid appsecret"}
        if self.token_calls >= self.token_daily_limit:
            return {"errcode": 45009, "errmsg": "reach max api daily quota limit"}
        return None

    def _issue_classic(self) -> dict:
        now = self.clock()
        for t, exp in list(self.classic.items()):
            self.classic[t] = min(exp, now + 300)   # assumed: old token valid 5 more minutes
        token = self._new_token()
        self.classic[token] = now + 7200
        return {"access_token": token, "expires_in": 7200}

    def _issue_stable(self, force: bool) -> dict:
        now = self.clock()
        if force:
            recent = [t for t in self.force_calls if now - t < DAY]
            if len(recent) >= 20:
                return {"errcode": 45009, "errmsg": "reach max api daily quota limit (force_refresh)"}
            if recent and now - recent[-1] < 30:
                return {"errcode": 45011, "errmsg": "api minute-quota reach limit (force_refresh 30s)"}
            self.force_calls.append(now)
            self.stable = None
        if self.stable is None or self.stable[1] - now <= 300:
            self.stable = (self._new_token(), now + 7200)
        return {"access_token": self.stable[0], "expires_in": int(self.stable[1] - now)}

    # ---- the app ------------------------------------------------------------------
    def _injected(self, path: str) -> Optional[dict]:
        queue = self.inject.get(path)
        if queue:
            code = queue.pop(0)
            return {"errcode": code, "errmsg": "injected"}
        return None

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/cgi-bin/token")
        async def token(grant_type: str = "", appid: str = "", secret: str = ""):
            self.requests.append({"path": "/cgi-bin/token", "appid": appid})
            if grant_type != "client_credential":
                return {"errcode": 40002, "errmsg": "invalid grant_type"}
            err = self._check_app(appid, secret) or self._injected("/cgi-bin/token")
            if err:
                return err
            self.token_calls += 1
            return self._issue_classic()

        @app.post("/cgi-bin/stable_token")
        async def stable_token(request: Request):
            body = await self._json(request)
            if isinstance(body, JSONResponse):
                return body
            self.requests.append({"path": "/cgi-bin/stable_token", "force": bool(body.get("force_refresh"))})
            if body.get("grant_type") != "client_credential":
                return {"errcode": 40002, "errmsg": "invalid grant_type"}
            err = self._check_app(body.get("appid", ""), body.get("secret", "")) or self._injected("/cgi-bin/stable_token")
            if err:
                return err
            self.token_calls += 1
            return self._issue_stable(bool(body.get("force_refresh")))

        @app.post("/cgi-bin/message/custom/send")
        async def custom_send(request: Request, access_token: str = ""):
            if self.send_delay:
                await asyncio.sleep(self.send_delay)
            return await self._authed(request, access_token, "/cgi-bin/message/custom/send", self._send)

        @app.post("/cgi-bin/message/custom/typing")
        async def typing(request: Request, access_token: str = ""):
            return await self._authed(request, access_token, "/cgi-bin/message/custom/typing", self._typing)

        @app.get("/cgi-bin/get_api_domain_ip")
        async def domain_ip(access_token: str = ""):
            code = self.token_status(access_token)
            self.requests.append({"path": "/cgi-bin/get_api_domain_ip", "token_ok": code == 0})
            if code:
                return {"errcode": code, "errmsg": "invalid credential" if code == 40001 else "access_token expired"}
            return {"ip_list": ["101.226.103.0/25", "101.226.233.128/25"]}

        return app

    async def _json(self, request: Request):
        raw = await request.body()
        if not raw.strip():
            return JSONResponse({"errcode": 44002, "errmsg": "empty post data"})
        try:
            return json.loads(raw)
        except ValueError:
            return JSONResponse({"errcode": 47001, "errmsg": "data format error"})

    async def _authed(self, request: Request, access_token: str, path: str, handler):
        code = self.token_status(access_token)
        body = await self._json(request)
        entry = {"path": path, "token_ok": code == 0, "body": body if isinstance(body, dict) else None}
        self.requests.append(entry)
        if code:
            return {"errcode": code, "errmsg": "invalid credential, access_token is invalid or not latest"
                    if code == 40001 else "access_token expired"}
        if isinstance(body, JSONResponse):
            return body
        injected = self._injected(path)
        if injected:
            entry["injected"] = injected["errcode"]
            return injected
        return handler(body)

    def _send(self, body: dict) -> dict:
        openid = body.get("touser", "")
        if body.get("msgtype") != "text" or not isinstance(body.get("text"), dict):
            return {"errcode": 47001, "errmsg": "data format error"}
        content = body["text"].get("content", "")
        if not content:
            return {"errcode": 44003, "errmsg": "empty news data"}
        if len(content.encode()) > self.max_text_bytes:
            return {"errcode": 45002, "errmsg": "content size out of limit"}   # assumed limit
        f = self.followers.get(openid)
        if not f:
            return {"errcode": 40003, "errmsg": "invalid openid"}
        if not f.subscribed or self.clock() - f.last_inbound > 48 * 3600:
            return {"errcode": 45015, "errmsg": "response out of time limit or subscription is canceled"}
        if f.cs_since_inbound >= self.cs_per_window:
            return {"errcode": 45047, "errmsg": "out of response count limit"}
        f.cs_since_inbound += 1
        f.chat.append({"from": "account", "via": "customer_service", "text": content, "at": self.clock()})
        return {"errcode": 0, "errmsg": "ok"}

    def _typing(self, body: dict) -> dict:
        f = self.followers.get(body.get("touser", ""))
        if not f:
            return {"errcode": 40003, "errmsg": "invalid openid"}
        if body.get("command") not in ("Typing", "CancelTyping"):
            return {"errcode": 47001, "errmsg": "data format error"}
        return {"errcode": 0, "errmsg": "ok"}


@dataclass
class Delivery:
    attempts: int
    outcome: str            # passive | no_reply | timeout | error
    reply_text: str = ""
    reply_type: str = ""
    statuses: List[int] = field(default_factory=list)
    seconds: float = 0.0
    error: str = ""


class FakeWeChatPush:
    """Sends follower messages to the callback the way WeChat's servers do."""

    def __init__(self, api: FakeWeChatAPI, client: httpx.AsyncClient, callback_url: str, token: str,
                 mode: str = "plaintext", aes_key: Optional[str] = None, timeout: float = 5.0, attempts: int = 3):
        if mode not in ("plaintext", "compatible", "safe"):
            raise ValueError("mode is plaintext, compatible or safe")
        self.api, self.client, self.url, self.token = api, client, callback_url, token
        self.mode, self.timeout, self.attempts = mode, timeout, attempts
        self.crypto = MessageCrypto(token, aes_key, api.appid) if mode != "plaintext" else None
        self._msg_id = 24000000000000000 + secrets.randbelow(10**15)

    def next_msg_id(self) -> str:
        self._msg_id += 1
        return str(self._msg_id)

    async def verify_url(self) -> httpx.Response:
        ts, nonce, echostr = str(int(self.api.clock())), str(secrets.randbelow(10**9)), secrets.token_hex(8)
        sig = sha1_signature(self.token, ts, nonce)
        r = await self.client.get(self.url, params={"signature": sig, "timestamp": ts, "nonce": nonce, "echostr": echostr})
        r.expected = echostr   # type: ignore[attr-defined]
        return r

    async def say(self, openid: str, text: str, msg_type: str = "text", event: str = "",
                  msg_id: Optional[str] = None) -> Delivery:
        f = self.api.follower(openid)
        now = self.api.clock()
        if msg_type == "event":
            if event == "subscribe":
                f.subscribed = True
            elif event == "unsubscribe":
                f.subscribed = False
        else:
            f.last_inbound, f.cs_since_inbound = now, 0
            f.chat.append({"from": "follower", "text": text if msg_type == "text" else f"[{msg_type}]", "at": now})
        xml = build_message(self.api.original_id, openid, text, msg_id or self.next_msg_id(), int(now),
                            msg_type=msg_type, event=event)
        return await self.post_xml(openid, xml)

    def sign(self, xml: str) -> tuple:
        ts, nonce = str(int(self.api.clock())), str(secrets.randbelow(10**9))
        params = {"signature": sha1_signature(self.token, ts, nonce), "timestamp": ts, "nonce": nonce,
                  "openid": ""}
        body = xml
        if self.mode != "plaintext":
            enc = self.crypto.encrypt(xml)
            params.update(encrypt_type="aes", msg_signature=sha1_signature(self.token, ts, nonce, enc))
            if self.mode == "safe":
                to_user = parse_xml(xml.encode())["ToUserName"]
                body = f"<xml><ToUserName><![CDATA[{to_user}]]></ToUserName><Encrypt><![CDATA[{enc}]]></Encrypt></xml>"
            else:
                body = xml[:-len("</xml>")] + f"<Encrypt><![CDATA[{enc}]]></Encrypt></xml>"
        return params, body

    async def post_xml(self, openid: str, xml: str) -> Delivery:
        started = time.monotonic()
        statuses: List[int] = []
        for attempt in range(1, self.attempts + 1):
            params, body = self.sign(xml)
            params["openid"] = openid
            try:
                r = await asyncio.wait_for(self.client.post(self.url, params=params, content=body.encode(),
                                                            headers={"Content-Type": "text/xml"}),
                                           timeout=self.timeout)
            except (asyncio.TimeoutError, httpx.TimeoutException):
                statuses.append(0)
                continue
            statuses.append(r.status_code)
            secs = round(time.monotonic() - started, 2)
            if r.status_code != 200:
                return Delivery(attempt, "error", statuses=statuses, seconds=secs, error=f"HTTP {r.status_code}")
            return self._check_reply(openid, r.text, params, attempt, statuses, secs)
        return Delivery(self.attempts, "timeout", statuses=statuses, seconds=round(time.monotonic() - started, 2))

    def _check_reply(self, openid: str, text: str, params: dict, attempt: int, statuses: list, secs: float) -> Delivery:
        if text.strip() in ("", "success"):
            return Delivery(attempt, "no_reply", statuses=statuses, seconds=secs)
        try:
            fields = parse_xml(text.encode())
            if params.get("encrypt_type") == "aes":
                enc = fields["Encrypt"]
                good = sha1_signature(self.token, fields["TimeStamp"], fields["Nonce"], enc)
                if good != fields.get("MsgSignature"):
                    raise ValueError("reply MsgSignature does not match")
                fields = parse_xml(self.crypto.decrypt(enc).encode())
            if fields.get("ToUserName") != openid or fields.get("FromUserName") != self.api.original_id:
                raise ValueError("reply is addressed wrongly")
            mtype = fields.get("MsgType", "")
            if mtype == "text":
                if not fields.get("Content"):
                    raise ValueError("empty Content")
                self.api.follower(openid).chat.append({"from": "account", "via": "passive", "text": fields["Content"],
                                                       "at": self.api.clock()})
                return Delivery(attempt, "passive", fields["Content"], mtype, statuses, secs)
            if mtype == "transfer_customer_service":
                return Delivery(attempt, "passive", "", mtype, statuses, secs)
            raise ValueError(f"unsupported reply MsgType {mtype!r}")
        except Exception as e:  # noqa: BLE001
            # WeChat shows the follower "该公众号暂时无法提供服务，请稍后再试" here.
            self.api.follower(openid).chat.append({"from": "wechat", "text": "该公众号暂时无法提供服务，请稍后再试",
                                                   "at": self.api.clock()})
            return Delivery(attempt, "error", statuses=statuses, seconds=secs, error=str(e))
