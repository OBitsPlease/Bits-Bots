import asyncio
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Literal

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .advisor import answer_market_question, recommend_bot
from .auth import AuthService, AuthenticationError
from .live_account import (
    LiveAccountError,
    decrypt_secret,
    encrypt_secret,
    fetch_live_account_snapshot,
    fetch_live_grid_bots,
    mask_access_key,
)
from .llm_assistant import LlmUnavailableError, answer_with_llm, is_available
from .models import User
from .nonkyc import NonKycError
from .execution import LiveTradingService, TradingError
from .exchange import ExchangeError
from .live_bots import GridEngine
from .service import TradingService

STATIC_DIR = Path(__file__).parent / "static"
BOOT_ID = secrets.token_hex(8)
service = TradingService()
auth = AuthService(service.storage, service.settings)
live = LiveTradingService(service.storage, service.settings)
grid_engine = GridEngine(live)
LIVE_MAINTENANCE_SECONDS = 15


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=1024)


class RegisterRequest(LoginRequest):
    display_name: str = Field(min_length=2, max_length=80)


class BotCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    symbol: str = Field(min_length=3, max_length=30)
    strategy: Literal["Multi-TF", "Grid", "Adaptive Grid"] = "Multi-TF"
    risk_level: Literal["low", "medium", "high"] = "medium"
    entry_score: float = Field(default=68, ge=50, le=95)
    max_allocation: float = Field(default=0.10, ge=0.01, le=0.50)
    lower_price: float | None = Field(default=None, gt=0)
    upper_price: float | None = Field(default=None, gt=0)
    grid_slices: int | None = Field(default=None, ge=3, le=30)
    grid_stop_price: float | None = Field(default=None, gt=0)


class BotStatusRequest(BaseModel):
    status: Literal["running", "paused"]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1000)


class ExchangeCredentialsRequest(BaseModel):
    access_key: str = Field(min_length=8, max_length=200)
    secret_key: str = Field(min_length=8, max_length=200)


def current_user(request: Request) -> User:
    token = request.cookies.get(service.settings.session_cookie_name)
    user = auth.authenticate(token)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    return user


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=service.settings.session_cookie_name,
        value=token,
        max_age=service.settings.session_lifetime_days * 86_400,
        httponly=True,
        secure=False,
        samesite="strict",
        path="/",
    )


async def live_maintenance_loop() -> None:
    while True:
        try:
            await live.maintenance_cycle()
            await grid_engine.run_cycle()
        except Exception:  # noqa: BLE001 - the reconciliation loop must never die
            pass
        await asyncio.sleep(LIVE_MAINTENANCE_SECONDS)


