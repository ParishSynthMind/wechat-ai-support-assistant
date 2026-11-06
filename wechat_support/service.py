"""What happens to one callback, from the signature check to the answer reaching the follower.

WeChat waits 5 seconds for the HTTP reply. If it gets nothing it drops the connection and
tries again, three times in all. So:

  * Every message is claimed once by its dedupe key (MsgId, or FromUserName + CreateTime for
    events). A retry never starts a second answer and never sends a second reply.
  * The answer runs as its own task. The request waits up to `passive_budget_seconds` (4 s by
    default). If the answer is ready, it goes back in the HTTP reply (a "passive" reply, free
    and instant).
  * If not, the request answers "success" (WeChat's "no reply" signal, so it stops retrying)
    and the answer is sent through the Customer Service message API as soon as it is ready.
    That API only works within 48 hours of the follower's last message and only a few times
    (5) per message, so the budget is counted before each send.
  * A retry that arrives while the answer is still being worked on waits for it again: if it
    is ready inside this new 5-second window it becomes the passive reply instead.

The state of each message lives in SQLite with compare-and-set updates, so this holds across
several worker processes: exactly one of "passive reply" or "customer-service message"
happens, never both and never neither (a sweep picks up anything left in between)."""
import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

import httpx

from . import lang as langmod
from .ai import Answer
from .config import Settings
from .crypto import CryptoError, MessageCrypto, SignatureError, check_signature, check_timestamp
from .kb import KnowledgeBase
from .store import Store
from .wechat_api import WeChatClient, WeChatError
from .xmlmsg import BadMessage, Incoming, parse_message, parse_xml, text_reply, transfer_reply

log = logging.getLogger("wechat_support.service")

SUCCESS = "success"
PERMANENT_CS_ERRORS = {40003, 43004, 45015, 45047, 48001, 45002}
MAX_CS_ATTEMPTS = 6


def mask(openid: str) -> str:
    return (openid[:4] + "..." + openid[-3:]) if len(openid) > 10 else "***"


@dataclass
class Reply:
    status: int
    body: str
    media_type: str = "text/plain"


def _clip(text: str, limit: int = 600) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


