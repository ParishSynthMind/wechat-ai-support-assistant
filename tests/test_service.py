"""The whole callback flow against the fake WeChat: passive replies, the 5-second rule,
retries, customer-service messages, handover, caps, encryption modes, several workers."""
import asyncio

import pytest

from tests.conftest import drain, events, make_rig
from wechat_support.ai import Answer, ScriptedProvider
from wechat_support.store import Store
from wechat_support.xmlmsg import build_message

pytestmark = pytest.mark.timeout(30)


def account_messages(api, openid):
    return [m for m in api.follower(openid).chat if m["from"] == "account"]


async def test_fast_answer_is_a_passive_reply(rig):
    d = await rig.push.say("oUser000000000000000000001", "How long does delivery to the UK take?")
    assert d.outcome == "passive" and d.attempts == 1
    assert "7 to 12 working days" in d.reply_text
    assert rig.store.get_message(events(rig.svc, "received")[0]["key"])["delivered_by"] == "passive"
    assert not [r for r in rig.api.requests if r["path"] == "/cgi-bin/message/custom/send"]


async def test_chinese_question_answered_from_chinese_sections(rig):
    d = await rig.push.say("oUser000000000000000000002", "退货政策是什么？")
    assert d.outcome == "passive"
    msg, sources, lang = rig.provider.calls[0]
    assert lang == "zh" and sources[0].startswith("zh/returns.md")
    assert "14天" in d.reply_text


async def test_slow_answer_goes_out_as_customer_service_message(rig):
    rig.provider.delay = 0.8          # longer than the 0.4 s passive budget
    oid = "oUser000000000000000000003"
    d = await rig.push.say(oid, "Do you offer gift wrapping?")
    assert d.outcome == "no_reply" and d.attempts == 1    # "success" within the timeout: no retry
    await drain(rig.svc)
    sent = account_messages(rig.api, oid)
    assert len(sent) == 1 and sent[0]["via"] == "customer_service" and "25 RMB" in sent[0]["text"]
    assert events(rig.svc, "went_async") and events(rig.svc, "cs_sent")


async def test_retry_after_timeout_collects_the_answer_passively(cfg, fake, clock):
    # WeChat gives up after 0.3 s here; we wait up to 0.5 s; the answer takes 0.45 s.
    cfg.wechat.passive_budget_seconds = 0.5
    rig, http = await make_rig(cfg, fake, clock, provider=ScriptedProvider(delay=0.45), timeout=0.3)
    async with rig.app.router.lifespan_context(rig.app):
        oid = "oUser000000000000000000004"
        d = await rig.push.say(oid, "How should I store green tea?")
        await drain(rig.svc)
    assert d.outcome == "passive" and d.attempts == 2 and d.statuses[0] == 0
    assert len(rig.provider.calls) == 1                         # one model call for three deliveries
    assert len(account_messages(fake, oid)) == 1                # one answer reached the follower
    assert events(rig.svc, "retry")
    await http.aclose()


async def test_every_attempt_times_out_answer_still_arrives_once(cfg, fake, clock):
    cfg.wechat.passive_budget_seconds = 2.0
    rig, http = await make_rig(cfg, fake, clock, provider=ScriptedProvider(delay=0.7), timeout=0.2)
    async with rig.app.router.lifespan_context(rig.app):
        oid = "oUser000000000000000000005"
        d = await rig.push.say(oid, "What payment methods do you accept?")
        assert d.outcome == "timeout" and d.attempts == 3
        await drain(rig.svc)
        await asyncio.sleep(0.05)
        assert account_messages(fake, oid) == []        # ready, but nobody collected it yet
        clock.advance(20)
        done = await rig.svc.sweep()                     # the background pass sends it
    assert done["rescued"] == 1
    sent = account_messages(fake, oid)
    assert len(sent) == 1 and sent[0]["via"] == "customer_service"
    await http.aclose()


