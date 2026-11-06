"""WeChat callback security: the URL signature and the AES "safe mode" message encryption.

Plaintext mode: every callback carries ?signature=&timestamp=&nonce=, where
signature = sha1 of token, timestamp and nonce sorted and joined. The first GET (when the
URL is saved in the WeChat admin page) also carries echostr, which is sent back as is.

Safe mode (encrypt_type=aes): the body is <xml><ToUserName/><Encrypt>...</Encrypt></xml>
and the query adds msg_signature = sha1 of sorted(token, timestamp, nonce, Encrypt).
The cipher is AES-256-CBC, key = base64decode(EncodingAESKey + "="), IV = the first 16
bytes of the key, PKCS#7 padding to a 32-byte block (not 16). The plaintext is
16 random bytes + 4-byte big-endian length + the XML + the appid (the corpid on WeCom).
Replies in safe mode are encrypted the same way and signed with a fresh timestamp/nonce.
"""
import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
from typing import Optional, Tuple

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BLOCK = 32


class SignatureError(Exception):
    """The request was not signed with our token (or was tampered with)."""


class CryptoError(Exception):
    """The encrypted body could not be decrypted, or was meant for another app."""


def sha1_signature(*parts: str) -> str:
    return hashlib.sha1("".join(sorted(str(p) for p in parts)).encode()).hexdigest()


def check_signature(token: str, signature: str, timestamp: str, nonce: str) -> None:
    if not signature or not hmac.compare_digest(sha1_signature(token, timestamp, nonce), signature.lower()):
        raise SignatureError("bad signature")


def check_timestamp(timestamp: str, max_age: int, now: Optional[float] = None) -> None:
    """Refuse a callback whose timestamp is far from our clock (replay of an old request).
    max_age 0 turns it off."""
    if not max_age:
        return
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        raise SignatureError("bad timestamp")
    if abs((now if now is not None else time.time()) - ts) > max_age:
        raise SignatureError("timestamp too old")


def pkcs7_pad(data: bytes) -> bytes:
    n = BLOCK - len(data) % BLOCK
    return data + bytes([n]) * n


def pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        raise CryptoError("empty plaintext")
    n = data[-1]
    if n < 1 or n > BLOCK or data[-n:] != bytes([n]) * n:
        raise CryptoError("bad padding")
    return data[:-n]


class MessageCrypto:
    """Encrypt and decrypt callback bodies for one app (appid on an Official Account,
    corpid on WeCom: WeChat calls it the receive id)."""

    def __init__(self, token: str, encoding_aes_key: str, receive_id: str):
        if len(encoding_aes_key or "") != 43:
            raise ValueError("EncodingAESKey must be 43 characters")
        try:
            self.key = base64.b64decode(encoding_aes_key + "=")
        except Exception as e:  # noqa: BLE001
            raise ValueError("EncodingAESKey is not valid base64") from e
        if len(self.key) != 32:
            raise ValueError("EncodingAESKey must decode to 32 bytes")
        self.token, self.receive_id = token, receive_id

    def _cipher(self):
        return Cipher(algorithms.AES(self.key), modes.CBC(self.key[:16]))

    def encrypt(self, xml: str, random_prefix: Optional[bytes] = None) -> str:
        body = xml.encode()
        prefix = random_prefix if random_prefix is not None else os.urandom(16)
        plain = pkcs7_pad(prefix + struct.pack(">I", len(body)) + body + self.receive_id.encode())
        enc = self._cipher().encryptor()
        return base64.b64encode(enc.update(plain) + enc.finalize()).decode()

    def decrypt(self, encrypted: str) -> str:
        try:
            raw = base64.b64decode(encrypted, validate=True)
        except Exception as e:  # noqa: BLE001
            raise CryptoError("Encrypt is not base64") from e
        if not raw or len(raw) % 16:
            raise CryptoError("ciphertext length is not a multiple of the AES block")
        dec = self._cipher().decryptor()
        plain = pkcs7_unpad(dec.update(raw) + dec.finalize())
        if len(plain) < 20:
            raise CryptoError("plaintext too short")
        (length,) = struct.unpack(">I", plain[16:20])
        if 20 + length > len(plain):
            raise CryptoError("length field is larger than the message")
        xml, rid = plain[20:20 + length], plain[20 + length:]
        if rid.decode(errors="replace") != self.receive_id:
            raise CryptoError("message is for another app id")
        return xml.decode()

    def check_msg_signature(self, msg_signature: str, timestamp: str, nonce: str, encrypted: str) -> None:
        good = sha1_signature(self.token, timestamp, nonce, encrypted)
        if not msg_signature or not hmac.compare_digest(good, msg_signature.lower()):
            raise SignatureError("bad msg_signature")

    def encrypt_reply(self, reply_xml: str, timestamp: Optional[str] = None,
                      nonce: Optional[str] = None) -> Tuple[str, str, str, str]:
        """Return (envelope_xml, encrypted, timestamp, nonce) for a safe-mode passive reply."""
        timestamp = timestamp or str(int(time.time()))
        nonce = nonce or str(secrets.randbelow(10**10))
        encrypted = self.encrypt(reply_xml)
        sig = sha1_signature(self.token, timestamp, nonce, encrypted)
        envelope = (f"<xml><Encrypt><![CDATA[{encrypted}]]></Encrypt>"
                    f"<MsgSignature><![CDATA[{sig}]]></MsgSignature>"
                    f"<TimeStamp>{timestamp}</TimeStamp><Nonce><![CDATA[{nonce}]]></Nonce></xml>")
        return envelope, encrypted, timestamp, nonce


def random_encoding_aes_key() -> str:
    """A fresh 43-character EncodingAESKey, the same shape WeChat's admin page generates."""
    return base64.b64encode(os.urandom(32)).decode().rstrip("=")
