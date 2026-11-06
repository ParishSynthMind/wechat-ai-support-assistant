"""Talking to api.weixin.qq.com: the access_token cache and the Customer Service message API.

access_token: valid 7200 s and rate-limited (2,000 fetches a day, 200 on a test account),
so it is fetched once and shared by every worker through the SQLite cache. A file lock
makes sure only one worker refreshes at a time; the others wait and read the new token.
It is refreshed 5 minutes before it expires, and at once if WeChat answers 40001, 40014
or 42001 (that request is then repeated once with the new token).

Two ways to get a token:
  stable  POST /cgi-bin/stable_token (default). The token stays the same until shortly
          before it expires, so two callers asking at once do not knock each other out.
          force_refresh (20 a day, 30 s apart) is only used after WeChat refuses a token.
  classic GET /cgi-bin/token. Every fetch makes a new token; the old one keeps working
          for a short overlap. Only for setups that already use it.
"""
import asyncio
import fcntl
import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

from .store import Store

log = logging.getLogger("wechat_support.api")

TOKEN_ERRORS = {40001, 40014, 42001}           # invalid credential, invalid token, expired token
RETRY_ERRORS = {-1, 45011}                      # system busy, per-minute quota
REFRESH_MARGIN = 300                            # seconds before expiry
ERROR_TEXT = {
    -1: "WeChat system busy, try again",
    40001: "access_token invalid, or AppSecret wrong",
    40003: "invalid openid (not a follower of this account)",
    40013: "invalid AppID",
    40014: "invalid access_token",
    40125: "invalid AppSecret",
    40164: "calling IP is not on the account's IP whitelist",
    42001: "access_token expired",
    43004: "the user does not follow the account",
    44002: "empty POST body",
    45002: "content too long",
    45009: "daily API quota used up",
    45011: "per-minute API quota used up",
    45015: "outside the 48-hour reply window, or the user unfollowed",
    45047: "too many customer-service messages since the user last wrote",
    47001: "JSON body not in the expected format",
    48001: "this API is not authorised for the account",
}


class WeChatError(Exception):
    def __init__(self, errcode: int, errmsg: str = ""):
        self.errcode, self.errmsg = int(errcode), errmsg
        super().__init__(f"WeChat {errcode}: {errmsg or ERROR_TEXT.get(int(errcode), 'error')}")

    @property
    def explain(self) -> str:
        return ERROR_TEXT.get(self.errcode, self.errmsg or "unknown error")


@dataclass
class Credentials:
    appid: str
    secret: str


class TokenManager:
    def __init__(self, store: Store, creds: Credentials, http: httpx.AsyncClient, base_url: str,
                 mode: str = "stable", lock_path: Optional[str] = None, clock=time.time):
        if mode not in ("stable", "classic"):
            raise ValueError("token mode must be 'stable' or 'classic'")
        self.store, self.creds, self.http, self.base = store, creds, http, base_url.rstrip("/")
        self.mode, self.clock = mode, clock
        self.lock_path = lock_path or (store.path + ".token.lock" if store.path != ":memory:" else "/tmp/wechat-token.lock")
        self.fetches = 0           # how many times this process asked WeChat for a token
        self._last_force = 0.0

    def _fresh(self, row) -> bool:
        return bool(row) and row["expires_at"] - REFRESH_MARGIN > self.clock()

    async def get(self, refused: Optional[str] = None) -> str:
        """A usable token. `refused` is a token WeChat just rejected: never hand that one back."""
        row = self.store.get_token(self.creds.appid)
        if self._fresh(row) and row["access_token"] != refused:
            return row["access_token"]
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise TimeoutError("waited 20 s for another worker to refresh the access_token")
                    await asyncio.sleep(0.05)
            try:
                row = self.store.get_token(self.creds.appid)   # someone else may have just refreshed it
                if self._fresh(row) and row["access_token"] != refused:
                    return row["access_token"]
                token, expires_in = await self._fetch(force=refused is not None)
                self.store.put_token(self.creds.appid, token, self.clock() + expires_in, self.mode)
                return token
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    async def _fetch(self, force: bool):
        self.fetches += 1
        if self.mode == "classic":
            r = await self.http.get(f"{self.base}/cgi-bin/token", params={
                "grant_type": "client_credential", "appid": self.creds.appid, "secret": self.creds.secret})
        else:
            # force_refresh is rationed by WeChat (20 a day, 30 s apart), so never twice in 30 s.
            force = force and self.clock() - self._last_force >= 30
            if force:
                self._last_force = self.clock()
            r = await self.http.post(f"{self.base}/cgi-bin/stable_token", json={
                "grant_type": "client_credential", "appid": self.creds.appid, "secret": self.creds.secret,
                "force_refresh": force})
        data = _json(r)
        if data.get("errcode"):
            raise WeChatError(data["errcode"], data.get("errmsg", ""))
        if not data.get("access_token"):
            raise WeChatError(-1, "no access_token in the reply")
        log.info("access_token fetched (%s%s), valid %ss", self.mode, ", forced" if force else "", data.get("expires_in"))
        return data["access_token"], int(data.get("expires_in", 7200))


def _json(r: httpx.Response) -> Dict[str, Any]:
    if r.status_code >= 500:
        raise WeChatError(-1, f"HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError:
        raise WeChatError(-1, f"HTTP {r.status_code}, not JSON")


class WeChatClient:
    def __init__(self, tokens: TokenManager, http: httpx.AsyncClient, base_url: str,
                 retries: int = 3, backoff: float = 0.5, sleep=asyncio.sleep):
        self.tokens, self.http, self.base = tokens, http, base_url.rstrip("/")
        self.retries, self.backoff, self.sleep = retries, backoff, sleep

    async def call(self, method: str, path: str, body: Optional[Dict] = None) -> Dict[str, Any]:
        token = await self.tokens.get()
        refreshed = False
        attempt = 0
        while True:
            try:
                r = await self.http.request(method, f"{self.base}{path}", params={"access_token": token},
                                            json=body if body is not None else None)
                data = _json(r)
            except (httpx.TransportError, WeChatError) as e:
                if isinstance(e, WeChatError) and e.errcode != -1:
                    raise
                data = {"errcode": -1, "errmsg": str(e)}
            code = int(data.get("errcode", 0) or 0)
            if code == 0:
                return data
            if code in TOKEN_ERRORS and not refreshed:
                log.warning("WeChat refused the access_token (%s), refreshing once", code)
                self.tokens.store.drop_token(self.tokens.creds.appid, token)
                token = await self.tokens.get(refused=token)
                refreshed = True
                continue
            if code in RETRY_ERRORS and attempt < self.retries:
                attempt += 1
                await self.sleep(self.backoff * (2 ** (attempt - 1)) * (1 + random.random() * 0.25))
                continue
            raise WeChatError(code, data.get("errmsg", ""))

    async def send_text(self, openid: str, content: str) -> Dict[str, Any]:
        return await self.call("POST", "/cgi-bin/message/custom/send",
                               {"touser": openid, "msgtype": "text", "text": {"content": content}})

    async def typing(self, openid: str, on: bool = True) -> None:
        try:
            await self.call("POST", "/cgi-bin/message/custom/typing",
                            {"touser": openid, "command": "Typing" if on else "CancelTyping"})
        except WeChatError as e:   # a typing indicator is a nicety, never a failure
            log.info("typing indicator not sent: %s", e)

    async def api_domain_ip(self) -> Dict[str, Any]:
        """A harmless call that proves the token works (used by `check`)."""
        return await self.call("GET", "/cgi-bin/get_api_domain_ip")
