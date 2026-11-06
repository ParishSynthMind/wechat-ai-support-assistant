"""The XML WeChat posts to the callback, and the XML we answer with.

Parsing goes through defusedxml, so an entity-expansion or external-entity body is refused
instead of being expanded. Only the fields this service uses are pulled out; the rest of
the message is kept in `raw` for the log."""
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from defusedxml import ElementTree as SafeET

MAX_BODY = 64 * 1024  # real callbacks are a few hundred bytes


class BadMessage(Exception):
    pass


@dataclass
class Incoming:
    to_user: str          # the Official Account's original id (gh_...)
    from_user: str        # the follower's openid
    create_time: int
    msg_type: str         # text, image, voice, video, shortvideo, location, link, event
    content: str = ""
    msg_id: str = ""
    event: str = ""       # subscribe, unsubscribe, CLICK, ...
    event_key: str = ""
    recognition: str = ""  # voice messages: WeChat's speech-to-text, when switched on
    raw: Dict[str, str] = field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        """WeChat's own advice: MsgId for messages, FromUserName + CreateTime for events."""
        if self.msg_id:
            return f"msg:{self.msg_id}"
        return f"evt:{self.from_user}:{self.create_time}:{self.event}"

    @property
    def text(self) -> str:
        """What the follower said, if it can be answered as text."""
        if self.msg_type == "text":
            return self.content
        if self.msg_type == "voice" and self.recognition:
            return self.recognition
        return ""


def parse_xml(body: bytes) -> Dict[str, str]:
    if not body or not body.strip():
        raise BadMessage("empty body")
    if len(body) > MAX_BODY:
        raise BadMessage("body too large")
    try:
        root = SafeET.fromstring(body)
    except Exception as e:  # noqa: BLE001 (defusedxml raises several types)
        raise BadMessage(f"not valid XML: {type(e).__name__}") from e
    if root.tag != "xml":
        raise BadMessage("root element is not <xml>")
    return {child.tag: (child.text or "") for child in root}


def parse_message(fields: Dict[str, str]) -> Incoming:
    for need in ("ToUserName", "FromUserName", "CreateTime", "MsgType"):
        if not fields.get(need):
            raise BadMessage(f"missing {need}")
    try:
        created = int(fields["CreateTime"])
    except ValueError as e:
        raise BadMessage("CreateTime is not a number") from e
    return Incoming(
        to_user=fields["ToUserName"], from_user=fields["FromUserName"], create_time=created,
        msg_type=fields["MsgType"].strip().lower(), content=fields.get("Content", ""),
        msg_id=fields.get("MsgId", "") or fields.get("MsgID", ""), event=fields.get("Event", ""),
        event_key=fields.get("EventKey", ""), recognition=fields.get("Recognition", ""), raw=dict(fields))


def cdata(value: str) -> str:
    # "]]>" cannot sit inside one CDATA section, so it is split across two.
    return "<![CDATA[" + str(value).replace("]]>", "]]]]><![CDATA[>") + "]]>"


def text_reply(incoming: Incoming, content: str, now: Optional[int] = None) -> str:
    """Passive text reply: To and From swap round."""
    return ("<xml>"
            f"<ToUserName>{cdata(incoming.from_user)}</ToUserName>"
            f"<FromUserName>{cdata(incoming.to_user)}</FromUserName>"
            f"<CreateTime>{now if now is not None else int(time.time())}</CreateTime>"
            f"<MsgType>{cdata('text')}</MsgType>"
            f"<Content>{cdata(content)}</Content>"
            "</xml>")


def transfer_reply(incoming: Incoming, now: Optional[int] = None) -> str:
    """Hand the conversation to WeChat's own customer-service desk (the kf accounts)."""
    return ("<xml>"
            f"<ToUserName>{cdata(incoming.from_user)}</ToUserName>"
            f"<FromUserName>{cdata(incoming.to_user)}</FromUserName>"
            f"<CreateTime>{now if now is not None else int(time.time())}</CreateTime>"
            f"<MsgType>{cdata('transfer_customer_service')}</MsgType>"
            "</xml>")


def build_message(to_user: str, from_user: str, content: str, msg_id: str, create_time: Optional[int] = None,
                  msg_type: str = "text", event: str = "") -> str:
    """The XML WeChat itself would post (used by the mock and the tests)."""
    created = create_time if create_time is not None else int(time.time())
    parts = [f"<ToUserName>{cdata(to_user)}</ToUserName>", f"<FromUserName>{cdata(from_user)}</FromUserName>",
             f"<CreateTime>{created}</CreateTime>", f"<MsgType>{cdata(msg_type)}</MsgType>"]
    if msg_type == "event":
        parts.append(f"<Event>{cdata(event)}</Event>")
    elif msg_type == "voice":
        parts += [f"<MediaId>{cdata('media_' + msg_id)}</MediaId>", f"<Format>{cdata('amr')}</Format>",
                  f"<Recognition>{cdata(content)}</Recognition>", f"<MsgId>{msg_id}</MsgId>"]
    elif msg_type == "image":
        parts += [f"<PicUrl>{cdata('http://mmbiz.qpic.cn/example')}</PicUrl>",
                  f"<MediaId>{cdata('media_' + msg_id)}</MediaId>", f"<MsgId>{msg_id}</MsgId>"]
    else:
        parts += [f"<Content>{cdata(content)}</Content>", f"<MsgId>{msg_id}</MsgId>"]
    return "<xml>" + "".join(parts) + "</xml>"
