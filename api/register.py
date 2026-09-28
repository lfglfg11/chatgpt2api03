from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from api.support import require_admin
from services.register import mail_domain_stats
from services.register_service import register_service


class RegisterConfigRequest(BaseModel):
    mail: dict | None = None
    proxy: str | None = None
    total: int | None = None
    threads: int | None = None
    mode: str | None = None
    target_quota: int | None = None
    target_available: int | None = None
    check_interval: int | None = None


class OutlookPoolResetRequest(BaseModel):
    scope: str | None = None


class GptMailStatusRequest(BaseModel):
    provider: dict | None = None
    force: bool | None = None


class MailDomainResetRequest(BaseModel):
    domain: str


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/register")
    async def get_register_config(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"register": register_service.get()}

    @router.post("/api/register")
    async def update_register_config(body: RegisterConfigRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"register": register_service.update(body.model_dump(exclude_none=True))}

    @router.post("/api/register/start")
    async def start_register(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"register": register_service.start()}

    @router.post("/api/register/stop")
    async def stop_register(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"register": register_service.stop()}

    @router.post("/api/register/reset")
    async def reset_register(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"register": register_service.reset()}

    @router.post("/api/register/outlook-pool/reset")
    async def reset_outlook_pool(body: OutlookPoolResetRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"register": register_service.reset_outlook_pool(body.scope or "all")}

    @router.post("/api/register/gptmail/status")
    async def get_gptmail_status(body: GptMailStatusRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return {"status": register_service.gptmail_status(body.provider, force=bool(body.force))}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/register/gptmail/refresh-key")
    async def refresh_gptmail_public_key(body: GptMailStatusRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return {"status": register_service.refresh_gptmail_public_key(body.provider, force=body.force is not False)}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/register/mail-domains")
    async def get_mail_domain_stats(
        status: str = Query(default="all", description="all|blocked|proven|dead"),
        keyword: str = Query(default=""),
        limit: int = Query(default=0, ge=0, le=5000),
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        snapshot = mail_domain_stats.stats_snapshot()
        scope = str(status or "all").strip().lower()
        needle = str(keyword or "").strip().lower()

        def match(item: dict) -> bool:
            if scope == "blocked" and not item.get("blocked"):
                return False
            if scope == "proven" and int(item.get("code_received_count") or 0) <= 0:
                return False
            if scope == "dead":
                if int(item.get("no_code_count") or 0) <= 0 or int(item.get("code_received_count") or 0) > 0:
                    return False
            return not needle or needle in str(item.get("domain") or "")

        items = [item for item in snapshot["items"] if match(item)]
        total = len(items)
        if limit:
            items = items[:limit]
        return {**snapshot, "items": items, "filtered_total": total, "filter": scope}

    @router.post("/api/register/mail-domains/reset")
    async def reset_mail_domain(body: MailDomainResetRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        result = mail_domain_stats.reset_domain(body.domain)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=str(result.get("error") or "域名解除失败"))
        return result

    @router.get("/api/register/events")
    async def register_events(token: str = ""):
        require_admin(f"Bearer {token}")

        async def stream():
            last = ""
            while True:
                payload = json.dumps(register_service.get(), ensure_ascii=False)
                if payload != last:
                    last = payload
                    yield f"data: {payload}\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return router
