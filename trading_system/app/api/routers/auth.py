from fastapi import APIRouter

from trading_system.app.api.routers.common import *

router = APIRouter()


@router.post("/auth/login")
def auth_login(request: AuthLoginRequest) -> dict:
    session, service = _runtime()
    try:
        service.bootstrap()
        result = AuthService(service.repository, service.settings).login(
            request.username, request.password
        )
        if not result.authenticated:
            raise HTTPException(status_code=401, detail=result.reason)
        return {
            "token": result.token,
            "username": result.username,
            "role": result.role,
            "expires_at": result.expires_at.isoformat() if result.expires_at else None,
            "reason": result.reason,
        }
    finally:
        session.close()


@router.post("/auth/refresh")
def auth_refresh(
    principal: AdminPrincipal = Depends(require_principal),
    authorization: str | None = Header(default=None),
) -> dict:
    token = authorization.removeprefix("Bearer ").strip() if authorization else ""
    session, service = _runtime()
    try:
        service.bootstrap()
        result = AuthService(service.repository, service.settings).refresh(token)
        if not result.authenticated or not result.token:
            raise HTTPException(status_code=401, detail=result.reason)
        return {
            "token": result.token,
            "username": result.username or principal.username,
            "role": result.role or principal.role,
            "expires_at": result.expires_at.isoformat() if result.expires_at else None,
            "reason": result.reason,
        }
    finally:
        session.close()


@router.post("/auth/logout")
def auth_logout(
    principal: AdminPrincipal = Depends(require_principal),
    authorization: str | None = Header(default=None),
) -> dict:
    token = authorization.removeprefix("Bearer ").strip() if authorization else ""
    session, service = _runtime()
    try:
        service.bootstrap()
        revoked = AuthService(service.repository, service.settings).logout(
            token, actor=principal.username
        )
        return {"revoked": revoked, "reason": "Logout processed."}
    finally:
        session.close()


@router.get("/auth/me")
def auth_me(principal: AdminPrincipal = Depends(require_principal)) -> dict:
    return {"username": principal.username, "role": principal.role}
