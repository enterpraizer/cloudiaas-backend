"""
Shared test fixtures for CloudIaaS integration tests.

Uses SQLite in-memory (aiosqlite + StaticPool) so no running PostgreSQL is needed.
Env vars are set at the very top — before any app module is imported — so that
`settings = Settings()` sees the correct values.
"""
# ── 1. Env vars first (must come before any app import) ───────────────────────
import os

_TEST_ENV = {
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "cloudiaas_test",
    "POSTGRES_USER": "postgres",
    "POSTGRES_PASSWORD": "postgres",
    "REDIS_HOST": "localhost",
    "REDIS_PORT": "6379",
    "REDIS_DB": "0",
    "EMAIL_HOST": "smtp.test.local",
    "EMAIL_PORT": "587",
    "EMAIL_USERNAME": "test@test.local",
    "EMAIL_PASSWORD": "testpass",
    "SECRET_KEY": "test-secret-key-for-testing-only-32chars!",
    "FRONTEND_URL": "http://localhost:3000",
    "SECRET_KEY": "test-access-secret-key-for-testing-1234",
    "REFRESH_SECRET_KEY": "test-refresh-secret-key-for-testing-12",
    "ALGORITHM": "HS256",
    "ACCESS_TOKEN_EXPIRE_MINUTES": "30",
    "REFRESH_TOKEN_EXPIRE_DAYS": "7",
    "DEBUG": "true",
}
for _k, _v in _TEST_ENV.items():
    os.environ.setdefault(_k, _v)

# ── 2. Regular imports ─────────────────────────────────────────────────────────
import uuid
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from passlib.context import CryptContext
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from src.infrastructure.models.base import Base
from src.infrastructure.models.users import User, Roles
from src.interfaces.api.app import app
from src.interfaces.api.dependencies.session import get_db

# ── 3. Constants ───────────────────────────────────────────────────────────────
# Use low bcrypt rounds to keep test fixtures fast
_bcrypt = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=4)
TEST_PASSWORD = "Test1234!"
# Pre-hash once at import time (not per-fixture)
TEST_PASSWORD_HASH = _bcrypt.hash(TEST_PASSWORD)

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

# Default VM payload used across tests
VM_PAYLOAD = {"name": "test-vm", "vcpu": 1, "ram_mb": 512, "disk_gb": 10}


# ── 4. Fixtures ────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db_engine():
    """Fresh in-memory SQLite per test function; all tables created from models."""
    engine = create_async_engine(
        TEST_DB_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def client(db_engine):
    """
    httpx AsyncClient pointing at the FastAPI app.
    - Overrides get_db to use the in-memory SQLite engine.
    - Patches send_confirmation_email.delay so registration never calls Celery.
    """
    _sm = async_sessionmaker(db_engine, expire_on_commit=False)

    async def _override_get_db():
        async with _sm() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = _override_get_db

    with patch("src.application.services.auth_service.send_confirmation_email") as mock_task:
        mock_task.delay = MagicMock()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac

    app.dependency_overrides.pop(get_db, None)


# ── 5. Helpers ─────────────────────────────────────────────────────────────────

async def _insert_user(
    db_engine, email: str, username: str, role: Roles = Roles.USER
) -> uuid.UUID:
    """Insert a pre-activated user directly into the test DB (bypasses email confirmation)."""
    user_id = uuid.uuid4()
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as session:
        await session.execute(
            insert(User).values(
                id=user_id,
                email=email,
                username=username,
                hashed_password=TEST_PASSWORD_HASH,
                is_active=True,
                is_verified=True,
                role=role,
            )
        )
        await session.commit()
    return user_id


async def _login(client: AsyncClient, username: str) -> dict:
    """Return raw token dict {access_token, refresh_token, token_type}."""
    resp = await client.post(
        "/auth/token",
        data={"username": username, "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200, f"Login failed for {username!r}: {resp.text}"
    return resp.json()


async def _make_tenant(client: AsyncClient, access_token: str, name: str) -> dict:
    """Create a tenant and return TenantTokenResponse dict (includes new JWT with tenant_id)."""
    resp = await client.post(
        "/auth/tenant",
        json={"name": name},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert resp.status_code == 201, f"Tenant creation failed for {name!r}: {resp.text}"
    return resp.json()


async def _create_vm(client: AsyncClient, headers: dict, payload: dict | None = None) -> dict:
    payload = payload or VM_PAYLOAD
    resp = await client.post("/vms", json=payload, headers=headers)
    assert resp.status_code == 201, f"VM creation failed: {resp.text}"
    return resp.json()


# ── 6. Tenant / admin client fixtures ─────────────────────────────────────────

@pytest_asyncio.fixture
async def tenant_a(client, db_engine):
    """Authenticated client context for Tenant Alpha.

    Returns dict: {headers, tenant_id, user_id}
    """
    user_id = await _insert_user(db_engine, "tenant_a@test.com", "tenant_a_user")
    tokens = await _login(client, "tenant_a_user")
    t_tokens = await _make_tenant(client, tokens["access_token"], "Tenant Alpha")
    return {
        "headers": {"Authorization": f"Bearer {t_tokens['access_token']}"},
        "tenant_id": t_tokens["tenant_id"],
        "user_id": str(user_id),
    }


@pytest_asyncio.fixture
async def tenant_b(client, db_engine):
    """Authenticated client context for Tenant Beta."""
    user_id = await _insert_user(db_engine, "tenant_b@test.com", "tenant_b_user")
    tokens = await _login(client, "tenant_b_user")
    t_tokens = await _make_tenant(client, tokens["access_token"], "Tenant Beta")
    return {
        "headers": {"Authorization": f"Bearer {t_tokens['access_token']}"},
        "tenant_id": t_tokens["tenant_id"],
        "user_id": str(user_id),
    }


@pytest_asyncio.fixture
async def admin(client, db_engine):
    """Admin user — no tenant context required for /admin/* routes."""
    await _insert_user(db_engine, "admin@test.com", "test_admin", role=Roles.ADMIN)
    tokens = await _login(client, "test_admin")
    return {"headers": {"Authorization": f"Bearer {tokens['access_token']}"}}