async def test_duplicate_msgid_after_delivery_is_ignored(rig):
    oid = "oUser000000000000000000006"
    xml = build_message(rig.api.original_id, oid, "Is there caffeine in your tea?", "777000111")
    rig.api.follower(oid).last_inbound = rig.api.clock()
    first = await rig.push.post_xml(oid, xml)
    again = await rig.push.post_xml(oid, xml)
    assert first.outcome == "passive" and again.outcome == "passive"
    assert again.reply_text == first.reply_text
    assert len(rig.provider.calls) == 1


async def test_retry_of_async_message_answers_success(rig):
    rig.provider.delay = 0.8
    oid = "oUser000000000000000000007"
    xml = build_message(rig.api.original_id, oid, "Opening hours?", "888000111")
    rig.api.follower(oid).last_inbound = rig.api.clock()
    first = await rig.push.post_xml(oid, xml)
    again = await rig.push.post_xml(oid, xml)
    await drain(rig.svc)
    assert first.outcome == "no_reply" and again.outcome == "no_reply"
    assert len(account_messages(rig.api, oid)) == 1


async def test_subscribe_gets_bilingual_welcome_and_no_ai_call(rig):
    d = await rig.push.say("oUser000000000000000000008", "", msg_type="event", event="subscribe")
    assert d.outcome == "passive" and "感谢关注" in d.reply_text and "Thanks for following" in d.reply_text
    assert rig.provider.calls == []


async def test_unsubscribed_follower_cannot_be_messaged(rig):
    rig.provider.delay = 0.8
    oid = "oUser000000000000000000009"
    await rig.push.say(oid, "What is the refund timing?")
    await rig.push.say(oid, "", msg_type="event", event="unsubscribe")
    await drain(rig.svc)
    assert account_messages(rig.api, oid) == []
    assert events(rig.svc, "cs_not_sent")[0]["reason"] == "unsubscribed"


async def test_handover_keyword_english(rig):
    oid = "oUser000000000000000000010"
    d = await rig.push.say(oid, "Can I talk to a human please")
    assert d.outcome == "passive" and "colleague" in d.reply_text
    assert rig.store.user(oid)["handover"] == 1 and rig.provider.calls == []


async def test_handover_keyword_chinese_and_bot_stays_quiet(rig):
    oid = "oUser000000000000000000011"
    d = await rig.push.say(oid, "转人工")
    assert "同事" in d.reply_text
    d2 = await rig.push.say(oid, "我的订单还没到")
    assert d2.outcome == "no_reply" and rig.provider.calls == []
    rig.store.set_handover(oid, False)
    d3 = await rig.push.say(oid, "运费多少？")
    assert d3.outcome == "passive" and len(rig.provider.calls) == 1


async def test_keyword_inside_a_word_is_not_a_handover(rig):
    d = await rig.push.say("oUser000000000000000000012", "Are you an agency? What is your management team?")
    assert rig.store.user("oUser000000000000000000012")["handover"] == 0
    assert d.outcome == "passive"


async def test_transfer_customer_service_mode(cfg, fake, clock):
    cfg.handover.mode = "transfer_customer_service"
    rig, http = await make_rig(cfg, fake, clock)
    async with rig.app.router.lifespan_context(rig.app):
        d = await rig.push.say("oUser000000000000000000013", "人工客服")
        d2 = await rig.push.say("oUser000000000000000000013", "还在吗")
    assert d.reply_type == "transfer_customer_service" and d2.reply_type == "transfer_customer_service"
    await http.aclose()


async def test_model_says_needs_human(rig):
    rig.provider.replies = [Answer("needs_human", "A colleague will look at your order.", [], 100, 20)]
    oid = "oUser000000000000000000014"
    d = await rig.push.say(oid, "I was charged twice for order 5521")
    assert d.reply_text == "A colleague will look at your order."
    assert rig.store.user(oid)["handover"] == 1


async def test_answer_without_valid_citation_is_not_trusted(rig):
    rig.provider.replies = [Answer("answered", "We ship to Mars in 2 days.", [9], 100, 20)]
    await rig.push.say("oUser000000000000000000015", "Do you ship to Mars?")
    assert events(rig.svc, "answered")[0]["status"] == "not_in_knowledge_base"


