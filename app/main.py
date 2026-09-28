import asyncio
import logging
from contextlib import asynccontextmanager

from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.browser.manager import BrowserManager
from app.config import get_settings, load_gateway_config
from app.logger import setup_logging
from app.middleware.rate_limit import SlidingWindowLimiter
from app import dashboard
from app.routes import chat, health, models
from app.services import GatewayService
from app.storage import RequestStore

# Setup logging immediately on module import
setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    app.state.settings = settings

    # Initialize Browser Manager
    browser_manager = BrowserManager(
        user_data_dir=settings.chrome_user_data_dir,
        headless=settings.browser_headless,
        cdp_url=settings.browser_cdp_url,
    )
    app.state.browser = browser_manager

    # Optional config for API fallback
    config = {}
    try:
        config = load_gateway_config(settings.config_path)
    except Exception:
        pass

    app.state.gateway = GatewayService(
        config=config,
        browser_manager=browser_manager,
        enable_api_keys=settings.enable_api_keys,
    )
    app.state.store = RequestStore(str(settings.database_path))
    await app.state.store.initialize()
    app.state.global_limiter = SlidingWindowLimiter(settings.global_rate_limit)
    app.state.user_limiter = SlidingWindowLimiter(settings.user_rate_limit)

    # Initialize browser in background if enabled
    if settings.browser_enabled:
        async def _warmup():
            try:
                await browser_manager.warmup()
                logger.info("Browser automation context warmed up and ready.")
            except Exception as e:
                logger.warning(f"Browser warmup notice (will retry on first request): {e}")
        asyncio.create_task(_warmup())

    yield

    # Shutdown
    try:
        await browser_manager.close()
    except Exception:
        pass


app = FastAPI(
    title="jugaad-llm",
    version="0.2.0",
    description="Conjure unlimited free LLM tokens from your free ChatGPT, Claude, Gemini, DeepSeek, and Grok web sessions into a drop-in OpenAI-compatible API gateway.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.mount("/assets", StaticFiles(directory=Path(__file__).parent.parent / "assets"), name="assets")
app.include_router(chat.router)
app.include_router(models.router)
app.include_router(health.router)
app.include_router(dashboard.router)
