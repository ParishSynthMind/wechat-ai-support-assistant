"""The access_token cache shared by workers, and the API client's error handling."""
import asyncio

import httpx
import pytest

from tests.conftest import APPID, SECRET
from wechat_support.fake_wechat import FakeWeChatAPI
from wechat_support.store import Store
from wechat_support.wechat_api import Credentials, TokenManager, WeChatClient, WeChatError

pytestmark = pytest.mark.timeout(30)


async def _nosleep(_):
    return None


def setup(tmp_path, clock, mode="stable", secret=SECRET, fake=None):
    fake = fake or FakeWeChatAPI(APPID, SECRET, clock=clock)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake.app), base_url="http://wechat.fake")
    store = Store(str(tmp_path / "t.db"))
    tm = TokenManager(store, Credentials(APPID, secret), http, "http://wechat.fake", mode=mode, clock=clock)
    return fake, http, store, tm, WeChatClient(tm, http, "http://wechat.fake", sleep=_nosleep)


async def test_token_is_cached(tmp_path, clock):
    fake, http, store, tm, client = setup(tmp_path, clock)
    for _ in range(5):
        await client.api_domain_ip()
    assert fake.token_calls == 1 and tm.fetches == 1
    await http.aclose()


async def test_token_shared_between_workers(tmp_path, clock):
    fake, http, store, tm, _ = setup(tmp_path, clock)
    other = TokenManager(Store(store.path), Credentials(APPID, SECRET), http, "http://wechat.fake", clock=clock)
    assert await tm.get() == await other.get()
    assert fake.token_calls == 1
    await http.aclose()


async def test_concurrent_refresh_fetches_once(tmp_path, clock):
    fake, http, store, tm, _ = setup(tmp_path, clock)
    managers = [TokenManager(Store(store.path), Credentials(APPID, SECRET), http, "http://wechat.fake", clock=clock)
                for _ in range(8)]
    tokens = await asyncio.gather(*(m.get() for m in managers))
    assert len(set(tokens)) == 1 and fake.token_calls == 1
    await http.aclose()


async def test_refreshed_five_minutes_before_expiry(tmp_path, clock):
    fake, http, store, tm, _ = setup(tmp_path, clock, mode="classic")
    first = await tm.get()
    clock.advance(7200 - 400)
    assert await tm.get() == first
    clock.advance(200)                      # now inside the last 5 minutes
    assert await tm.get() != first and fake.token_calls == 2
    await http.aclose()


async def test_classic_old_token_still_works_during_overlap(tmp_path, clock):
    fake, http, store, tm, _ = setup(tmp_path, clock, mode="classic")
    first = await tm.get()
    store.drop_token(APPID, first)
    await tm.get()
    assert fake.token_status(first) == 0
    clock.advance(301)
    assert fake.token_status(first) == 42001
    await http.aclose()


async def test_expired_token_refreshed_and_call_repeated(tmp_path, clock):
    fake, http, store, tm, client = setup(tmp_path, clock)
    await client.api_domain_ip()
    fake.expire_all_tokens()
    data = await client.api_domain_ip()
    assert data["ip_list"]
    calls = [r for r in fake.requests if r["path"] == "/cgi-bin/get_api_domain_ip"]
    assert [c["token_ok"] for c in calls] == [True, False, True]
    assert [r["force"] for r in fake.requests if r["path"] == "/cgi-bin/stable_token"] == [False, True]
    await http.aclose()


async def test_token_refused_twice_gives_up(tmp_path, clock):
    fake, http, store, tm, client = setup(tmp_path, clock)
    await client.api_domain_ip()
    fake.inject["/cgi-bin/message/custom/send"] = []
    fake.follower("oX").last_inbound = clock()
    # Make WeChat refuse every token from now on.
    fake.token_status = lambda token: 40001
    with pytest.raises(WeChatError) as e:
        await client.send_text("oX", "hi")
    assert e.value.errcode == 40001
    await http.aclose()


async def test_force_refresh_never_twice_in_30_seconds(tmp_path, clock):
    fake, http, store, tm, client = setup(tmp_path, clock)
    t1 = await tm.get()
    t2 = await tm.get(refused=t1)
    t3 = await tm.get(refused=t2)       # within 30 s: asks normally, not forced
    forces = [r["force"] for r in fake.requests if r["path"] == "/cgi-bin/stable_token"]
    assert forces == [False, True, False] and t3 == t2
    await http.aclose()


