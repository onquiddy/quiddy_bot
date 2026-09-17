from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import asyncpg
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from .security import authenticate


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = await asyncpg.create_pool(
        host="postgres",
        port=5432,
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        database=os.environ["POSTGRES_DB"],
        min_size=1,
        max_size=5,
        command_timeout=10,
    )
    app.state.redis = Redis(
        host="redis",
        port=6379,
        password=os.environ["REDIS_PASSWORD"],
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    await app.state.redis.ping()
    yield
    await app.state.redis.aclose()
    await app.state.db.close()


app = FastAPI(
    title="Quiddy Internal API",
    version="1.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


class UserUpsert(BaseModel):
    discord_id: int
    username: str = Field(min_length=1, max_length=255)
    global_name: str | None = Field(default=None, max_length=255)
    avatar_hash: str | None = Field(default=None, max_length=255)
    is_bot: bool = False


class GuildUpsert(BaseModel):
    discord_guild_id: int
    name: str = Field(min_length=1, max_length=255)
    owner_discord_id: int | None = None


class MemberUpsert(BaseModel):
    guild: GuildUpsert
    user: UserUpsert
    nickname: str | None = Field(default=None, max_length=255)
    joined_at: datetime | None = None


class MemberLeft(BaseModel):
    discord_guild_id: int
    discord_user_id: int


class GuildLeft(BaseModel):
    discord_guild_id: int


class AuditItem(BaseModel):
    action: str = Field(min_length=1, max_length=255)
    source: str = Field(default="discord", max_length=64)
    guild_id: int | None = None
    actor_type: str | None = Field(default=None, max_length=64)
    actor_id: str | None = Field(default=None, max_length=255)
    entity_type: str | None = Field(default=None, max_length=64)
    entity_id: str | None = Field(default=None, max_length=255)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class AuditBatch(BaseModel):
    records: list[AuditItem] = Field(max_length=250)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "quiddy-api"}


@app.get("/v1/me")
async def me(client: str = Depends(authenticate)):
    return {"authenticated": True, "client": client}


@app.get("/v1/users/{discord_id}")
async def get_user(discord_id: int, client: str = Depends(authenticate)):
    async with app.state.db.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, discord_id, username, global_name, is_bot, created_at, updated_at
            FROM users WHERE discord_id = $1
            """,
            discord_id,
        )
    if row is None:
        raise HTTPException(404, "User not found")
    return dict(row)


async def _upsert_user(conn: asyncpg.Connection, user: UserUpsert) -> int:
    return int(await conn.fetchval(
        """
        INSERT INTO users (discord_id, username, global_name, avatar_hash, is_bot, created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
        ON CONFLICT (discord_id) DO UPDATE SET
            username = EXCLUDED.username,
            global_name = EXCLUDED.global_name,
            avatar_hash = EXCLUDED.avatar_hash,
            is_bot = EXCLUDED.is_bot,
            updated_at = NOW()
        RETURNING id
        """,
        user.discord_id, user.username, user.global_name, user.avatar_hash, user.is_bot,
    ))


async def _upsert_guild(conn: asyncpg.Connection, guild: GuildUpsert) -> int:
    return int(await conn.fetchval(
        """
        INSERT INTO guilds (discord_guild_id, name, owner_discord_id, joined_at, left_at, created_at, updated_at)
        VALUES ($1, $2, $3, NOW(), NULL, NOW(), NOW())
        ON CONFLICT (discord_guild_id) DO UPDATE SET
            name = EXCLUDED.name,
            owner_discord_id = EXCLUDED.owner_discord_id,
            left_at = NULL,
            updated_at = NOW()
        RETURNING id
        """,
        guild.discord_guild_id, guild.name, guild.owner_discord_id,
    ))


@app.post("/v1/core/users/upsert")
async def upsert_user(payload: UserUpsert, client: str = Depends(authenticate)):
    async with app.state.db.acquire() as conn:
        row_id = await _upsert_user(conn, payload)
    return {"id": row_id}


@app.post("/v1/core/guilds/upsert")
async def upsert_guild(payload: GuildUpsert, client: str = Depends(authenticate)):
    async with app.state.db.acquire() as conn:
        row_id = await _upsert_guild(conn, payload)
    return {"id": row_id}


@app.post("/v1/core/members/upsert")
async def upsert_member(payload: MemberUpsert, client: str = Depends(authenticate)):
    async with app.state.db.acquire() as conn:
        async with conn.transaction():
            user_id = await _upsert_user(conn, payload.user)
            guild_id = await _upsert_guild(conn, payload.guild)
            await conn.execute(
                """
                INSERT INTO guild_members
                    (guild_id, user_id, nickname, joined_at, left_at, is_staff, is_active, created_at, updated_at)
                VALUES ($1, $2, $3, $4, NULL, FALSE, TRUE, NOW(), NOW())
                ON CONFLICT (guild_id, user_id) DO UPDATE SET
                    nickname = EXCLUDED.nickname,
                    joined_at = COALESCE(guild_members.joined_at, EXCLUDED.joined_at),
                    left_at = NULL,
                    is_active = TRUE,
                    updated_at = NOW()
                """,
                guild_id, user_id, payload.nickname, payload.joined_at,
            )
    return {"ok": True}


@app.post("/v1/core/members/left")
async def member_left(payload: MemberLeft, client: str = Depends(authenticate)):
    async with app.state.db.acquire() as conn:
        await conn.execute(
            """
            UPDATE guild_members gm
            SET is_active = FALSE, left_at = NOW(), updated_at = NOW()
            FROM guilds g, users u
            WHERE gm.guild_id = g.id AND gm.user_id = u.id
              AND g.discord_guild_id = $1 AND u.discord_id = $2
            """,
            payload.discord_guild_id, payload.discord_user_id,
        )
    return {"ok": True}


@app.post("/v1/core/guilds/left")
async def guild_left(payload: GuildLeft, client: str = Depends(authenticate)):
    async with app.state.db.acquire() as conn:
        await conn.execute(
            "UPDATE guilds SET left_at = NOW(), updated_at = NOW() WHERE discord_guild_id = $1",
            payload.discord_guild_id,
        )
    return {"ok": True}


@app.post("/v1/audit/batch")
async def audit_batch(payload: AuditBatch, client: str = Depends(authenticate)):
    if not payload.records:
        return {"inserted": 0}
    rows = [(
        r.guild_id, r.source, r.actor_type, r.actor_id, r.action,
        r.entity_type, r.entity_id, r.metadata, r.created_at,
    ) for r in payload.records]
    async with app.state.db.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO audit_log
                (guild_id, source, actor_type, actor_id, action, entity_type, entity_id, metadata, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9)
            """,
            [row[:-2] + (__import__('json').dumps(row[-2], ensure_ascii=False), row[-1]) for row in rows],
        )
    return {"inserted": len(rows)}
