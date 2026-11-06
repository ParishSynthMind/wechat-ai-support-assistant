"""Signatures, AES safe mode (cross-checked against wechatpy, an independent implementation),
and the XML in and out."""
import base64
import re

import pytest

from wechat_support.crypto import (
    CryptoError,
    MessageCrypto,
    SignatureError,
    check_signature,
    check_timestamp,
    pkcs7_pad,
    pkcs7_unpad,
    random_encoding_aes_key,
    sha1_signature,
)
from wechat_support.xmlmsg import BadMessage, build_message, cdata, parse_message, parse_xml, text_reply, transfer_reply

APPID = "wx1234567890abcdef"


def test_signature_is_sha1_of_sorted_parts():
    import hashlib

    assert sha1_signature("tok", "1409304348", "xxxxxx") == hashlib.sha1(b"".join(
        sorted([b"tok", b"1409304348", b"xxxxxx"]))).hexdigest()


def test_check_signature_accepts_good_and_uppercase():
    sig = sha1_signature("tok", "1", "2")
    check_signature("tok", sig, "1", "2")
    check_signature("tok", sig.upper(), "1", "2")


@pytest.mark.parametrize("sig", ["", "0" * 40, "nothex"])
def test_check_signature_refuses_bad(sig):
    with pytest.raises(SignatureError):
        check_signature("tok", sig, "1", "2")


def test_timestamp_window():
    check_timestamp("1000", 300, now=1200)
    with pytest.raises(SignatureError):
        check_timestamp("1000", 300, now=1400)
    with pytest.raises(SignatureError):
        check_timestamp("abc", 300, now=1000)
    check_timestamp("1", 0, now=10**9)   # 0 switches the check off


def test_pkcs7_pads_to_32_bytes():
    for n in (0, 1, 31, 32, 33):
        padded = pkcs7_pad(b"a" * n)
        assert len(padded) % 32 == 0 and len(padded) > n
        assert pkcs7_unpad(padded) == b"a" * n
    with pytest.raises(CryptoError):
        pkcs7_unpad(b"a" * 31 + b"\x05")


def test_round_trip_and_layout():
    key = random_encoding_aes_key()
    assert len(key) == 43
    c = MessageCrypto("tok", key, APPID)
    xml = "<xml><Content><![CDATA[你好, hello]]></Content></xml>"
    enc = c.encrypt(xml, random_prefix=b"R" * 16)
    assert c.decrypt(enc) == xml
    # The plaintext layout: 16 random bytes, 4-byte big-endian length, the XML, the appid.
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    d = Cipher(algorithms.AES(c.key), modes.CBC(c.key[:16])).decryptor()
    plain = pkcs7_unpad(d.update(base64.b64decode(enc)) + d.finalize())
    assert plain[:16] == b"R" * 16 and int.from_bytes(plain[16:20], "big") == len(xml.encode())
    assert plain.endswith(APPID.encode())


def test_random_prefix_changes_ciphertext():
    c = MessageCrypto("tok", random_encoding_aes_key(), APPID)
    assert c.encrypt("<xml/>") != c.encrypt("<xml/>")


def test_message_for_another_appid_is_refused():
    key = random_encoding_aes_key()
    enc = MessageCrypto("tok", key, "wxOTHER").encrypt("<xml/>")
    with pytest.raises(CryptoError, match="another app"):
        MessageCrypto("tok", key, APPID).decrypt(enc)


@pytest.mark.parametrize("bad", ["not base64!!", base64.b64encode(b"short").decode(), ""])
def test_garbage_ciphertext_is_refused(bad):
    with pytest.raises(CryptoError):
        MessageCrypto("tok", random_encoding_aes_key(), APPID).decrypt(bad)


def test_wrong_key_is_refused():
    enc = MessageCrypto("tok", random_encoding_aes_key(), APPID).encrypt("<xml/>")
    with pytest.raises(CryptoError):
        MessageCrypto("tok", random_encoding_aes_key(), APPID).decrypt(enc)


@pytest.mark.parametrize("key", ["short", "x" * 44])
def test_bad_encoding_aes_key(key):
    with pytest.raises(ValueError):
        MessageCrypto("tok", key, APPID)


