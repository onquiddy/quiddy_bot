from __future__ import annotations

import discord

from .api import QuiddyAPIClient


class CoreRepository:
    """Discord identity persistence through Quiddy Internal API only."""

    def __init__(self, api: QuiddyAPIClient) -> None:
        self.api = api

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def health(self) -> dict[str, str]:
        return {"status": "up"}

    async def upsert_user(self, user: discord.abc.User) -> int:
        avatar = getattr(user, "avatar", None)
        result = await self.api.post("/v1/core/users/upsert", json_data={
            "discord_id": user.id,
            "username": user.name,
            "global_name": getattr(user, "global_name", None),
            "avatar_hash": getattr(avatar, "key", None),
            "is_bot": bool(user.bot),
        })
        return int(result["id"])

    async def upsert_guild(self, guild: discord.Guild) -> int:
        result = await self.api.post("/v1/core/guilds/upsert", json_data={
            "discord_guild_id": guild.id,
            "name": guild.name,
            "owner_discord_id": guild.owner_id,
        })
        return int(result["id"])

    async def upsert_member(self, member: discord.Member) -> None:
        avatar = getattr(member, "avatar", None)
        await self.api.post("/v1/core/members/upsert", json_data={
            "guild": {
                "discord_guild_id": member.guild.id,
                "name": member.guild.name,
                "owner_discord_id": member.guild.owner_id,
            },
            "user": {
                "discord_id": member.id,
                "username": member.name,
                "global_name": getattr(member, "global_name", None),
                "avatar_hash": getattr(avatar, "key", None),
                "is_bot": bool(member.bot),
            },
            "nickname": member.nick,
            "joined_at": member.joined_at.isoformat() if member.joined_at else None,
        })

    async def mark_member_left(self, member: discord.Member) -> None:
        await self.api.post("/v1/core/members/left", json_data={
            "discord_guild_id": member.guild.id,
            "discord_user_id": member.id,
        })

    async def mark_guild_left(self, guild_id: int) -> None:
        await self.api.post("/v1/core/guilds/left", json_data={"discord_guild_id": guild_id})
