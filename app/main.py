"""
FastAPI Application Entry Point.

This is the main application file that:
1. Creates the FastAPI app with lifespan management
2. Configures CORS middleware
3. Mounts all API routers under /api/v1/
4. Starts the scheduler as a background thread on startup
5. Wires the scheduler → LiveCache → WebSocket pipeline

Architecture:
    Scheduler (background thread)
        → LiveCache.update() callback
            → WebSocket Publisher broadcasts delta
    
    API Handlers read from LiveCache (never from S3 for live queries)
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config.settings import get_settings
from app.cache.live_cache import live_cache

logger = logging.getLogger(__name__)

# Configure root logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

# ── Globals initialized during lifespan ─────────────────────────────────
scheduler_instance = None
ws_publisher = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan handler.
    
    Startup:
        - Initialize and start the Upstox scheduler in a background thread
        - Register LiveCache as the scheduler's data callback
        - Initialize the WebSocket publisher
    
    Shutdown:
        - Stop the scheduler gracefully
    """
    global scheduler_instance, ws_publisher
    settings = get_settings()

    # ── Startup ─────────────────────────────────────────────────────────
    logger.info("Starting MarketPulse backend...")

    from app.services.history_service import get_cache_manager
    get_cache_manager().startup_recovery()

    # Wire the WebSocket publisher to the async event loop
    import asyncio
    from app.websocket.publisher import set_event_loop, on_cache_updated

    loop = asyncio.get_running_loop()
    set_event_loop(loop)

    if settings.SCHEDULER_ENABLED:
        try:
            from scheduler.upstox_equity import UpstoxScheduler

            scheduler_instance = UpstoxScheduler(settings)

            # Single callback: update the cache AND immediately push the
            # changed rows to WebSocket clients using update()'s return value.
            # This eliminates the double-diff race that previously caused
            # WebSocket broadcasts to be suppressed (falling back to polling).
            def _on_scheduler_data(df):
                """Bridge: scheduler data → LiveCache → WebSocket delta push."""
                changed_rows = live_cache.update(df)
                if changed_rows:
                    on_cache_updated(changed_rows)

            scheduler_instance.register_callback(_on_scheduler_data)

            # Start scheduler in its own background thread (APScheduler handles this)
            scheduler_instance.start()
            logger.info("Scheduler started successfully")
        except Exception as e:
            logger.error(f"Failed to start scheduler: {e}")
            logger.info("API will start without live data (historical mode only)")
    else:
        logger.info("Scheduler disabled via settings")

    # Store references on app state for access from route handlers
    app.state.live_cache = live_cache
    app.state.scheduler = scheduler_instance

    logger.info("MarketPulse backend ready")

    yield

    # ── Shutdown ────────────────────────────────────────────────────────
    logger.info("Shutting down MarketPulse backend...")
    if scheduler_instance:
        scheduler_instance.stop()
    logger.info("Shutdown complete")


def create_app() -> FastAPI:
    """
    Application factory.
    
    Creates and configures the FastAPI application with all middleware,
    routers, and the WebSocket endpoint.
    """
    settings = get_settings()

    app = FastAPI(
        title="MarketPulse API",
        description="Live Market Analytics Platform — REST API & WebSocket",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    # ── CORS Middleware ─────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── API Routers ─────────────────────────────────────────────────────
    from app.api.dashboard import router as dashboard_router
    from app.api.stocks import router as stocks_router
    from app.api.history import router as history_router
    from app.api.scanner import router as scanner_router
    from app.api.metadata import router as metadata_router
    from app.api.market_status import router as market_status_router
    from app.api.health import router as health_router

    api_prefix = "/api/v1"
    app.include_router(dashboard_router, prefix=api_prefix, tags=["Dashboard"])
    app.include_router(stocks_router, prefix=api_prefix, tags=["Stocks"])
    app.include_router(history_router, prefix=api_prefix, tags=["History"])
    app.include_router(scanner_router, prefix=api_prefix, tags=["Scanner"])
    app.include_router(metadata_router, prefix=api_prefix, tags=["Metadata"])
    app.include_router(market_status_router, prefix=api_prefix, tags=["Market Status"])
    app.include_router(health_router, prefix=api_prefix, tags=["Health"])

    # ── WebSocket Endpoint ──────────────────────────────────────────────
    from app.websocket.connection_manager import manager
    from fastapi import WebSocket, WebSocketDisconnect

    @app.websocket("/api/v1/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await manager.connect(websocket)
        try:
            while True:
                # Keep connection alive, handle client messages
                data = await websocket.receive_text()
                await manager.handle_client_message(websocket, data)
        except WebSocketDisconnect:
            manager.disconnect(websocket)

    return app


# ── Application Instance ────────────────────────────────────────────────
# Uvicorn will import this: uvicorn app.main:app
app = create_app()
