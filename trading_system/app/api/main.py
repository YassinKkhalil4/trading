from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from trading_system.app.api.routers import admin, auth, execution, market
from trading_system.app.core.config import get_settings
from trading_system.app.alpha.ml_inference import load_alpha_model

settings = get_settings()

app = FastAPI(title="Autonomous Trading Intelligence Platform", version="0.1.0")


@app.on_event("startup")
def load_alpha_model_on_startup() -> None:
    load_alpha_model()
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_allowed_origins),
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(market.router)
app.include_router(execution.router)
