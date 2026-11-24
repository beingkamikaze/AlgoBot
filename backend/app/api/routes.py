from fastapi import APIRouter, Depends, Request

from app.auth import User, current_user
from app.config import Settings, get_settings
from app.core.calendar import market_status

router = APIRouter()


@router.get("/health")
def health(request: Request, settings: Settings = Depends(get_settings)) -> dict:
    m = market_status()
    loop = getattr(request.app.state, "paper_loop", None)
    return {
        "paper_loop": loop.last if loop else None,
        "status": "ok",
        "mode": settings.mode,
        "supabase_configured": settings.supabase_configured,
        "market": {
            "now_ist": m.now.isoformat(timespec="seconds"),
            "is_open": m.is_open,
            "phase": m.phase,
            "reason": m.reason,
            "next_open": m.next_open.isoformat(timespec="minutes"),
            "calendar_known": m.calendar_known,
        },
    }


@router.get("/me")
async def me(user: User = Depends(current_user), settings: Settings = Depends(get_settings)) -> dict:
    return {"id": user.id, "email": user.email, "mode": settings.mode,
            "paper_starting_capital": settings.paper_starting_capital}
