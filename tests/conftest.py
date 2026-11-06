import time
from dataclasses import dataclass

import httpx
import pytest

from wechat_support import config as config_mod
from wechat_support.ai import ScriptedProvider
from wechat_support.app import create_app
from wechat_support.crypto import random_encoding_aes_key
from wechat_support.fake_wechat import FakeWeChatAPI, FakeWeChatPush
from wechat_support.kb import KnowledgeBase
from wechat_support.service import SupportService
from wechat_support.store import Store
from wechat_support.wechat_api import Credentials, TokenManager, WeChatClient

APPID = "wxtest0123456789ab"
SECRET = "test-app-secret-not-real"
TOKEN = "callbacktoken42"


class Clock:
    """Wall clock that the tests can move forward (token expiry, the 48-hour window)."""

    def __init__(self):
        self.offset = 0.0

    def __call__(self):
        return time.time() + self.offset

    def advance(self, seconds):
        self.offset += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def cfg(tmp_path):
    s = config_mod.load(env={})
    s.database = str(tmp_path / "test.db")
    s.wechat.appid = APPID
    s.wechat.api_base = "http://wechat.fake"
    s.wechat.passive_budget_seconds = 0.4
    s.ai.provider = "scripted"
    return s


@pytest.fixture
def fake(clock):
    return FakeWeChatAPI(APPID, SECRET, clock=clock)


@dataclass
class Rig:
    svc: SupportService
    api: FakeWeChatAPI
    push: FakeWeChatPush
    provider: ScriptedProvider
    store: Store
    app: object
    http: httpx.AsyncClient


async def make_rig(cfg, fake, clock, provider=None, mode="plaintext", aes_key=None, timeout=1.0, store=None,
                   kb=None):
    provider = provider or ScriptedProvider()
    api_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake.app), base_url="http://wechat.fake")
    store = store or Store(cfg.database)
    tokens = TokenManager(store, Credentials(APPID, SECRET), api_http, cfg.wechat.api_base, clock=clock)
    client = WeChatClient(tokens, api_http, cfg.wechat.api_base, sleep=_nosleep)
    kb = kb or KnowledgeBase.load(cfg.knowledge_base)
    svc = SupportService(cfg, store, kb, provider, client, token=TOKEN, aes_key=aes_key, http=api_http, clock=clock)
    app = create_app(cfg, service=svc, sweep_every=3600)
    svc_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://svc")
    push = FakeWeChatPush(fake, svc_http, "http://svc/wechat", TOKEN, mode=mode, aes_key=aes_key, timeout=timeout)
    return Rig(svc, fake, push, provider, store, app, api_http), svc_http


async def _nosleep(_):
    return None


@pytest.fixture
async def rig(cfg, fake, clock):
    r, svc_http = await make_rig(cfg, fake, clock)
    async with r.app.router.lifespan_context(r.app):
        yield r
    await svc_http.aclose()
    await r.http.aclose()
    await drain(r.svc)


@pytest.fixture
def aes_key():
    return random_encoding_aes_key()


async def drain(svc):
    import asyncio

    pending = [t for t in svc.tasks.values() if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=5)


def events(svc, name):
    return [e for e in svc.events if e["event"] == name]
