"""The web service: GET/POST /wechat for WeChat's callback, GET /health, and a background
loop that sends queued customer-service messages. Run it with
`wechat-assistant serve` (uvicorn; several --workers share the SQLite file)."""
import asyncio
import contextlib
import logging
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response

from .ai import make_provider
from .config import Secrets, Settings
from .kb import KnowledgeBase
from .service import SupportService
from .store import Store
from .wechat_api import Credentials, TokenManager, WeChatClient

log = logging.getLogger("wechat_support.app")


def build_service(cfg: Settings, secrets: Secrets, http: httpx.AsyncClient, provider=None,
                  ai_client=None, clock=None) -> SupportService:
    import time

    clock = clock or time.time
    store = Store(cfg.database)
    kb = KnowledgeBase.load(cfg.knowledge_base)
    provider = provider or make_provider(cfg, secrets, client=ai_client)
    wechat = None
    if cfg.wechat.appid:
        tokens = TokenManager(store, Credentials(cfg.wechat.appid, secrets.require("WECHAT_APPSECRET")), http,
                              cfg.wechat.api_base, mode=cfg.wechat.token_mode, clock=clock)
        wechat = WeChatClient(tokens, http, cfg.wechat.api_base)
    return SupportService(cfg, store, kb, provider, wechat, token=secrets.require("WECHAT_TOKEN"),
                          aes_key=secrets.get("WECHAT_AES_KEY"), http=http, clock=clock)


def create_app(cfg: Settings, secrets: Optional[Secrets] = None, service: Optional[SupportService] = None,
               sweep_every: float = 2.0) -> FastAPI:
    secrets = secrets or Secrets()
    state = {}

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        http = httpx.AsyncClient(timeout=10)
        svc = service or build_service(cfg, secrets, http)
        state["svc"] = svc

        async def loop():
            while True:
                await asyncio.sleep(sweep_every)
                try:
                    if svc.wechat is not None:
                        await svc.sweep()
                except Exception:  # noqa: BLE001 (the loop must survive one bad pass)
                    log.exception("sweep failed")

        task = asyncio.create_task(loop())
        try:
            yield
        finally:
            task.cancel()
            await http.aclose()

    app = FastAPI(title="WeChat AI support assistant", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.get("/health")
    async def health():
        svc = state["svc"]
        return {"ok": True, "brand": cfg.brand, "provider": getattr(svc.provider, "name", "?"),
                "kb_sections": len(svc.kb.sections), "encryption": "on" if svc.crypto else "off"}

    @app.get("/wechat")
    async def verify(request: Request):
        r = state["svc"].verify_url(dict(request.query_params))
        return PlainTextResponse(r.body, status_code=r.status)

    @app.post("/wechat")
    async def callback(request: Request):
        body = await request.body()
        r = await state["svc"].handle_post(dict(request.query_params), body)
        return Response(r.body, status_code=r.status, media_type=r.media_type)

    app.state.svc = state
    return app