class SupportService:
    def __init__(self, cfg: Settings, store: Store, kb: KnowledgeBase, provider, wechat: Optional[WeChatClient],
                 token: str, aes_key: Optional[str] = None, http: Optional[httpx.AsyncClient] = None,
                 clock=time.time):
        self.cfg, self.store, self.kb, self.provider, self.wechat = cfg, store, kb, provider, wechat
        self.token, self.clock, self.http = token, clock, http
        self.crypto = MessageCrypto(token, aes_key, cfg.wechat.appid) if aes_key else None
        self.tasks: Dict[str, asyncio.Task] = {}
        self.events = []   # what happened, for the local run and the tests

    # ------------------------------------------------------------------ HTTP layer
    def verify_url(self, q: Dict[str, str]) -> Reply:
        """GET from WeChat when the URL is saved in the admin page: echo echostr if signed."""
        try:
            check_signature(self.token, q.get("signature", ""), q.get("timestamp", ""), q.get("nonce", ""))
        except SignatureError:
            return Reply(403, "bad signature")
        return Reply(200, q.get("echostr", ""))

    async def handle_post(self, q: Dict[str, str], body: bytes) -> Reply:
        try:
            check_signature(self.token, q.get("signature", ""), q.get("timestamp", ""), q.get("nonce", ""))
            check_timestamp(q.get("timestamp", ""), self.cfg.wechat.timestamp_max_age, self.clock())
        except SignatureError as e:
            log.warning("callback refused: %s", e)
            return Reply(403, str(e))
        encrypted = q.get("encrypt_type", "raw").lower() == "aes"
        try:
            fields = parse_xml(body)
            if encrypted:
                if not self.crypto:
                    return Reply(400, "encrypted callback but no EncodingAESKey configured")
                enc = fields.get("Encrypt", "")
                self.crypto.check_msg_signature(q.get("msg_signature", ""), q.get("timestamp", ""),
                                                q.get("nonce", ""), enc)
                fields = parse_xml(self.crypto.decrypt(enc).encode())
            elif self.cfg.wechat.require_encryption:
                return Reply(403, "plaintext callbacks are switched off")
            msg = parse_message(fields)
        except SignatureError as e:
            return Reply(403, str(e))
        except (BadMessage, CryptoError) as e:
            log.warning("callback not understood: %s", e)
            return Reply(400, str(e))
        xml = await self.process(msg)
        if xml == SUCCESS:
            return Reply(200, SUCCESS)
        if encrypted:
            envelope, *_ = self.crypto.encrypt_reply(xml, nonce=q.get("nonce") or None)
            return Reply(200, envelope, "application/xml")
        return Reply(200, xml, "application/xml")

    # ------------------------------------------------------------------ one message
    async def process(self, msg: Incoming) -> str:
        key = msg.dedupe_key
        earlier = self.store.claim(key, msg.from_user, self.clock())
        if earlier is not None:
            return await self._retry(msg, earlier)
        self.store.touch_user(msg.from_user, self.clock())
        self.store.log(msg.from_user, "in" if msg.msg_type != "event" else "event",
                       msg.text or msg.event or f"[{msg.msg_type}]",
                       detail={"msg_type": msg.msg_type, "key": key})
        self._event("received", msg, kind=msg.msg_type, text=msg.text or msg.event)

        if msg.msg_type == "event":
            return self._handle_event(msg)

        lang = langmod.detect(msg.text or msg.content, default=self.cfg.default_lang)
        user = self.store.user(msg.from_user)
        if user and user["handover"]:
            self.store.set_message(key, "ignored", delivered_by="none")
            self.store.log(msg.from_user, "note", "in handover: left for the team", status="handover")
            self._event("handover_quiet", msg)
            if self.cfg.handover.mode == "transfer_customer_service":
                return transfer_reply(msg, int(self.clock()))
            return SUCCESS

        if msg.text and self._wants_human(msg.text):
            await self._start_handover(msg, "keyword", lang)
            if self.cfg.handover.mode == "transfer_customer_service":
                self.store.set_message(key, "passive", delivered_by="transfer")
                return transfer_reply(msg, int(self.clock()))
            text = self.cfg.holding[lang]
            return self._passive_now(msg, key, text, lang, "handover")

        if not msg.text:
            return self._passive_now(msg, key, self.cfg.unsupported[lang], lang, "unsupported")

        self.tasks[key] = asyncio.create_task(self._answer(msg, key, lang))
        return await self._wait_for_answer(msg, key, self.cfg.wechat.passive_budget_seconds)

    def _handle_event(self, msg: Incoming) -> str:
        key = msg.dedupe_key
        ev = msg.event.lower()
        if ev == "subscribe":
            # A follow event says nothing about the follower's language, so the welcome has both.
            self.store.set_subscribed(msg.from_user, True)
            return self._passive_now(msg, key, self.cfg.welcome["zh"] + "\n\n" + self.cfg.welcome["en"], "zh+en",
                                     "welcome")
        if ev == "unsubscribe":
            self.store.set_subscribed(msg.from_user, False)
            self.store.set_message(key, "ignored", delivered_by="none")
            return SUCCESS
        self.store.set_message(key, "ignored", delivered_by="none")
        return SUCCESS

    def _passive_now(self, msg: Incoming, key: str, text: str, lang: str, why: str) -> str:
        self.store.set_message(key, "passive", answer=text, delivered_by="passive")
        self.store.log(msg.from_user, "out", text, channel="passive", lang=lang, status=why)
        self._event("passive_reply", msg, text=text, why=why, lang=lang)
        return text_reply(msg, text, int(self.clock()))

    async def _retry(self, msg: Incoming, row) -> str:
        """WeChat sent this message again because an earlier reply did not reach it in 5 s."""
        state = row["state"]
        self._event("retry", msg, state=state)
        if state == "passive" and row["answer"]:
            # Our passive reply was lost or late: send the same one again (still one answer).
            if row["delivered_by"] == "transfer":
                return transfer_reply(msg, int(self.clock()))
            return text_reply(msg, row["answer"], int(self.clock()))
        if state in ("working", "ready"):
            return await self._wait_for_answer(msg, msg.dedupe_key, self.cfg.wechat.passive_budget_seconds)
        return SUCCESS

    async def _wait_for_answer(self, msg: Incoming, key: str, budget: float) -> str:
        deadline = time.monotonic() + budget
        task = self.tasks.get(key)
        while True:
            row = self.store.get_message(key)
            if row["state"] == "ready":
                if self.store.set_message(key, "passive", delivered_by="passive", only_if="ready"):
                    lang = langmod.detect(row["answer"] or "")
                    self.store.log(msg.from_user, "out", row["answer"], channel="passive", lang=lang, status="answer")
                    self._event("passive_reply", msg, text=row["answer"], why="answer", lang=lang)
                    return text_reply(msg, row["answer"], int(self.clock()))
                continue  # another attempt took it; read the state again
            if row["state"] == "passive" and row["delivered_by"] == "passive" and row["answer"]:
                # An earlier attempt (one WeChat has probably already given up on) took the
                # answer. WeChat only listens to the newest attempt, so give the same answer here.
                return text_reply(msg, row["answer"], int(self.clock()))
            if row["state"] != "working":
                return SUCCESS
            left = deadline - time.monotonic()
            if left <= 0:
                break
            if task is not None and not task.done():
                await asyncio.wait({task}, timeout=min(left, 0.25))
            else:
                await asyncio.sleep(min(left, 0.1))   # the answer runs in another worker process
        # Out of time: tell WeChat "success" and send the answer later as a customer-service message.
        if self.store.set_message(key, "async", only_if="working"):
            self._event("went_async", msg, waited=budget)
            if self.wechat and self.cfg.wechat.typing_indicator:
                asyncio.create_task(self.wechat.typing(msg.from_user, True))
            return SUCCESS
        return await self._wait_for_answer(msg, key, 0.0)   # it became ready at the last moment

    # ------------------------------------------------------------------ the answer
    def today(self) -> str:
        return datetime.fromtimestamp(self.clock(), tz=timezone.utc).strftime("%Y-%m-%d")

    def _wants_human(self, text: str) -> bool:
        low = text.lower()
        for kw in self.cfg.handover.keywords:
            k = kw.lower()
            if re.search(r"[a-z]", k):
                if re.search(rf"(?<![a-z]){re.escape(k)}(?![a-z])", low):
                    return True
            elif k in low:
                return True
        return False

    async def _answer(self, msg: Incoming, key: str, lang: str) -> None:
        started = time.monotonic()
        try:
            text, status, detail = await self._compose(msg, lang)
        except Exception as e:  # noqa: BLE001 (any failure still gets the follower a message)
            log.exception("answer failed for %s", mask(msg.from_user))
            text, status, detail = self.cfg.error[lang], "error", {"error": f"{type(e).__name__}: {e}"[:300]}
            await self._start_handover(msg, "error", lang, announce=False)
        detail["seconds"] = round(time.monotonic() - started, 2)
        self._event("answered", msg, status=status, text=text, lang=lang, **detail)
        self.store.log(msg.from_user, "note", status, lang=lang, status=status, detail=detail)
        if self.store.set_message(key, "ready", answer=text, only_if="working"):
            return   # a waiting request (this one or a retry) will reply passively
        row = self.store.get_message(key)
        if row["state"] == "async":
            self.store.set_message(key, "async", answer=text)
            await self.deliver_cs(msg.from_user, key, text, lang)

    async def _compose(self, msg: Incoming, lang: str) -> Tuple[str, str, dict]:
        cap = self.store.take_ai_call(msg.from_user, self.today(), self.cfg.ai.per_user_daily, self.cfg.ai.daily)
        if cap:
            await self._start_handover(msg, f"cap:{cap}", lang, announce=False)
            return self.cfg.busy[lang], "capped", {"cap": cap}
        question = msg.text.strip()[:1000]
        hits = self.kb.search(question, lang=lang, limit=self.cfg.ai.max_sections)
        ans: Answer = await asyncio.wait_for(self.provider.answer(question, hits, lang), timeout=45)
        valid = [c for c in dict.fromkeys(ans.citations) if 1 <= c <= len(hits)]
        status = ans.status
        if status == "answered" and not valid:
            status = "not_in_knowledge_base"   # an answer that cannot point at a section is not trusted
        text = _clip(ans.text) or self.cfg.error[lang]
        detail = {"sources": [hits[c - 1].section.id for c in valid], "tokens_in": ans.tokens_in,
                  "tokens_out": ans.tokens_out, "model": getattr(ans, "model", ""), "hits": len(hits)}
        if status == "needs_human" or (status == "not_in_knowledge_base" and self.cfg.handover.on_not_found):
            await self._start_handover(msg, status, lang, announce=False)
        return text, status, detail

    async def _start_handover(self, msg: Incoming, reason: str, lang: str, announce: bool = True) -> None:
        self.store.set_handover(msg.from_user, True)
        self.store.log(msg.from_user, "note", f"handover: {reason}", status="handover")
        self._event("handover", msg, reason=reason)
        url = self.cfg.handover.webhook_url
        if url and self.http is not None:
            try:
                await self.http.post(url, json={"event": "wechat.handover", "openid": msg.from_user,
                                                "reason": reason, "message": msg.text, "lang": lang,
                                                "at": int(self.clock())}, timeout=5)
            except httpx.HTTPError as e:
                log.warning("handover webhook failed: %s", e)

    # ------------------------------------------------------------------ customer-service messages
    async def deliver_cs(self, openid: str, key: str, text: str, lang: str = "") -> str:
        """Queue one customer-service message for this dedupe key (at most once) and try it now."""
        self.store.enqueue_cs(key, openid, text, self.clock())
        rows = [r for r in self.store.cs_rows() if r["dedupe_key"] == key]
        return await self._send_row(rows[0]) if rows else "already"

    async def _send_row(self, row) -> str:
        row = self.store.claim_cs(row["id"], self.clock())
        if row is None:
            return "already"     # another worker or the sweep is sending it
        openid, key, text = row["openid"], row["dedupe_key"], row["text"]
        why = self.store.take_cs_slot(openid, self.clock(), self.cfg.wechat.cs_window_hours * 3600,
                                      self.cfg.wechat.cs_per_window)
        if why:
            self.store.cs_result(row["id"], "dead", error=why)
            self.store.set_message(key, "failed", delivered_by="none")
            self.store.log(openid, "note", f"not sent: {why}", channel="customer_service", status="undeliverable")
            self._event("cs_not_sent", None, openid=openid, reason=why)
            return why
        try:
            await self.wechat.send_text(openid, text)
        except WeChatError as e:
            if e.errcode != 45047:   # WeChat itself says the budget is used up: keep it counted
                self.store.give_back_cs_slot(openid)
            if e.errcode in PERMANENT_CS_ERRORS or row["attempts"] + 1 >= MAX_CS_ATTEMPTS:
                self.store.cs_result(row["id"], "dead", error=str(e))
                self.store.set_message(key, "failed", delivered_by="none")
                self.store.log(openid, "note", f"not sent: {e.explain}", channel="customer_service",
                               status=f"error {e.errcode}")
                self._event("cs_failed", None, openid=openid, errcode=e.errcode, reason=e.explain)
                return f"failed {e.errcode}"
            delay = min(300, 5 * 2 ** row["attempts"])
            self.store.cs_result(row["id"], "pending", error=str(e), next_at=self.clock() + delay)
            self._event("cs_retry_later", None, openid=openid, errcode=e.errcode, delay=delay)
            return f"retry in {delay}s"
        except (httpx.HTTPError, TimeoutError) as e:
            self.store.give_back_cs_slot(openid)
            delay = min(300, 5 * 2 ** row["attempts"])
            self.store.cs_result(row["id"], "pending", error=f"{type(e).__name__}", next_at=self.clock() + delay)
            self._event("cs_retry_later", None, openid=openid, errcode=None, delay=delay)
            return f"retry in {delay}s"
        self.store.cs_result(row["id"], "sent")
        self.store.set_message(key, "done", delivered_by="customer_service")
        self.store.log(openid, "out", text, channel="customer_service", lang=langmod.detect(text), status="sent")
        self._event("cs_sent", None, openid=openid, text=text)
        return "sent"

    async def staff_reply(self, openid: str, text: str) -> str:
        """A person on the team answers a follower (from the CLI). Same window and budget rules."""
        key = f"staff:{openid}:{time.time_ns()}"
        return await self.deliver_cs(openid, key, text)

    async def sweep(self) -> Dict[str, int]:
        """Background pass: send queued customer-service messages that are due, and pick up any
        answer that was ready but never collected by a request (for example a worker restart)."""
        done = {"sent": 0, "rescued": 0}
        for row in self.store.due_cs(self.clock()):
            if await self._send_row(row) == "sent":
                done["sent"] += 1
        with self.store._conn() as c:
            stale = c.execute("SELECT * FROM messages WHERE state='ready' AND updated_at < ?",
                              (self.clock() - 15,)).fetchall()
        for row in stale:
            if self.store.set_message(row["dedupe_key"], "async", only_if="ready"):
                await self.deliver_cs(row["openid"], row["dedupe_key"], row["answer"])
                done["rescued"] += 1
        self.store.prune_messages(self.clock() - 3 * 86400)
        return done

    def _event(self, name: str, msg: Optional[Incoming], **data) -> None:
        ev = {"t": round(self.clock(), 3), "event": name}
        if msg is not None:
            ev["openid"] = msg.from_user
            ev["key"] = msg.dedupe_key
        ev.update(data)
        self.events.append(ev)
        log.info("%s %s", name, {k: (mask(v) if k == "openid" else v) for k, v in ev.items() if k != "event"})