async def test_ai_failure_sends_apology_and_hands_over(rig):
    rig.provider.replies = [RuntimeError("model down")]
    oid = "oUser000000000000000000016"
    d = await rig.push.say(oid, "How do I brew green tea?")
    assert d.outcome == "passive" and "something went wrong" in d.reply_text
    assert rig.store.user(oid)["handover"] == 1


async def test_per_user_daily_cap(cfg, fake, clock):
    cfg.ai.per_user_daily = 2
    rig, http = await make_rig(cfg, fake, clock)
    async with rig.app.router.lifespan_context(rig.app):
        oid = "oUser000000000000000000017"
        for q in ("Delivery costs?", "Refund timing?"):
            await rig.push.say(oid, q)
        d = await rig.push.say(oid, "Caffeine?")
    assert "can't answer more questions today" in d.reply_text and len(rig.provider.calls) == 2
    await http.aclose()


async def test_whole_account_daily_cap(cfg, fake, clock):
    cfg.ai.daily = 1
    rig, http = await make_rig(cfg, fake, clock)
    async with rig.app.router.lifespan_context(rig.app):
        await rig.push.say("oUser000000000000000000018", "Delivery costs?")
        d = await rig.push.say("oUser000000000000000000019", "退款多久到账？")
    assert "上限" in d.reply_text
    await http.aclose()


async def test_image_message_gets_text_only_notice(rig):
    d = await rig.push.say("oUser000000000000000000020", "", msg_type="image")
    assert d.outcome == "passive" and "text" in d.reply_text and rig.provider.calls == []


async def test_voice_message_uses_wechat_recognition(rig):
    d = await rig.push.say("oUser000000000000000000021", "绿茶怎么泡", msg_type="voice")
    assert d.outcome == "passive" and "80" in d.reply_text


async def test_customer_service_budget_of_five(rig):
    oid = "oUser000000000000000000022"
    await rig.push.say(oid, "hello")
    results = [await rig.svc.staff_reply(oid, f"update {i}") for i in range(6)]
    assert results[:5] == ["sent"] * 5 and results[5] == "budget_used"
    assert len([m for m in account_messages(rig.api, oid) if m["via"] == "customer_service"]) == 5
    await rig.push.say(oid, "thanks")          # a new message from them opens a new budget
    assert await rig.svc.staff_reply(oid, "you're welcome") == "sent"


async def test_48_hour_window(rig, clock):
    oid = "oUser000000000000000000023"
    await rig.push.say(oid, "hello")
    clock.advance(49 * 3600)
    assert await rig.svc.staff_reply(oid, "late answer") == "window_closed"
    assert not [r for r in rig.api.requests if r["path"] == "/cgi-bin/message/custom/send"]


async def test_wechat_45015_is_permanent_and_logged(rig):
    oid = "oUser000000000000000000024"
    await rig.push.say(oid, "hello")
    rig.api.inject["/cgi-bin/message/custom/send"] = [45015]
    assert await rig.svc.staff_reply(oid, "hi") == "failed 45015"
    assert any(r["status"] == "error 45015" for r in rig.store.history(oid))


async def test_system_busy_is_retried_by_the_client(rig):
    oid = "oUser000000000000000000025"
    await rig.push.say(oid, "hello")
    rig.api.inject["/cgi-bin/message/custom/send"] = [-1, -1]
    assert await rig.svc.staff_reply(oid, "hi") == "sent"


async def test_busy_beyond_retries_is_queued_and_sent_by_sweep(rig, clock):
    oid = "oUser000000000000000000026"
    await rig.push.say(oid, "hello")
    rig.api.inject["/cgi-bin/message/custom/send"] = [-1, -1, -1, -1]
    assert (await rig.svc.staff_reply(oid, "hi")).startswith("retry in")
    clock.advance(10)
    assert (await rig.svc.sweep())["sent"] == 1
    assert account_messages(rig.api, oid)[-1]["text"] == "hi"