@asynccontextmanager
async def lifespan(_: FastAPI):
    live.recover_on_startup()
    scanner_task = asyncio.create_task(service.run_forever())
    live_task = asyncio.create_task(live_maintenance_loop())
    yield
    for task in (scanner_task, live_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await live.close()
    await service.close()


app = FastAPI(title="BitsBots", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index(request: Request) -> Response:
    if auth.authenticate(request.cookies.get(service.settings.session_cookie_name)) is None:
        return RedirectResponse("/login", status_code=303)
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/login", include_in_schema=False)
async def login_page(request: Request) -> Response:
    if auth.authenticate(request.cookies.get(service.settings.session_cookie_name)):
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/api/auth/setup")
async def setup_state() -> dict[str, bool]:
    return {"first_run": service.storage.user_count() == 0}


@app.post("/api/auth/register")
async def register(payload: RegisterRequest, response: Response) -> dict:
    try:
        session = auth.register(payload.email, payload.display_name, payload.password)
    except (AuthenticationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    set_session_cookie(response, session.token)
    return {"user": session.user}


@app.post("/api/auth/login")
async def login(payload: LoginRequest, response: Response) -> dict:
    try:
        session = auth.login(payload.email, payload.password)
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    set_session_cookie(response, session.token)
    return {"user": session.user}


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response) -> dict[str, str]:
    auth.logout(request.cookies.get(service.settings.session_cookie_name))
    response.delete_cookie(
        service.settings.session_cookie_name, path="/", samesite="strict"
    )
    return {"status": "logged out"}


@app.get("/api/auth/me")
async def me(user: User = Depends(current_user)) -> dict:
    return {"user": user}


@app.get("/api/state")
async def state(user: User = Depends(current_user)) -> dict:
    return service.snapshot(user.id)


@app.post("/api/bots", status_code=201)
async def create_bot(
    payload: BotCreateRequest, user: User = Depends(current_user)
) -> dict:
    normalized_symbol = payload.symbol.strip().upper()
    available_markets = {
        market["symbol"]: market for market in await service.available_markets()
    }
    market = available_markets.get(normalized_symbol)
    if market is None:
        raise HTTPException(
            status_code=400,
            detail="Select an active NonKYC USDT or USDC market",
        )
    is_grid = payload.strategy in {"Grid", "Adaptive Grid"}
    grid_values = (
        payload.lower_price,
        payload.upper_price,
        payload.grid_slices,
        payload.grid_stop_price,
    )
    if is_grid and any(value is None for value in grid_values):
        raise HTTPException(
            status_code=400,
            detail="Grid bots require lower price, upper price, slices, and stop price",
        )
    if is_grid:
        assert payload.lower_price is not None
        assert payload.upper_price is not None
        assert payload.grid_slices is not None
        assert payload.grid_stop_price is not None
        if not (
            payload.grid_stop_price
            < payload.lower_price
            < market["last_price"]
            < payload.upper_price
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Grid range must contain the current price, and the stop "
                    "must be below the lower price"
                ),
            )
        spacing_rate = (
            (payload.upper_price - payload.lower_price)
            / payload.grid_slices
            / market["last_price"]
        )
        minimum_spacing = 2 * (
            service.settings.fee_rate + service.settings.slippage_rate
        )
        if spacing_rate <= minimum_spacing:
            raise HTTPException(
                status_code=400,
                detail="Grid slices are too close together to cover modeled fees and slippage",
            )
    try:
        bot = service.storage.create_bot(
            user.id,
            payload.name,
            normalized_symbol,
            payload.strategy,
            payload.risk_level,
            payload.entry_score,
            payload.max_allocation,
            payload.lower_price if is_grid else None,
            payload.upper_price if is_grid else None,
            payload.grid_slices if is_grid else None,
            payload.grid_stop_price if is_grid else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"bot": asdict(bot)}


@app.get("/api/bots/recommendation")
async def bot_recommendation(
    strategy: Literal["Multi-TF", "Grid", "Adaptive Grid"] = "Multi-TF",
    user: User = Depends(current_user),
) -> dict:
    try:
        recommendation = recommend_bot(
            service.signals,
            service.storage.list_bots(user.id),
            strategy,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"recommendation": recommendation}


@app.get("/api/markets")
async def available_markets(_: User = Depends(current_user)) -> dict:
    return {"markets": await service.available_markets()}


@app.patch("/api/bots/{bot_id}/status")
async def update_bot_status(
    bot_id: int,
    payload: BotStatusRequest,
    user: User = Depends(current_user),
) -> dict:
    bot = service.storage.update_bot_status(user.id, bot_id, payload.status)
    if bot is None:
        raise HTTPException(status_code=404, detail="Paper bot not found")
    return {"bot": asdict(bot)}


@app.delete("/api/bots/{bot_id}")
async def delete_bot(
    bot_id: int, user: User = Depends(current_user)
) -> dict[str, str]:
    if not service.storage.delete_bot(user.id, bot_id):
        raise HTTPException(status_code=404, detail="Paper bot not found")
    return {"status": "deleted"}


@app.post("/api/assistant/chat")
async def assistant_chat(
    payload: ChatRequest, user: User = Depends(current_user)
) -> dict:
    if not payload.message.strip():
        raise HTTPException(status_code=400, detail="Enter a question")
    snapshot = service.snapshot(user.id)
    bots = service.storage.list_bots(user.id)
    positions = service.storage.all_positions(user.id)
    cash = snapshot["account"]["cash"]
    equity = snapshot["account"]["equity"]

    if await is_available():
        live_grid_bots: dict | None = None
        stored = service.storage.get_exchange_credentials(user.id)
        if stored is not None:
            try:
                access_key, encrypted_secret = stored
                secret_key = decrypt_secret(encrypted_secret, service.settings)
                live_grid_bots = await fetch_live_grid_bots(
                    access_key, secret_key, service.settings
                )
            except LiveAccountError:
                live_grid_bots = None
        try:
            return await answer_with_llm(
                payload.message,
                service.signals,
                bots,
                positions,
                cash,
                equity,
                live_grid_bots,
            )
        except LlmUnavailableError:
            pass  # fall back to the rule-based assistant below

    try:
        return answer_market_question(
            payload.message, service.signals, bots, positions, cash, equity
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/candles/{symbol:path}")
async def candles(
    symbol: str, period: int = 60, _: User = Depends(current_user)
) -> list[dict]:
    if period not in (15, 60, 240, 1440):
        raise HTTPException(status_code=400, detail="Unsupported candle period")
    try:
        stored = await service.market_candles(
            symbol, period, limit=100, max_age_ms=period * 60_000
        )
    except NonKycError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return [
        {
            "timestamp": candle.timestamp,
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "volume": candle.volume,
        }
        for candle in stored
    ]


@app.post("/api/scan")
async def scan(_: User = Depends(current_user)) -> dict[str, str]:
    if service._scan_lock.locked():
        raise HTTPException(status_code=409, detail="A scan is already running")
    asyncio.create_task(service.scan_now())
    return {"status": "scan started"}


@app.get("/api/live-account/credentials")
async def live_account_credentials(user: User = Depends(current_user)) -> dict:
    stored = service.storage.get_exchange_credentials(user.id)
    if stored is None:
        return {"connected": False, "masked_access_key": None}
    access_key, _ = stored
    return {"connected": True, "masked_access_key": mask_access_key(access_key)}


@app.post("/api/live-account/credentials", status_code=201)
async def save_live_account_credentials(
    payload: ExchangeCredentialsRequest, user: User = Depends(current_user)
) -> dict:
    access_key = payload.access_key.strip()
    secret_key = payload.secret_key.strip()
    try:
        await fetch_live_account_snapshot(access_key, secret_key, service.settings)
    except LiveAccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    encrypted_secret = encrypt_secret(secret_key, service.settings)
    service.storage.save_exchange_credentials(user.id, access_key, encrypted_secret)
    await live.credentials_changed(user.id)
    return {"connected": True, "masked_access_key": mask_access_key(access_key)}


@app.delete("/api/live-account/credentials")
async def delete_live_account_credentials(
    user: User = Depends(current_user),
) -> dict[str, str]:
    service.storage.delete_exchange_credentials(user.id)
    await live.credentials_changed(user.id)
    return {"status": "disconnected"}


@app.get("/api/live-account/snapshot")
async def live_account_snapshot(user: User = Depends(current_user)) -> dict:
    stored = service.storage.get_exchange_credentials(user.id)
    if stored is None:
        raise HTTPException(
            status_code=404,
            detail="No NonKYC API credentials are connected yet",
        )
    access_key, encrypted_secret = stored
    try:
        secret_key = decrypt_secret(encrypted_secret, service.settings)
        return await fetch_live_account_snapshot(access_key, secret_key, service.settings)
    except LiveAccountError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/live-account/grid-bots")
async def live_account_grid_bots(user: User = Depends(current_user)) -> dict:
    stored = service.storage.get_exchange_credentials(user.id)
    if stored is None:
        raise HTTPException(
            status_code=404,
            detail="No NonKYC API credentials are connected yet",
        )
    access_key, encrypted_secret = stored
    try:
        secret_key = decrypt_secret(encrypted_secret, service.settings)
        snapshot = await fetch_live_grid_bots(access_key, secret_key, service.settings)
    except LiveAccountError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    service.storage.record_grid_bot_pnl(user.id, snapshot.get("bots", []))
    return snapshot


@app.get("/api/live-account/grid-bots/history")
async def live_account_grid_bots_history(
    since_hours: float = 168, user: User = Depends(current_user)
) -> dict:
    since_ms = int(time.time() * 1000) - int(since_hours * 3_600_000)
    history = service.storage.get_grid_bot_pnl_history(user.id, since_ms)
    return {"history": history}


class LiveEnableRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)
    ip_address: str = Field(min_length=3, max_length=64)
    trade_permission_confirmed: bool
    withdrawals_disabled_confirmed: bool
    ip_allowlisted_confirmed: bool
    risk_acknowledged: bool
    typed_phrase: str = Field(max_length=64)


class PasswordRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class LiveIpRequest(BaseModel):
    ip_address: str = Field(min_length=3, max_length=64)


class KillSwitchRequest(BaseModel):
    engaged: bool
    password: str | None = Field(default=None, max_length=1024)


class ModeRequest(BaseModel):
    mode: Literal["dry_run", "live"]


class OrderPreviewRequest(BaseModel):
    mode: Literal["dry_run", "live"] = "dry_run"
    symbol: str = Field(min_length=3, max_length=30)
    side: Literal["buy", "sell"]
    price: str = Field(min_length=1, max_length=40)
    quantity: str = Field(min_length=1, max_length=40)


class OrderConfirmRequest(BaseModel):
    preview_id: str = Field(min_length=8, max_length=64)
    password: str | None = Field(default=None, max_length=1024)


class ResolveRequest(BaseModel):
    exchange_id: str | None = Field(default=None, max_length=80)
    password: str = Field(min_length=1, max_length=1024)


def require_password(user: User, password: str | None) -> None:
    if not password or not auth.verify_password(user, password):
        raise HTTPException(status_code=403, detail="Password confirmation failed")


def trading_error(exc: Exception) -> HTTPException:
    if isinstance(exc, TradingError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=502, detail=f"Exchange error: {exc}")


@app.get("/api/app/info")
async def app_info() -> dict[str, str]:
    from . import __version__

    return {"boot_id": BOOT_ID, "version": __version__}


@app.get("/api/live/status")
async def live_status(user: User = Depends(current_user)) -> dict:
    return live.status(user.id)


@app.put("/api/live/limits")
async def live_limits(payload: dict, user: User = Depends(current_user)) -> dict:
    try:
        return live.update_limits(user.id, payload)
    except TradingError as exc:
        raise trading_error(exc) from exc


@app.post("/api/live/enable")
async def live_enable(payload: LiveEnableRequest, user: User = Depends(current_user)) -> dict:
    require_password(user, payload.password)
    try:
        return await live.enable_live(
            user.id,
            payload.ip_address,
            payload.trade_permission_confirmed,
            payload.withdrawals_disabled_confirmed,
            payload.ip_allowlisted_confirmed,
            payload.risk_acknowledged,
            payload.typed_phrase,
        )
    except (TradingError, ExchangeError) as exc:
        raise trading_error(exc) from exc


@app.post("/api/live/disable")
async def live_disable(user: User = Depends(current_user)) -> dict:
    return live.disable_live(user.id)


@app.post("/api/live/ip")
async def live_ip(payload: LiveIpRequest, user: User = Depends(current_user)) -> dict:
    try:
        return live.confirm_ip(user.id, payload.ip_address)
    except TradingError as exc:
        raise trading_error(exc) from exc


@app.post("/api/live/kill-switch")
async def live_kill_switch(payload: KillSwitchRequest, user: User = Depends(current_user)) -> dict:
    if not payload.engaged:
        require_password(user, payload.password)
    return live.set_kill_switch(user.id, payload.engaged)


@app.post("/api/live/emergency-cancel")
async def live_emergency_cancel(payload: ModeRequest, user: User = Depends(current_user)) -> dict:
    try:
        return await live.emergency_cancel(user.id, payload.mode)
    except (TradingError, ExchangeError) as exc:
        raise trading_error(exc) from exc


@app.post("/api/live/orders/preview")
async def live_order_preview(payload: OrderPreviewRequest, user: User = Depends(current_user)) -> dict:
    try:
        return await live.preview_order(
            user.id, payload.mode, payload.symbol, payload.side, payload.price, payload.quantity
        )
    except (TradingError, ExchangeError) as exc:
        raise trading_error(exc) from exc


@app.post("/api/live/orders/confirm")
async def live_order_confirm(payload: OrderConfirmRequest, user: User = Depends(current_user)) -> dict:
    preview = live.store.get_preview(user.id, payload.preview_id)
    if preview is not None and preview["mode"] == "live":
        require_password(user, payload.password)
    try:
        return {"order": await live.confirm_order(user.id, payload.preview_id)}
    except (TradingError, ExchangeError) as exc:
        raise trading_error(exc) from exc


@app.get("/api/live/orders")
async def live_orders(
    mode: Literal["dry_run", "live"] | None = None, user: User = Depends(current_user)
) -> dict:
    orders = live.store.list_intents(user.id, mode, limit=200)
    filled_at = live.store.fill_times(user.id, orders)
    for order in orders:
        order["filled_at"] = filled_at.get(int(order["id"]))
    return {"orders": orders}


@app.get("/api/live/orders/{intent_id}/events")
async def live_order_events(intent_id: int, user: User = Depends(current_user)) -> dict:
    if live.store.get_intent(user.id, intent_id) is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return {"events": live.store.intent_events(user.id, intent_id)}


@app.post("/api/live/orders/{intent_id}/cancel")
async def live_order_cancel(intent_id: int, user: User = Depends(current_user)) -> dict:
    try:
        return {"order": await live.cancel_intent(user.id, intent_id)}
    except (TradingError, ExchangeError) as exc:
        raise trading_error(exc) from exc


@app.post("/api/live/orders/{intent_id}/reconcile")
async def live_order_reconcile(intent_id: int, user: User = Depends(current_user)) -> dict:
    intent = live.store.get_intent(user.id, intent_id)
    if intent is None:
        raise HTTPException(status_code=404, detail="Order not found")
    try:
        return {"order": await live.refresh_intent(intent)}
    except (TradingError, ExchangeError) as exc:
        raise trading_error(exc) from exc


@app.post("/api/live/orders/{intent_id}/resolve")
async def live_order_resolve(
    intent_id: int, payload: ResolveRequest, user: User = Depends(current_user)
) -> dict:
    require_password(user, payload.password)
    try:
        return {"order": await live.resolve_review(user.id, intent_id, payload.exchange_id)}
    except (TradingError, ExchangeError) as exc:
        raise trading_error(exc) from exc


class BotStopRequest(BaseModel):
    liquidate: bool = False


@app.post("/api/live/bots/preview")
async def live_bot_preview(payload: dict, user: User = Depends(current_user)) -> dict:
    try:
        return await grid_engine.plan(user.id, payload)
    except (TradingError, ExchangeError) as exc:
        raise trading_error(exc) from exc


@app.post("/api/live/bots", status_code=201)
async def live_bot_create(payload: dict, user: User = Depends(current_user)) -> dict:
    try:
        return {"bot": await grid_engine.create(user.id, payload)}
    except (TradingError, ExchangeError) as exc:
        raise trading_error(exc) from exc


@app.get("/api/live/bots")
async def live_bot_list(user: User = Depends(current_user)) -> dict:
    return {"bots": grid_engine.summaries(user.id)}


@app.post("/api/live/bots/suggest")
async def live_bot_suggest(payload: dict, user: User = Depends(current_user)) -> dict:
    symbol = str(payload.get("symbol", "")).strip().upper()
    mode = str(payload.get("mode", "dry_run"))
    try:
        candles = await service.market_candles(symbol, 60, limit=100, max_age_ms=3_600_000)
    except NonKycError:
        candles = []
    ranges = [(Decimal(str(c.high)), Decimal(str(c.low))) for c in candles]
    try:
        return await grid_engine.suggest(user.id, mode, symbol, ranges)
    except (TradingError, ExchangeError) as exc:
        raise trading_error(exc) from exc


@app.get("/api/live/bots/{bot_id}/activity")
async def live_bot_activity(bot_id: int, user: User = Depends(current_user)) -> dict:
    try:
        activity = grid_engine.activity(user.id, bot_id)
    except TradingError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    bot = activity["bot"]
    try:
        rules = await live.connector(user.id, bot["mode"]).market_rules(bot["symbol"])
        activity["last_price"] = str(rules.last_price)
    except (TradingError, ExchangeError):
        activity["last_price"] = None
    return activity


@app.post("/api/live/bots/{bot_id}/start")
async def live_bot_start(
    bot_id: int, payload: PasswordRequest | None = None, user: User = Depends(current_user)
) -> dict:
    bot = live.store.get_bot(user.id, bot_id)
    if bot is not None and bot["mode"] == "live":
        require_password(user, payload.password if payload else None)
    try:
        return {"bot": grid_engine.start(user.id, bot_id)}
    except TradingError as exc:
        raise trading_error(exc) from exc


@app.post("/api/live/bots/{bot_id}/pause")
async def live_bot_pause(bot_id: int, user: User = Depends(current_user)) -> dict:
    try:
        return {"bot": grid_engine.pause(user.id, bot_id)}
    except TradingError as exc:
        raise trading_error(exc) from exc


@app.post("/api/live/bots/{bot_id}/stop")
async def live_bot_stop(
    bot_id: int, payload: BotStopRequest, user: User = Depends(current_user)
) -> dict:
    try:
        return {"bot": grid_engine.stop(user.id, bot_id, payload.liquidate)}
    except TradingError as exc:
        raise trading_error(exc) from exc


@app.delete("/api/live/bots/{bot_id}")
async def live_bot_delete(bot_id: int, user: User = Depends(current_user)) -> dict:
    try:
        grid_engine.delete(user.id, bot_id)
    except TradingError as exc:
        raise trading_error(exc) from exc
    return {"status": "deleted"}


def run() -> None:
    uvicorn.run("bitsbots.main:app", host="127.0.0.1", port=8765, reload=False)


if __name__ == "__main__":
    run()
