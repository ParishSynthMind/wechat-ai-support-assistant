# WeChat facts this build relies on

Checked against WeChat's developer documentation on 6 November 2026 (the docs now live under
`developers.weixin.qq.com/doc/service/`; the older `/doc/offiaccount/` links return 404).
Where the documentation did not say, the choice made in the code and the fake is marked **assumed**.

| Fact | Status | Where it is used |
| --- | --- | --- |
| Callback signature = sha1 of token, timestamp, nonce sorted and joined; the URL check echoes `echostr` | Confirmed | `crypto.check_signature`, `GET /wechat` |
| Safe mode: `msg_signature` = sha1 of sorted token, timestamp, nonce, Encrypt; AES-CBC, key = base64(EncodingAESKey + "="), 32 bytes, PKCS#7 to 32-byte blocks; plaintext = 16 random bytes + 4-byte network-order length + XML + appid | Confirmed | `crypto.MessageCrypto` |
| IV = first 16 bytes of the key | Not stated on the page (it points to the sample code); confirmed by interoperating with wechatpy in both directions, in the tests | `crypto.MessageCrypto` |
| No HTTP reply within 5 s: WeChat drops the connection and retries, three times in all | Confirmed | `service.py`, `FakeWeChatPush` |
| Reply `success` or an empty body means "no reply" | Confirmed | `service.SUCCESS` |
| Dedupe: MsgId for messages, FromUserName + CreateTime for events | Confirmed | `xmlmsg.Incoming.dedupe_key` |
| access_token valid 7,200 s; `/cgi-bin/token` limited to 2,000 a day (200 on a test account) | Confirmed | `TokenManager`, the fake |
| A new `/cgi-bin/token` token leaves the old one valid for about 5 more minutes | **Assumed** (not found on the page this time) | the fake only |
| `POST /cgi-bin/stable_token`: same token until 5 min before expiry; `force_refresh` makes a new one and kills the old, limited to 20 a day, at least 30 s apart | Confirmed | `TokenManager` (default mode) |
| Customer-service messages: within 48 h of the follower's last message, at most 5 per message they send (3 within 1 minute after a menu click, follow or scan) | Confirmed | `Store.take_cs_slot`, `cs_per_window: 5` |
| Error 45015 outside the 48-hour window or after unfollowing; 45047 over the count | Codes **assumed** from common client libraries (wechatpy's list); the docs pages did not list them | `PERMANENT_CS_ERRORS`, the fake |
| 40001 / 40014 / 42001 token errors, 40013 appid, 40125 secret, 40003 openid, 43004 not following, 45009 daily quota, 45011 minute quota, -1 busy | Codes as in wechatpy's constants | `wechat_api.py` |
| Maximum length of a text reply | Not stated; the fake refuses over 2,048 bytes (**assumed**) and the service cuts answers to 600 characters | `service._clip` |
| Test account: log in by scanning a QR code with WeChat at `mp.weixin.qq.com/debug/cgi-bin/sandbox?t=sandbox/login`; no Official Account needed; advanced APIs can be tested; 50,000 customer-service messages a day | Confirmed (public page) | ACCOUNT-NEEDS |
| Test account settings show appID, appsecret, a URL and a Token; whether it offers EncodingAESKey (safe mode) | Only visible after logging in: to be checked in the live test. Expect plaintext only. | live test plan |
| Callback URL on port 80 or 443 | Confirmed for normal accounts | the tunnel gives https on 443 |
| Registration and verification of an Official Account by a business outside mainland China (fee, documents) | Not found in official pages; to be confirmed with WeChat by the business that owns the account | README "What a real account needs" |
