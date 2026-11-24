import datetime as dt
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from app.api.market import router as market_router
from app.api.routes import router
from app.api.strategy import router as strategy_router
from app.api.paper import router as paper_router
from app.api.journal import router as journal_router
from app.api.options import router as options_router
from app.api.webhooks import router as webhooks_router
from app.api.lab import router as lab_router
from app.api.live import router as live_router
from app.api.monitor import router as monitor_router
from app.api.forward import router as forward_router
from app.auth import current_user
from app.config import get_settings
from app.db import DBError
from app.global_markets.api import router as global_router
from app.telegram.api import router as telegram_router
from app.global_markets.data.backfill import BackfillThread
from app.market import nse
from app.paper.service import Loop
from app.lab.registry import register_strategies


@asynccontextmanager
async def lifespan(app: FastAPI):
    await run_in_threadpool(nse.load_holidays)   # NSE's own list; the built-in 2026 list if NSE is down
    if get_settings().supabase_service_role_key:
        try:
            await run_in_threadpool(register_strategies)     # the learning loop's variants point at these rows
        except Exception as e:  # noqa: BLE001 - the app still runs without the registry
            logging.getLogger("startup").warning("could not register strategies: %s", e)
    loop = None
    if get_settings().supabase_service_role_key:
        loop = Loop()                              # paper fills, stops, targets and the 15:15 exit
        loop.start()
        app.state.paper_loop = loop
    live = feed_loop = None
    if get_settings().supabase_service_role_key and get_settings().kite_configured:
        from app.live.service import LiveLoop
        live = LiveLoop()                          # live orders: fills, stops, exits, reconciliation
        live.start()
        app.state.live_loop = live
        from app.market import ticks
        feed_loop = ticks.loop = ticks.FeedLoop()  # Kite ticks -> bars, while you are logged in to Kite today
        feed_loop.start()
    global_loop = global_scan = global_agent = None
    if get_settings().supabase_service_role_key:
        from app.global_markets.paper import Loop as GlobalLoop
        global_loop = GlobalLoop()                 # XAUUSD paper: fills, stops, targets and the flat-by exit
        global_loop.start()
        from app.global_markets.scanner import ScanLoop
        global_scan = ScanLoop()                   # XAUUSD live signals (never trades: you make a ticket and confirm it)
        global_scan.start()
        from app.global_markets.agent.desk import AgentLoop
        global_agent = AgentLoop()                 # the gold agent: looks when something happens, proposes; you confirm
        global_agent.start()
    telegram = None
    if get_settings().supabase_service_role_key and get_settings().telegram_bot_token:
        from app.global_markets import paper as global_paper
        from app.global_markets.agent import desk as agent_desk
        from app.telegram import service as tg
        agent_desk.listeners.append(tg.on_idea)    # live ideas, fills and exits go to your linked Telegram chat
        global_paper.listeners.append(tg.on_paper)
        telegram = tg.TelegramLoop()
        telegram.start()
    backfill = None
    if get_settings().global_backfill_enabled:
        backfill = BackfillThread("XAUUSD", dt.date.fromisoformat(get_settings().global_backfill_start))
        backfill.start_thread()
    recorder = None
    if get_settings().chain_recorder_enabled:
        from app.fno_research.recorder import Recorder
        recorder = Recorder()                      # NIFTY option chain every 5 minutes in market hours, local files
        recorder.start()
    yield
    if telegram:
        telegram.stop()
    if recorder:
        recorder.stop()
    if backfill:
        backfill.stop()
    if global_loop:
        global_loop.stop()
    if global_scan:
        global_scan.stop()
    if global_agent:
        global_agent.stop()
    if loop:
        loop.stop()
    if live:
        live.stop()
    if feed_loop:
        feed_loop.stop()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="TradingBot API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])
    app.include_router(router, prefix="/api")
    app.include_router(market_router, prefix="/api")
    app.include_router(strategy_router, prefix="/api")
    app.include_router(paper_router, prefix="/api")
    app.include_router(journal_router, prefix="/api")
    app.include_router(options_router, prefix="/api")
    app.include_router(webhooks_router, prefix="/api")
    app.include_router(lab_router, prefix="/api")
    app.include_router(live_router, prefix="/api")
    app.include_router(monitor_router, prefix="/api")
    app.include_router(forward_router, prefix="/api")
    app.include_router(global_router, prefix="/api")
    app.include_router(telegram_router, prefix="/api")

    @app.exception_handler(DBError)
    async def database_unavailable(_request: Request, e: DBError):
        # inside the CORS layer, so the browser can read the reason instead of reporting a CORS error
        logging.getLogger("db").warning("database error: %s", e)
        return JSONResponse({"detail": f"database temporarily unavailable: {e}"}, status_code=503)

    # On a server (REQUIRE_AUTH=true) every API route needs a signed-in user, except these.
    public = ("/api/health", "/api/webhooks/tradingview/", "/api/live/kite/callback")

    @app.middleware("http")
    async def require_auth(request: Request, call_next):
        s = get_settings()
        path = request.url.path
        if s.require_auth and path.startswith("/api") and request.method != "OPTIONS" and not path.startswith(public):
            try:
                await current_user(request.headers.get("authorization", ""), s)
            except HTTPException as e:
                return JSONResponse({"detail": e.detail}, status_code=e.status_code)
        return await call_next(request)
    return app


app = create_app()
