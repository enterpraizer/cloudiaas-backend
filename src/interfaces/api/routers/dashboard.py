import json
from datetime import datetime
from typing import Optional
from uuid import UUID

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, status

from src.application.services.auth_service import AuthService
from src.application.services.quota_service import QuotaService, usage_cache_key
from src.infrastructure.models.tenant import Tenant
from src.infrastructure.repositories.audit_log import AuditLogRepository
from src.infrastructure.repositories.network import NetworkRepository
from src.infrastructure.repositories.virtual_machine import VMRepository
from src.infrastructure.schemas.users import UserRequest
from src.interfaces.api.dependencies.tenant import get_current_tenant
from src.settings import settings

dashboard_router = APIRouter(prefix="/dashboard", tags=["Dashboard"])

_redis_client: Optional[aioredis.Redis] = None


def _get_redis() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(settings.redis.url, decode_responses=True)
    return _redis_client


@dashboard_router.get("/usage", status_code=status.HTTP_200_OK)
async def get_usage(
    current_user: UserRequest = Depends(AuthService.get_current_user),
    tenant: Tenant = Depends(get_current_tenant),
    quota_service: QuotaService = Depends(),
) -> dict:
    """Resource quota usage for the tenant. Cached in Redis for 30 seconds."""
    cache_key = usage_cache_key(tenant.id)

    try:
        redis = _get_redis()
        cached = await redis.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass  # Redis unavailable — fall through to live query

    summary = await quota_service.get_usage_summary(tenant.id)

    try:
        redis = _get_redis()
        await redis.setex(cache_key, 30, json.dumps(summary))
    except Exception:
        pass  # Cache write failure is non-fatal

    return summary


@dashboard_router.get("/vms/summary", status_code=status.HTTP_200_OK)
async def get_vm_summary(
    current_user: UserRequest = Depends(AuthService.get_current_user),
    tenant: Tenant = Depends(get_current_tenant),
    vm_repo: VMRepository = Depends(),
) -> dict:
    counts = await vm_repo.count_by_status(tenant.id)
    total = sum(counts.values())
    return {
        "total": total,
        "running": counts.get("running", 0),
        "stopped": counts.get("stopped", 0),
        "pending": counts.get("pending", 0),
        "terminated": counts.get("terminated", 0),
    }


@dashboard_router.get("/networks/summary", status_code=status.HTTP_200_OK)
async def get_network_summary(
    current_user: UserRequest = Depends(AuthService.get_current_user),
    tenant: Tenant = Depends(get_current_tenant),
    network_repo: NetworkRepository = Depends(),
) -> dict:
    from src.infrastructure.models.virtual_network import VirtualNetwork, NetworkStatus
    total = await network_repo.count(tenant_id=tenant.id)
    active = await network_repo.count(
        tenant.id,
        VirtualNetwork.status == NetworkStatus.ACTIVE,
    )
    return {"total": total, "active": active}


@dashboard_router.get("/activity", status_code=status.HTTP_200_OK)
async def get_activity(
    current_user: UserRequest = Depends(AuthService.get_current_user),
    tenant: Tenant = Depends(get_current_tenant),
    audit_repo: AuditLogRepository = Depends(),
) -> list[dict]:
    logs = await audit_repo.get_recent(tenant_id=tenant.id, limit=20)
    return [
        {
            "id": str(log.id),
            "action": log.action,
            "resource_type": log.resource_type,
            "resource_id": str(log.resource_id) if log.resource_id else None,
            "details": log.details,
            "created_at": log.created_at.isoformat(),
        }
        for log in logs
    ]