async def test_expired_token_mid_conversation_is_refreshed(rig):
    rig.provider.delay = 0.8
    oid = "oUser000000000000000000027"
    await rig.push.say(oid, "Gift wrapping?")
    await drain(rig.svc)
    rig.api.expire_all_tokens()
    rig.provider.delay = 0.8
    await rig.push.say(oid, "Delivery costs?")
    await drain(rig.svc)
    assert len(account_messages(rig.api, oid)) == 2
    sends = [r for r in rig.api.requests if r["path"] == "/cgi-bin/message/custom/send"]
    assert [s["token_ok"] for s in sends] == [True, False, True]


async def test_bad_signature_is_refused(rig):
    r = await rig.push.client.post("/wechat", params={"signature": "0" * 40, "timestamp": "1", "nonce": "2"},
                                   content=b"<xml/>")
    assert r.status_code == 403


async def test_old_timestamp_is_refused(rig):
    """A correctly signed request from 20 minutes ago is a replay: refused."""
    from wechat_support.crypto import sha1_signature

    ts, nonce = str(int(rig.api.clock()) - 1200), "77"
    xml = build_message(rig.api.original_id, "oUser000000000000000000028", "hello", "4242")
    r = await rig.push.client.post("/wechat", params={"signature": sha1_signature("callbacktoken42", ts, nonce),
                                                      "timestamp": ts, "nonce": nonce}, content=xml.encode())
    assert r.status_code == 403 and "too old" in r.text


async def test_malformed_xml_is_400(rig):
    from wechat_support.crypto import sha1_signature

    ts, nonce = str(int(rig.api.clock())), "55"
    r = await rig.push.client.post("/wechat", params={"signature": sha1_signature("callbacktoken42", ts, nonce),
                                                      "timestamp": ts, "nonce": nonce}, content=b"<xml><a>")
    assert r.status_code == 400


@pytest.mark.parametrize("mode", ["safe", "compatible"])
async def test_encrypted_modes_end_to_end(cfg, fake, clock, aes_key, mode):
    rig, http = await make_rig(cfg, fake, clock, mode=mode, aes_key=aes_key)
    async with rig.app.router.lifespan_context(rig.app):
        d = await rig.push.say("oUser000000000000000000029", "How long does delivery to the UK take?")
    assert d.outcome == "passive" and "7 to 12" in d.reply_text   # the fake decrypted and checked the reply
    await http.aclose()


async def test_encrypted_with_wrong_key_is_refused(cfg, fake, clock, aes_key):
    from wechat_support.crypto import random_encoding_aes_key

    rig, http = await make_rig(cfg, fake, clock, mode="safe", aes_key=aes_key)
    rig.push.crypto.key = __import__("base64").b64decode(random_encoding_aes_key() + "=")
    async with rig.app.router.lifespan_context(rig.app):
        d = await rig.push.say("oUser000000000000000000030", "hello")
    assert d.outcome == "error" and d.error == "HTTP 400"
    await http.aclose()


async def test_require_encryption_refuses_plaintext(cfg, fake, clock, aes_key):
    cfg.wechat.require_encryption = True
    rig, http = await make_rig(cfg, fake, clock, mode="plaintext", aes_key=aes_key)
    async with rig.app.router.lifespan_context(rig.app):
        d = await rig.push.say("oUser000000000000000000031", "hello")
    assert d.error == "HTTP 403"
    await http.aclose()