async def test_wrong_secret_is_40125(tmp_path, clock):
    fake, http, store, tm, client = setup(tmp_path, clock, secret="wrong")
    with pytest.raises(WeChatError) as e:
        await tm.get()
    assert e.value.errcode == 40125 and "AppSecret" in e.value.explain
    await http.aclose()


async def test_wrong_appid_is_40013(tmp_path, clock):
    fake = FakeWeChatAPI("wxsomeoneelse000000", SECRET, clock=clock)
    _, http, store, tm, client = setup(tmp_path, clock, fake=fake)
    with pytest.raises(WeChatError) as e:
        await tm.get()
    assert e.value.errcode == 40013
    await http.aclose()


async def test_test_account_daily_token_limit(tmp_path, clock):
    fake, http, store, tm, client = setup(tmp_path, clock, mode="classic")
    fake.token_calls = 200
    with pytest.raises(WeChatError) as e:
        await tm.get()
    assert e.value.errcode == 45009
    await http.aclose()


async def test_system_busy_and_minute_quota_are_retried(tmp_path, clock):
    fake, http, store, tm, client = setup(tmp_path, clock)
    fake.follower("oX").last_inbound = clock()
    fake.inject["/cgi-bin/message/custom/send"] = [-1, 45011]
    assert (await client.send_text("oX", "hi"))["errcode"] == 0
    await http.aclose()


async def test_retries_are_limited(tmp_path, clock):
    fake, http, store, tm, client = setup(tmp_path, clock)
    fake.follower("oX").last_inbound = clock()
    fake.inject["/cgi-bin/message/custom/send"] = [-1, -1, -1, -1, -1]
    with pytest.raises(WeChatError) as e:
        await client.send_text("oX", "hi")
    assert e.value.errcode == -1
    await http.aclose()


@pytest.mark.parametrize("setup_follower,code", [
    (lambda f, now: None, 40003),                                        # never wrote to us
    (lambda f, now: setattr(f.follower("oX"), "last_inbound", now - 49 * 3600), 45015),
    (lambda f, now: (setattr(f.follower("oX"), "last_inbound", now), setattr(f.follower("oX"), "subscribed", False)),
     45015),
    (lambda f, now: (setattr(f.follower("oX"), "last_inbound", now), setattr(f.follower("oX"), "cs_since_inbound", 5)),
     45047),
])
async def test_permanent_errors_are_not_retried(tmp_path, clock, setup_follower, code):
    fake, http, store, tm, client = setup(tmp_path, clock)
    setup_follower(fake, clock())
    with pytest.raises(WeChatError) as e:
        await client.send_text("oX", "hi")
    assert e.value.errcode == code
    assert len([r for r in fake.requests if r["path"] == "/cgi-bin/message/custom/send"]) == 1
    await http.aclose()


async def test_too_long_and_malformed_bodies(tmp_path, clock):
    fake, http, store, tm, client = setup(tmp_path, clock)
    fake.follower("oX").last_inbound = clock()
    with pytest.raises(WeChatError) as e:
        await client.send_text("oX", "长" * 1000)
    assert e.value.errcode == 45002
    token = await tm.get()
    r = await http.post("/cgi-bin/message/custom/send", params={"access_token": token}, content=b"{nope")
    assert r.json()["errcode"] == 47001
    r = await http.post("/cgi-bin/message/custom/send", params={"access_token": token}, content=b"")
    assert r.json()["errcode"] == 44002
    await http.aclose()


async def test_http_5xx_counts_as_busy(tmp_path, clock):
    calls = {"n": 0}

    async def handler(request):
        if request.url.path == "/cgi-bin/stable_token":
            return httpx.Response(200, json={"access_token": "T", "expires_in": 7200})
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://w")
    tm = TokenManager(Store(str(tmp_path / "x.db")), Credentials(APPID, SECRET), http, "http://w", clock=clock)
    client = WeChatClient(tm, http, "http://w", sleep=_nosleep)
    assert (await client.send_text("oX", "hi"))["errcode"] == 0 and calls["n"] == 3
    await http.aclose()


async def test_typing_failure_is_swallowed(tmp_path, clock):
    fake, http, store, tm, client = setup(tmp_path, clock)
    await client.typing("oNobody")          # 40003, logged, no exception
    await http.aclose()
