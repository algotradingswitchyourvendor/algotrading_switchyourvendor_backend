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

from app.utils.log_capture import log_capture_handler

# Configure root logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        log_capture_handler
    ]
)

# ── Globals initialized during lifespan ─────────────────────────────────
scheduler_instance = None
ws_publisher = None


async def _seed_subscription_plans():
    """
    Seed subscription plan definitions in the database on startup.

    Creates plan rows if they don't exist. For paid plans, creates
    corresponding Razorpay plans if razorpay_plan_id is not yet set.

    This is idempotent — safe to call on every startup.
    """
    from app.db.database import get_session_factory
    from app.db.models import SubscriptionPlan
    from app.entitlements.plans import PLAN_FEATURES, PLAN_PRICES
    from sqlalchemy import select

    plans_config = [
        {"name": "FREE", "price_inr": 0},
        {"name": "BASIC", "price_inr": 99},
        {"name": "PRO", "price_inr": 149},
        {"name": "ULTRA", "price_inr": 199},
    ]

    factory = get_session_factory()
    async with factory() as db:
        for plan_config in plans_config:
            name = plan_config["name"]
            result = await db.execute(
                select(SubscriptionPlan).where(SubscriptionPlan.name == name)
            )
            plan = result.scalar_one_or_none()

            if not plan:
                plan = SubscriptionPlan(
                    name=name,
                    price_inr=plan_config["price_inr"],
                    features=PLAN_FEATURES.get(name, {}),
                )
                db.add(plan)
                await db.flush()
                logger.info(f"Created subscription plan: {name}")

            # Create Razorpay plan for paid plans
            if plan_config["price_inr"] > 0 and not plan.razorpay_plan_id:
                settings = get_settings()
                if settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET:
                    try:
                        from app.services.razorpay_service import create_razorpay_plan
                        razorpay_plan_id = await create_razorpay_plan(
                            name=f"MarketPulse {name}",
                            amount_inr=plan_config["price_inr"],
                        )
                        plan.razorpay_plan_id = razorpay_plan_id
                        logger.info(f"Created Razorpay plan for {name}: {razorpay_plan_id}")
                    except Exception as e:
                        logger.warning(f"Could not create Razorpay plan for {name}: {e}")

        await db.commit()


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

    # ── Database & Redis Startup ───────────────────────────────────────────
    logger.info("Starting MarketPulse backend...")

    # Initialize PostgreSQL
    if settings.DATABASE_URL:
        try:
            from app.db.database import init_db
            await init_db()
            logger.info("PostgreSQL connected (tables verified)")

            # Seed subscription plans and create Razorpay plans if needed
            await _seed_subscription_plans()

            logger.info("Subscription plans seeded")
        except Exception as e:
            logger.error(f"Database initialization failed: {e}")
            logger.warning("API will start without database — auth and billing disabled")
    else:
        logger.warning("DATABASE_URL not set — auth, billing, and presets disabled")

    # Initialize Redis
    try:
        from app.redis_client import init_redis
        redis_client = await init_redis()
        app.state.redis = redis_client
    except Exception as e:
        logger.warning(f"Redis initialization failed: {e} — rate limiting disabled")
        app.state.redis = None

    # ── Market Data Startup ───────────────────────────────────────────────

    from app.services.history_service import get_cache_manager
    get_cache_manager().startup_recovery()

    # Wire the WebSocket publisher to the async event loop
    import asyncio
    from app.websocket.publisher import set_event_loop, on_cache_updated

    loop = asyncio.get_running_loop()
    set_event_loop(loop)

    if settings.SCHEDULER_ENABLED:
        try:
            from scheduler.upstox_variables import UpstoxScheduler

            scheduler_instance = UpstoxScheduler(settings)

            # Single callback: update the cache AND immediately push the
            # changed rows to WebSocket clients using update()'s return value.
            # This eliminates the double-diff race that previously caused
            def _on_scheduler_data(df):
                """Bridge: scheduler data → LiveCache → WebSocket delta push."""
                import time
                t0 = time.time()
                changed_rows = live_cache.update(df)
                t_cache = time.time() - t0

                t_cb = 0.0
                if changed_rows:
                    t0 = time.time()
                    on_cache_updated(changed_rows)
                    t_cb = time.time() - t0
                
                return t_cache, t_cb

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

    # ── Shutdown ────────────────────────────────────────────────────────────
    logger.info("Shutting down MarketPulse backend...")
    if scheduler_instance:
        scheduler_instance.stop()
    from app.redis_client import close_redis
    await close_redis()
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
    from app.api.presets import router as presets_router

    api_prefix = "/api/v1"
    app.include_router(dashboard_router, prefix=api_prefix, tags=["Dashboard"])
    app.include_router(stocks_router, prefix=api_prefix, tags=["Stocks"])
    app.include_router(history_router, prefix=api_prefix, tags=["History"])
    app.include_router(scanner_router, prefix=api_prefix, tags=["Scanner"])
    app.include_router(metadata_router, prefix=api_prefix, tags=["Metadata"])
    app.include_router(market_status_router, prefix=api_prefix, tags=["Market Status"])
    app.include_router(health_router, prefix=api_prefix, tags=["Health"])
    app.include_router(presets_router, prefix=api_prefix, tags=["Presets"])

    # ── New SaaS Routers ───────────────────────────────────────────────────
    from app.api.auth import router as auth_router
    from app.api.admin import router as admin_router
    from app.api.subscriptions import router as subscriptions_router
    from app.api.payments import router as payments_router
    from app.api.support import router as support_router
    from app.api.users import router as users_router

    app.include_router(auth_router, prefix=api_prefix)
    app.include_router(admin_router, prefix=api_prefix)
    app.include_router(subscriptions_router, prefix=api_prefix)
    app.include_router(payments_router, prefix=api_prefix)
    app.include_router(support_router, prefix=api_prefix)
    app.include_router(users_router, prefix=api_prefix)

    # ── WebSocket Endpoint ──────────────────────────────────────────────
    from app.websocket.connection_manager import manager
    from fastapi import WebSocket, WebSocketDisconnect

    @app.websocket("/api/v1/ws")
    async def websocket_endpoint(websocket: WebSocket):
        try:
            await manager.connect(websocket)
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