async def test_two_workers_share_state(cfg, fake, clock):
    """A retry landing on a different worker process (same SQLite file) still gets the answer
    that the first worker is computing, and only one model call is made."""
    cfg.wechat.passive_budget_seconds = 0.6
    provider = ScriptedProvider(delay=0.5)
    store_a, store_b = Store(cfg.database), Store(cfg.database)
    a, http_a = await make_rig(cfg, fake, clock, provider=provider, store=store_a, timeout=0.25)
    b, http_b = await make_rig(cfg, fake, clock, provider=ScriptedProvider(), store=store_b, timeout=0.25)
    oid = "oUser000000000000000000032"
    xml = build_message(fake.original_id, oid, "Refund timing?", "999000222")
    fake.follower(oid).last_inbound = fake.clock()
    async with a.app.router.lifespan_context(a.app), b.app.router.lifespan_context(b.app):
        a.push.attempts = 1
        first = await a.push.post_xml(oid, xml)        # times out on worker A (WeChat gives up at 0.25 s)
        second = await b.push.post_xml(oid, xml)                              # retry lands on worker B
        await drain(a.svc)
    assert first.outcome == "timeout"
    assert second.outcome == "passive"            # worker B read the answer A wrote to the shared file
    assert len(provider.calls) == 1 and b.provider.calls == []
    assert len(account_messages(fake, oid)) == 1
    await http_a.aclose()
    await http_b.aclose()


async def test_conversation_log_records_both_directions(rig):
    oid = "oUser000000000000000000033"
    await rig.push.say(oid, "Caffeine in green tea?")
    rows = rig.store.history(oid)
    assert [r["direction"] for r in rows if r["direction"] in ("in", "out")] == ["in", "out"]
    assert rows[-1]["channel"] == "passive"
    counts = rig.store.counts()
    assert counts["delivered_passive"] == 1


async def test_handover_webhook_is_called(cfg, fake, clock):
    import httpx

    seen = []

    async def handler(request):
        seen.append(request)
        return httpx.Response(200)

    cfg.handover.webhook_url = "http://hook.test/notify"
    rig, http = await make_rig(cfg, fake, clock)
    rig.svc.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with rig.app.router.lifespan_context(rig.app):
        await rig.push.say("oUser000000000000000000034", "human")
    assert len(seen) == 1 and b'"wechat.handover"' in seen[0].content
    await http.aclose()


async def test_sweep_and_direct_send_never_double_send(rig, clock):
    """Found by the local run: the background sweep picked up a queued message while the
    request that queued it was still sending it, and the follower got it twice."""
    oid = "oUser000000000000000000035"
    await rig.push.say(oid, "hello")
    rig.api.send_delay = 0.3
    results = await asyncio.gather(rig.svc.staff_reply(oid, "only once"), _sweep_soon(rig.svc))
    assert results[0] == "sent"
    assert [m["text"] for m in account_messages(rig.api, oid)].count("only once") == 1


async def _sweep_soon(svc):
    await asyncio.sleep(0.05)
    return await svc.sweep()


async def test_send_left_by_a_dead_worker_is_picked_up_after_the_lease(rig, clock):
    oid = "oUser000000000000000000036"
    await rig.push.say(oid, "hello")
    rig.store.enqueue_cs("staff:dead", oid, "from a dead worker", clock())
    row = rig.store.cs_rows()[-1]
    assert rig.store.claim_cs(row["id"], clock()) is not None      # claimed, then the worker "dies"
    assert (await rig.svc.sweep())["sent"] == 0                     # lease still running
    clock.advance(61)
    assert (await rig.svc.sweep())["sent"] == 1


async def test_abandoned_attempt_cannot_swallow_the_answer(rig):
    """Over real HTTP an attempt WeChat has given up on keeps running on the server. If it takes
    the answer first, the retry WeChat is actually waiting on must still get the answer."""
    from wechat_support.xmlmsg import parse_message, parse_xml

    rig.provider.delay = 0.2
    oid = "oUser000000000000000000037"
    rig.store.touch_user(oid, rig.api.clock())
    msg = parse_message(parse_xml(build_message(rig.api.original_id, oid, "Refund timing?", "5550001").encode()))
    first = asyncio.create_task(rig.svc.process(msg))
    await asyncio.sleep(0.05)
    retry = asyncio.create_task(rig.svc.process(msg))      # WeChat's retry, while the first still waits
    a, b = await asyncio.gather(first, retry)
    assert "<Content>" in a and "<Content>" in b
    assert parse_xml(a.encode())["Content"] == parse_xml(b.encode())["Content"]
    assert len(rig.provider.calls) == 1
