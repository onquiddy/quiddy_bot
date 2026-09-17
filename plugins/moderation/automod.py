from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import discord

log = logging.getLogger("quiddy.moderation.automod")

# Эти буквы дают почти бесплатный уверенный сигнал. Одного символа всё равно мало — ниже есть min_letters.
_UK_UNIQUE = set("іїєґІЇЄҐ")
_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁёІіЇїЄєҐґ']+", re.UNICODE)
_INVITE_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/[\w-]+", re.I)
_URL_RE = re.compile(r"https?://[^\s<>]+", re.I)


@dataclass(slots=True)
class Detection:
    category: str
    confidence: float
    reason: str
    source: str = "rules"


class OpenAILanguageJudge:
    """AI — только второй голос. При сетевой ошибке AutoMod не наказывает вслепую."""
    def __init__(self, http, cfg: dict[str, Any]) -> None:
        self.http = http
        self.cfg = cfg
        self.key = os.getenv("OPENAI_API_KEY", "").strip()
        self.model = str(cfg.get("model", "gpt-5.6-luna"))
        self.timeout = float(cfg.get("timeout_seconds", 4.0))
        self.sem = asyncio.Semaphore(max(1, int(cfg.get("max_concurrency", 4))))
        self._cache: dict[str, tuple[float, Detection | None]] = {}

    async def classify(self, text: str) -> Detection | None:
        if not self.cfg.get("enabled", False) or not self.key or not text.strip():
            return None
        key = text.casefold().strip()[:500]
        cached = self._cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        prompt = (
            "Определи язык сообщения Discord. Нас интересует только различение украинского и не-украинского. "
            "Суржик считай украинским только если украинская часть явно преобладает. Имена, ссылки, цитаты и "
            "1-2 неоднозначных слова не угадывай. Верни ТОЛЬКО JSON: "
            '{"language":"uk|ru|other|uncertain","confidence":0.0,"reason":"коротко"}.\n\n'
            f"Сообщение: {text[:1200]}"
        )
        payload = {"model": self.model, "input": prompt, "max_output_tokens": 120}
        try:
            async with self.sem:
                session = self.http.session
                if not session:
                    return None
                async with asyncio.timeout(self.timeout):
                    async with session.post(
                        "https://api.openai.com/v1/responses",
                        headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
                        json=payload,
                    ) as resp:
                        if resp.status >= 400:
                            log.warning("OpenAI AutoMod недоступен • HTTP %s", resp.status)
                            return None
                        data = await resp.json()
            raw = data.get("output_text")
            if not raw:
                parts=[]
                for item in data.get("output", []):
                    for c in item.get("content", []):
                        if c.get("type") in {"output_text","text"}: parts.append(c.get("text", ""))
                raw="".join(parts)
            raw=(raw or "").strip().removeprefix("```json").removesuffix("```").strip()
            obj=json.loads(raw)
            if obj.get("language") != "uk":
                result=None
            else:
                result=Detection("language.uk", float(obj.get("confidence",0)), str(obj.get("reason","Украинский язык")), "openai")
            self._cache[key]=(time.monotonic()+float(self.cfg.get("cache_seconds",300)), result)
            if len(self._cache)>1000:
                now=time.monotonic(); self._cache={k:v for k,v in self._cache.items() if v[0]>now}
            return result
        except (TimeoutError, json.JSONDecodeError, OSError):
            log.debug("AI language judge failed", exc_info=True)
            return None


