"""Command line.

  wechat-assistant check                 settings, knowledge base, and a real access_token + API call
  wechat-assistant serve [--port 8080] [--workers 2]
  wechat-assistant ask "退货政策是什么?"   answer one question locally (no WeChat), shows sources
  wechat-assistant handovers             followers waiting for a person
  wechat-assistant reply OPENID "text"   a person answers (customer-service message, 48 h rules apply)
  wechat-assistant release OPENID        hand the follower back to the assistant
  wechat-assistant history [OPENID]      the conversation log
  wechat-assistant report                counts: passive vs customer-service, failures, handovers
  wechat-assistant new-aes-key           a fresh 43-character EncodingAESKey (for safe mode)
"""
import argparse
import asyncio
import json
import sys

import httpx

from . import config as config_mod
from . import lang as langmod
from .ai import make_provider
from .app import build_service, create_app
from .crypto import random_encoding_aes_key
from .kb import KnowledgeBase
from .service import mask
from .store import Store


def _cfg(args):
    return config_mod.load(args.config)


async def _check(cfg, secrets) -> int:
    ok = True
    kb = KnowledgeBase.load(cfg.knowledge_base)
    langs = sorted({s.lang for s in kb.sections})
    print(f"brand: {cfg.brand}\nknowledge base: {len(kb.sections)} sections ({', '.join(langs)})")
    print(f"AI provider: {cfg.ai.provider} ({cfg.ai.model if cfg.ai.provider == 'anthropic' else cfg.ai.gemini_model})")
    for name in ("WECHAT_TOKEN", "WECHAT_APPSECRET", "WECHAT_AES_KEY",
                 "ANTHROPIC_API_KEY" if cfg.ai.provider == "anthropic" else "GEMINI_API_KEY"):
        print(f"  {name}: {'set' if secrets.get(name) else 'MISSING'}")
        if name != "WECHAT_AES_KEY" and not secrets.get(name):
            ok = False
    if not cfg.wechat.appid:
        print("WECHAT_APPID not set: cannot test the WeChat API")
        return 1
    async with httpx.AsyncClient(timeout=10) as http:
        svc = build_service(cfg, secrets, http, provider=object())
        try:
            data = await svc.wechat.api_domain_ip()
            print(f"WeChat API: access_token OK ({cfg.wechat.token_mode}), api_domain_ip returned "
                  f"{len(data.get('ip_list', []))} ranges")
        except Exception as e:  # noqa: BLE001
            print(f"WeChat API: FAILED: {e}")
            ok = False
    return 0 if ok else 1


async def _ask(cfg, secrets, question: str) -> int:
    kb = KnowledgeBase.load(cfg.knowledge_base)
    provider = make_provider(cfg, secrets)
    lang = langmod.detect(question, cfg.default_lang)
    hits = kb.search(question, lang=lang, limit=cfg.ai.max_sections)
    ans = await provider.answer(question, hits, lang)
    print(json.dumps({"lang": lang, "status": ans.status, "answer": ans.text,
                      "sources": [hits[c - 1].section.id for c in ans.citations if 1 <= c <= len(hits)],
                      "tokens": [ans.tokens_in, ans.tokens_out]}, ensure_ascii=False, indent=2))
    return 0


async def _reply(cfg, secrets, openid: str, text: str) -> int:
    async with httpx.AsyncClient(timeout=10) as http:
        svc = build_service(cfg, secrets, http, provider=object())
        result = await svc.staff_reply(openid, text)
        print(result)
        return 0 if result == "sent" else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="wechat-assistant", description=__doc__.split("\n")[0])
    p.add_argument("--config", help="YAML settings (default config/assistant.yaml)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--workers", type=int, default=1)
    a = sub.add_parser("ask")
    a.add_argument("question")
    sub.add_parser("handovers")
    r = sub.add_parser("reply")
    r.add_argument("openid")
    r.add_argument("text")
    rel = sub.add_parser("release")
    rel.add_argument("openid")
    h = sub.add_parser("history")
    h.add_argument("openid", nargs="?")
    h.add_argument("--limit", type=int, default=50)
    sub.add_parser("report")
    sub.add_parser("new-aes-key")
    args = p.parse_args(argv)
    secrets = config_mod.Secrets()

    if args.cmd == "new-aes-key":
        print(random_encoding_aes_key())
        return 0
    cfg = _cfg(args)
    if args.cmd == "check":
        return asyncio.run(_check(cfg, secrets))
    if args.cmd == "serve":
        import uvicorn

        if args.workers > 1:
            uvicorn.run("wechat_support.cli:app_factory", factory=True, host=args.host, port=args.port,
                        workers=args.workers)
        else:
            uvicorn.run(create_app(cfg, secrets), host=args.host, port=args.port)
        return 0
    if args.cmd == "ask":
        return asyncio.run(_ask(cfg, secrets, args.question))
    if args.cmd == "reply":
        return asyncio.run(_reply(cfg, secrets, args.openid, args.text))
    store = Store(cfg.database)
    if args.cmd == "handovers":
        rows = store.handovers()
        for u in rows:
            print(f"{u['openid']}  since {u['handover_at']:.0f}  lang={u['lang'] or '?'}")
        print(f"{len(rows)} waiting")
        return 0
    if args.cmd == "release":
        store.set_handover(args.openid, False)
        print(f"{mask(args.openid)} is back with the assistant")
        return 0
    if args.cmd == "history":
        for row in store.history(args.openid, args.limit):
            print(f"{row['at']:.0f} {mask(row['openid'])} {row['direction']:5} {row['channel'] or '':16} "
                  f"{row['status'] or '':22} {(row['text'] or '')[:80]}")
        return 0
    if args.cmd == "report":
        print(json.dumps(store.counts(), indent=2))
        return 0
    return 2


def app_factory():
    """For `serve --workers N`: each worker process builds its own app from the environment."""
    return create_app(config_mod.load(), config_mod.Secrets())


if __name__ == "__main__":
    sys.exit(main())