def test_msg_signature_and_reply_envelope():
    c = MessageCrypto("tok", random_encoding_aes_key(), APPID)
    envelope, enc, ts, nonce = c.encrypt_reply("<xml><Content>x</Content></xml>", timestamp="1700000000", nonce="42")
    fields = parse_xml(envelope.encode())
    assert fields["TimeStamp"] == "1700000000" and fields["Nonce"] == "42" and fields["Encrypt"] == enc
    c.check_msg_signature(fields["MsgSignature"], ts, nonce, enc)
    with pytest.raises(SignatureError):
        c.check_msg_signature(fields["MsgSignature"], ts, "43", enc)


def test_interop_wechatpy_decrypts_our_reply():
    wechatpy = pytest.importorskip("wechatpy.crypto")
    key = random_encoding_aes_key()
    xml = build_message("gh_x", "oABC", "退货 refund", "123")
    envelope, enc, ts, nonce = MessageCrypto("tok", key, APPID).encrypt_reply(xml)
    theirs = wechatpy.WeChatCrypto("tok", key, APPID)
    assert theirs.decrypt_message(envelope, sha1_signature("tok", ts, nonce, enc), ts, nonce) == xml


def test_interop_we_decrypt_wechatpy_message():
    wechatpy = pytest.importorskip("wechatpy.crypto")
    key = random_encoding_aes_key()
    xml = build_message("gh_x", "oABC", "发货时间", "456")
    out = wechatpy.WeChatCrypto("tok", key, APPID).encrypt_message(xml, "999", "1700000000")
    enc = re.search(r"<Encrypt><!\[CDATA\[(.*?)\]\]>", out).group(1)
    sig = re.search(r"<MsgSignature><!\[CDATA\[(.*?)\]\]>", out).group(1)
    ours = MessageCrypto("tok", key, APPID)
    ours.check_msg_signature(sig, "1700000000", "999", enc)
    assert ours.decrypt(enc) == xml


# ---------------------------------------------------------------- XML
def test_parse_text_message():
    m = parse_message(parse_xml(build_message("gh_1", "oU1", "hi there", "6000000001", 1700000000).encode()))
    assert (m.to_user, m.from_user, m.create_time, m.msg_type, m.text, m.msg_id) == \
        ("gh_1", "oU1", 1700000000, "text", "hi there", "6000000001")
    assert m.dedupe_key == "msg:6000000001"


def test_event_dedupe_key_uses_sender_and_time():
    m = parse_message(parse_xml(build_message("gh_1", "oU1", "", "", 1700000000, msg_type="event",
                                              event="subscribe").encode()))
    assert m.msg_id == "" and m.dedupe_key == "evt:oU1:1700000000:subscribe" and m.text == ""


def test_voice_uses_recognition_image_has_no_text():
    v = parse_message(parse_xml(build_message("gh", "o", "物流", "1", msg_type="voice").encode()))
    i = parse_message(parse_xml(build_message("gh", "o", "", "2", msg_type="image").encode()))
    assert v.text == "物流" and i.text == ""


@pytest.mark.parametrize("body,match", [
    (b"", "empty"), (b"<xml><ToUserName>a</ToUserName>", "valid XML"), (b"<root/>", "root"),
    (b"<xml><ToUserName>a</ToUserName></xml>", "missing"), (b"x" * 70000, "too large"),
])
def test_bad_bodies(body, match):
    with pytest.raises(BadMessage, match=match):
        parse_message(parse_xml(body))


def test_entity_expansion_is_refused():
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]><xml><Content>&b;</Content></xml>'
    with pytest.raises(BadMessage):
        parse_xml(bomb)


def test_external_entity_is_refused():
    xxe = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><xml><Content>&e;</Content></xml>'
    with pytest.raises(BadMessage):
        parse_xml(xxe)


def test_text_reply_swaps_users_and_survives_cdata_end():
    m = parse_message(parse_xml(build_message("gh_1", "oU1", "q", "1").encode()))
    out = parse_xml(text_reply(m, "a ]]> b <b>", now=5).encode())
    assert out == {"ToUserName": "oU1", "FromUserName": "gh_1", "CreateTime": "5", "MsgType": "text",
                   "Content": "a ]]> b <b>"}
    assert parse_xml(transfer_reply(m, now=5).encode())["MsgType"] == "transfer_customer_service"
    assert cdata("x") == "<![CDATA[x]]>"