class AutoModEngine:
    def __init__(self, plugin) -> None:
        self.p = plugin
        self.cfg = plugin.cfg.get("automod", {}) or {}
        self.recent: dict[tuple[int,int], deque[tuple[float,str]]] = defaultdict(lambda: deque(maxlen=20))
        self.violations: dict[tuple[int,int], deque[float]] = defaultdict(lambda: deque(maxlen=50))
        self.joins: dict[int, deque[float]] = defaultdict(lambda: deque(maxlen=100))
        self.raid_until: dict[int,float] = {}
        self.ai = OpenAILanguageJudge(plugin.ctx.services.get("http"), self.cfg.get("language_guard",{}).get("ai",{}))

    def reload(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg.get("automod", {}) or {}
        self.ai = OpenAILanguageJudge(self.p.ctx.services.get("http"), self.cfg.get("language_guard",{}).get("ai",{}))

    def exempt(self, message: discord.Message) -> bool:
        ex=self.cfg.get("exempt",{}) or {}
        if message.author.bot and ex.get("bots",True): return True
        if not isinstance(message.author,discord.Member): return True
        if ex.get("admins",True) and message.author.guild_permissions.administrator: return True
        if message.channel.id in {int(x) for x in ex.get("channel_ids",[]) or []}: return True
        roles={r.id for r in message.author.roles}
        return bool(roles & {int(x) for x in ex.get("role_ids",[]) or []})

    async def on_message(self, message: discord.Message) -> None:
        if not self.cfg.get("enabled",False) or not message.guild or self.exempt(message): return
        text=message.content or ""
        if not text: return
        detection = self._fast_rules(message,text)
        if detection is None:
            detection = await self._language(message,text)
        if detection:
            await self._act(message,detection)

    def _fast_rules(self, m: discord.Message, text: str) -> Detection | None:
        now=time.monotonic(); spam=self.cfg.get("spam",{}) or {}; key=(m.guild.id,m.author.id)
        q=self.recent[key]; q.append((now," ".join(text.casefold().split())))
        window=float(spam.get("window_seconds",5)); recent=[x for x in q if now-x[0]<=window]
        if spam.get("enabled",True) and len(recent)>=int(spam.get("messages",6)):
            return Detection("spam.rate",1.0,f"Слишком много сообщений за {window:g} сек.")
        dup_window=float(spam.get("duplicate_window_seconds",20)); norm=q[-1][1]
        dups=sum(1 for ts,t in q if now-ts<=dup_window and t==norm and len(norm)>=4)
        if spam.get("enabled",True) and dups>=int(spam.get("duplicate_messages",3)):
            return Detection("spam.duplicate",1.0,"Повтор одинаковых сообщений")
        mentions=self.cfg.get("mentions",{}) or {}
        if mentions.get("enabled",True) and len(m.mentions)>=int(mentions.get("max_users",5)):
            return Detection("spam.mentions",1.0,"Массовые упоминания")
        caps=self.cfg.get("caps",{}) or {}; letters=[c for c in text if c.isalpha()]
        if caps.get("enabled",True) and len(letters)>=int(caps.get("min_letters",20)):
            pct=sum(c.isupper() for c in letters)/len(letters)*100
            if pct>=float(caps.get("max_percent",80)): return Detection("text.caps",.99,"Слишком много CAPS")
        links=self.cfg.get("links",{}) or {}
        if links.get("block_invites",True) and _INVITE_RE.search(text): return Detection("links.invite",1.0,"Discord invite запрещён")
        blocked=[str(x).casefold() for x in (self.cfg.get("blocked_words",{}) or {}).get("words",[]) or []]
        low=text.casefold()
        if (self.cfg.get("blocked_words",{}) or {}).get("enabled",False) and any(w and w in low for w in blocked):
            return Detection("text.blocked",1.0,"Запрещённое выражение")
        return None

    async def _language(self, m: discord.Message, text: str) -> Detection | None:
        cfg=self.cfg.get("language_guard",{}) or {}
        if not cfg.get("enabled",False): return None
        # URL/emoji не должны внезапно превращаться в «украинский язык».
        clean=_URL_RE.sub(" ",text); letters=[c for c in clean if c.isalpha()]
        if len(letters)<int(cfg.get("min_letters",8)): return None
        unique=sum(c in _UK_UNIQUE for c in clean)
        if unique>=int(cfg.get("unique_letter_hits",2)):
            return Detection("language.uk",.995,"Украинская орфография", "rules")
        ai=await self.ai.classify(clean)
        if ai and ai.confidence>=float(cfg.get("ai",{}).get("confidence_threshold",.92)): return ai
        return None

    async def _act(self, m: discord.Message, d: Detection) -> None:
        policy=self.cfg.get("actions",{}) or {}; action=str(policy.get("default","delete_warn"))
        try:
            if action != "observe": await m.delete()
        except discord.HTTPException:
            log.warning("AutoMod не смог удалить сообщение • guild=%s user=%s",m.guild.id,m.author.id)
        key=(m.guild.id,m.author.id); now=time.monotonic(); v=self.violations[key]; v.append(now)
        decay=float(policy.get("violation_window_seconds",3600)); count=sum(1 for x in v if now-x<=decay)
        timeout_map=policy.get("timeouts",{}) or {}; minutes=int(timeout_map.get(str(count),timeout_map.get(count,0)) or 0)
        if action != "observe" and minutes and isinstance(m.author,discord.Member):
            try: await m.author.timeout(timedelta(minutes=min(minutes,40320)),reason=f"Quiddy AutoMod: {d.category}")
            except discord.HTTPException: pass
        await self._log(m,d,count,minutes,action)
        if action != "observe" and policy.get("notify_user",True):
            try:
                await m.channel.send(f"🛡️ {m.author.mention}, сообщение удалено: **{d.reason}**.",delete_after=8,allowed_mentions=discord.AllowedMentions(users=True))
            except discord.HTTPException: pass

    async def _log(self,m:discord.Message,d:Detection,count:int,minutes:int,action:str)->None:
        log.info("AutoMod • %s • %s • confidence=%.0f%% • source=%s • user=%s",d.category,d.reason,d.confidence*100,d.source,m.author.id)
        cid=self.cfg.get("log_channel_id") or self.p.cfg.get("mod_log_channel_id")
        ch=m.guild.get_channel(int(cid)) if cid else None
        if isinstance(ch,discord.TextChannel):
            e=discord.Embed(title="🛡️ Quiddy AutoMod",color=0xffb000)
            e.add_field(name="Участник",value=f"{m.author} (`{m.author.id}`)",inline=False)
            e.add_field(name="Причина",value=d.reason,inline=True);e.add_field(name="Детектор",value=f"{d.source} • {d.confidence:.0%}",inline=True)
            e.add_field(name="Нарушения за окно",value=str(count),inline=True)
            if minutes:e.add_field(name="Автотаймаут",value=f"{minutes} мин.",inline=True)
            try: await ch.send(embed=e,allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException: pass

    async def on_member_join(self, member: discord.Member) -> None:
        cfg=self.cfg.get("raid",{}) or {}
        if not cfg.get("enabled",True): return
        now=time.monotonic(); q=self.joins[member.guild.id]; q.append(now); window=float(cfg.get("window_seconds",15)); n=sum(1 for x in q if now-x<=window)
        if n>=int(cfg.get("joins",8)):
            self.raid_until[member.guild.id]=now+float(cfg.get("heightened_seconds",600))
            log.warning("AntiRaid активирован • guild=%s • joins=%s/%ss",member.guild.id,n,window)
