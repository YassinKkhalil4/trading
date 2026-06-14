from fastapi import FastAPI

from trading_system.app.api.routers import admin, auth, execution, market

app = FastAPI(title="Autonomous Trading Intelligence Platform", version="0.1.0")

app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(market.router)
app.include_router(execution.router)
