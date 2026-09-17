from __future__ import annotations
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any
import discord
from discord import app_commands
from discord.ext import commands
from quiddy.core.audit import AuditRecord
from quiddy.core.plugin import BasePlugin, PluginContext
from .store import WarningStore
from .automod import AutoModEngine

log=logging.getLogger('quiddy.moderation')

def cut(text:str,n:int=900)->str: return text if len(text)<=n else text[:n-1]+'…'

class ModerationCog(commands.GroupCog, group_name='mod', group_description='Модерация сервера Quiddy'):
    def __init__(self,p:'ModerationPlugin')->None:self.p=p

    async def interaction_check(self,i:discord.Interaction)->bool:
        if not i.guild or not isinstance(i.user,discord.Member):
            await i.response.send_message('⛔ Команды модерации доступны только на сервере.',ephemeral=True);return False
        if not self.p.enabled:
            await i.response.send_message('⛔ Модуль модерации выключен.',ephemeral=True);return False
        return True

    @app_commands.command(name='ban',description='Забанить участника')
    @app_commands.checks.has_permissions(ban_members=True)
    async def ban(self,i:discord.Interaction,member:discord.Member,reason:str,delete_messages_hours:app_commands.Range[int,0,168]=0):
        if not await self.p.guard(i,member,'бан'):return
        await i.response.defer(ephemeral=True); await self.p.notify(member,'бан',reason)
        await member.ban(reason=self.p.audit_reason(i,reason),delete_message_seconds=int(delete_messages_hours)*3600)
        await self.p.finish(i,'ban',member,reason,f'🔨 {member.mention} забанен.')

    @app_commands.command(name='unban',description='Разбанить пользователя по Discord ID')
    @app_commands.checks.has_permissions(ban_members=True)
    async def unban(self,i:discord.Interaction,user_id:str,reason:str='Причина не указана'):
        try: uid=int(user_id); user=await i.client.fetch_user(uid)
        except (ValueError,discord.HTTPException): await i.response.send_message('❌ Не удалось найти пользователя по этому ID.',ephemeral=True);return
        await i.guild.unban(user,reason=self.p.audit_reason(i,reason)); await self.p.finish(i,'unban',user,reason,f'♻️ **{user}** разбанен.')

    @app_commands.command(name='kick',description='Кикнуть участника')
    @app_commands.checks.has_permissions(kick_members=True)
    async def kick(self,i:discord.Interaction,member:discord.Member,reason:str):
        if not await self.p.guard(i,member,'кик'):return
        await i.response.defer(ephemeral=True); await self.p.notify(member,'кик',reason); await member.kick(reason=self.p.audit_reason(i,reason))
        await self.p.finish(i,'kick',member,reason,f'👢 **{member}** исключён с сервера.')

    @app_commands.command(name='timeout',description='Выдать Discord-таймаут')
    @app_commands.checks.has_permissions(moderate_members=True)
    async def timeout(self,i:discord.Interaction,member:discord.Member,minutes:app_commands.Range[int,1,40320],reason:str='Причина не указана'):
        if not await self.p.guard(i,member,'таймаут'):return
        maximum=int(self.p.cfg.get('timeouts',{}).get('max_days',28))*1440; minutes=min(int(minutes),maximum)
        await i.response.defer(ephemeral=True); await self.p.notify(member,f'таймаут на {minutes} мин.',reason)
        await member.timeout(timedelta(minutes=minutes),reason=self.p.audit_reason(i,reason))
        await self.p.finish(i,'timeout',member,reason,f'⏳ {member.mention} получил таймаут на **{minutes} мин.**',{'minutes':minutes})

    @app_commands.command(name='untimeout',description='Снять Discord-таймаут')
    @app_commands.checks.has_permissions(moderate_members=True)
    async def untimeout(self,i:discord.Interaction,member:discord.Member,reason:str='Таймаут снят модератором'):
        if not await self.p.guard(i,member,'снятие таймаута'):return
        await member.timeout(None,reason=self.p.audit_reason(i,reason)); await self.p.finish(i,'untimeout',member,reason,f'✅ Таймаут {member.mention} снят.')

    @app_commands.command(name='warn',description='Выдать предупреждение участнику')
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn(self,i:discord.Interaction,member:discord.Member,reason:str):
        if not await self.p.guard(i,member,'предупреждение'):return
        item=await self.p.store.add(i.guild.id,member.id,i.user.id,reason); count=len(self.p.store.get(i.guild.id,member.id))
        if self.p.cfg.get('warnings',{}).get('dm_user',True): await self.p.notify(member,f'предупреждение #{item["id"]}',reason)
        await self.p.finish(i,'warn',member,reason,f'⚠️ {member.mention} получил предупреждение **#{item["id"]}** • всего: **{count}**',{'warning_id':item['id'],'count':count})
        await self.p.maybe_auto_timeout(member,count,reason,i.user)

    @app_commands.command(name='warnings',description='Показать предупреждения участника')
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warnings(self,i:discord.Interaction,member:discord.Member):
        rows=self.p.store.get(i.guild.id,member.id)
        if not rows: await i.response.send_message(f'✅ У {member.mention} нет предупреждений.',ephemeral=True);return
        text='\n'.join(f'`#{x["id"]}` <@{x["moderator_id"]}> • {cut(x["reason"],160)}' for x in rows[-20:])
        e=discord.Embed(title=f'⚠️ Предупреждения • {member}',description=text,color=0xffb000);e.set_footer(text=f'Всего: {len(rows)}')
        await i.response.send_message(embed=e,ephemeral=True)

    @app_commands.command(name='unwarn',description='Удалить одно предупреждение')
    @app_commands.checks.has_permissions(moderate_members=True)
    async def unwarn(self,i:discord.Interaction,member:discord.Member,warning_id:int):
        ok=await self.p.store.remove(i.guild.id,member.id,warning_id)
        await i.response.send_message(('✅ Предупреждение удалено.' if ok else '❌ Такого предупреждения нет.'),ephemeral=True)
        if ok: await self.p.audit(i,'unwarn',member,'warning removed',{'warning_id':warning_id})

    @app_commands.command(name='clearwarns',description='Очистить все предупреждения участника')
    @app_commands.checks.has_permissions(moderate_members=True)
    async def clearwarns(self,i:discord.Interaction,member:discord.Member):
        n=await self.p.store.clear(i.guild.id,member.id);await i.response.send_message(f'🧹 Удалено предупреждений: **{n}**.',ephemeral=True);await self.p.audit(i,'clearwarns',member,'warnings cleared',{'count':n})

    @app_commands.command(name='purge',description='Удалить последние сообщения в канале')
    @app_commands.checks.has_permissions(manage_messages=True)
    async def purge(self,i:discord.Interaction,amount:app_commands.Range[int,1,200]=25,member:discord.Member|None=None):
        limit=min(int(amount),int(self.p.cfg.get('purge',{}).get('max_messages',200)));await i.response.defer(ephemeral=True)
        check=(lambda m:m.author.id==member.id) if member else None
        deleted=await i.channel.purge(limit=limit,check=check,reason=self.p.audit_reason(i,'очистка сообщений'))
        await i.followup.send(f'🧹 Удалено сообщений: **{len(deleted)}**.',ephemeral=True);await self.p.audit(i,'purge',member,'message purge',{'count':len(deleted),'channel_id':i.channel_id})

    @app_commands.command(name='slowmode',description='Изменить slowmode канала')
    @app_commands.checks.has_permissions(manage_channels=True)
    async def slowmode(self,i:discord.Interaction,seconds:app_commands.Range[int,0,21600]):
        mx=int(self.p.cfg.get('channels',{}).get('slowmode_max_seconds',21600));seconds=min(int(seconds),mx)
        await i.channel.edit(slowmode_delay=seconds,reason=self.p.audit_reason(i,'slowmode'));await i.response.send_message(f'🐢 Slowmode: **{seconds} сек.**',ephemeral=True);await self.p.audit(i,'slowmode',None,'slowmode',{'seconds':seconds,'channel_id':i.channel_id})

    @app_commands.command(name='lock',description='Закрыть канал для @everyone')
    @app_commands.checks.has_permissions(manage_channels=True)
    async def lock(self,i:discord.Interaction,reason:str='Канал временно закрыт'):
        ow=i.channel.overwrites_for(i.guild.default_role);ow.send_messages=False;await i.channel.set_permissions(i.guild.default_role,overwrite=ow,reason=self.p.audit_reason(i,reason));await i.response.send_message('🔒 Канал закрыт.',ephemeral=True);await self.p.audit(i,'lock',None,reason,{'channel_id':i.channel_id})

    @app_commands.command(name='unlock',description='Открыть канал после /mod lock')
    @app_commands.checks.has_permissions(manage_channels=True)
    async def unlock(self,i:discord.Interaction,reason:str='Канал открыт'):
        ow=i.channel.overwrites_for(i.guild.default_role);ow.send_messages=None;await i.channel.set_permissions(i.guild.default_role,overwrite=ow,reason=self.p.audit_reason(i,reason));await i.response.send_message('🔓 Канал открыт.',ephemeral=True);await self.p.audit(i,'unlock',None,reason,{'channel_id':i.channel_id})

    @app_commands.command(name='nick',description='Изменить или сбросить ник участника')
    @app_commands.checks.has_permissions(manage_nicknames=True)
    async def nick(self,i:discord.Interaction,member:discord.Member,nickname:str|None=None):
        if not await self.p.guard(i,member,'смена ника'):return
        await member.edit(nick=(nickname.strip()[:32] if nickname else None),reason=self.p.audit_reason(i,'смена ника'));await self.p.finish(i,'nick',member,'nickname changed',f'✏️ Ник {member.mention} обновлён.')

    @app_commands.command(name='automod',description='Показать состояние AutoMod')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def automod(self,i:discord.Interaction):
        cfg=self.p.cfg.get('automod',{}) or {}; lg=cfg.get('language_guard',{}) or {}; ai=lg.get('ai',{}) or {}
        ai_ready=bool(ai.get('enabled',False) and __import__('os').getenv('OPENAI_API_KEY','').strip())
        e=discord.Embed(title='🛡️ Quiddy AutoMod',color=0xffb000)
        e.add_field(name='Состояние',value='🟢 включён' if cfg.get('enabled',False) else '🔴 выключен')
        e.add_field(name='Языковой фильтр',value='🟢 UK блокируется' if lg.get('enabled',False) else '⚪ выключен')
        e.add_field(name='AI',value=f"{'🟢' if ai_ready else '🟡'} {ai.get('model','gpt-5.6-luna')}")
        e.add_field(name='Режим',value=str((cfg.get('actions',{}) or {}).get('default','delete_warn')),inline=False)
        await i.response.send_message(embed=e,ephemeral=True)

    async def cog_app_command_error(self,i:discord.Interaction,error:app_commands.AppCommandError)->None:
        if isinstance(error,app_commands.MissingPermissions): msg='⛔ Недостаточно прав: '+', '.join(error.missing_permissions)
        elif isinstance(error,app_commands.BotMissingPermissions): msg='⛔ Quiddy не хватает прав: '+', '.join(error.missing_permissions)
        else: log.exception('Ошибка команды модерации',exc_info=(type(error),error,error.__traceback__));msg='❌ Не удалось выполнить действие. Ошибка записана в лог.'
        try:
            if i.response.is_done(): await i.followup.send(msg,ephemeral=True)
            else: await i.response.send_message(msg,ephemeral=True)
        except discord.HTTPException: pass

class ModerationPlugin(BasePlugin):
    def __init__(self,ctx:PluginContext)->None:
        super().__init__(ctx);self.cfg=ctx.config;self.enabled=bool(self.cfg.get('enabled',True));self.store=WarningStore(ctx.root/self.cfg.get('warnings',{}).get('storage','data/moderation_warnings.json'));self.automod=AutoModEngine(self)
    async def start(self)->None:
        await self.store.load();await self.add_cog(ModerationCog(self))
        self.ctx.bot.add_listener(self.automod.on_message,'on_message')
        self.ctx.bot.add_listener(self.automod.on_member_join,'on_member_join')
        log.info('Модуль модерации готов • AutoMod=%s • LanguageGuard=%s',bool(self.cfg.get('automod',{}).get('enabled',False)),bool(self.cfg.get('automod',{}).get('language_guard',{}).get('enabled',False)))
    async def stop(self)->None:
        self.ctx.bot.remove_listener(self.automod.on_message,'on_message')
        self.ctx.bot.remove_listener(self.automod.on_member_join,'on_member_join')
    async def guard(self,i:discord.Interaction,target:discord.Member,action:str)->bool:
        if self.cfg.get('safety',{}).get('prevent_self_actions',True) and target.id==i.user.id: await i.response.send_message(f'⛔ Нельзя применить «{action}» к самому себе.',ephemeral=True);return False
        if target.id==i.guild.owner_id and self.cfg.get('safety',{}).get('protect_owner',True): await i.response.send_message('⛔ Владелец сервера защищён.',ephemeral=True);return False
        me=i.guild.me
        if self.cfg.get('safety',{}).get('require_hierarchy',True):
            if i.user.id!=i.guild.owner_id and target.top_role>=i.user.top_role: await i.response.send_message('⛔ Роль участника не ниже твоей.',ephemeral=True);return False
            if me and target.top_role>=me.top_role: await i.response.send_message('⛔ Роль участника не ниже роли Quiddy.',ephemeral=True);return False
        return True
    def audit_reason(self,i:discord.Interaction,reason:str)->str:return cut(f'{reason} • модератор {i.user} ({i.user.id})',500)
    async def notify(self,member:discord.Member,action:str,reason:str)->None:
        if not self.cfg.get('safety',{}).get('dm_before_action',True):return
        try: await member.send(f'🛡️ **{member.guild.name}**\nК тебе применено действие: **{action}**\nПричина: {cut(reason,1200)}')
        except discord.HTTPException: pass
    async def audit(self,i:discord.Interaction,action:str,target:Any,reason:str,metadata:dict[str,Any]|None=None)->None:
        try: await self.ctx.services.get('audit').write(AuditRecord(action=f'moderation.{action}',guild_id=i.guild_id,actor_type='discord_user',actor_id=str(i.user.id),entity_type='user' if target else 'channel',entity_id=str(getattr(target,'id',i.channel_id)),metadata={'reason':reason,**(metadata or {})}))
        except Exception: log.exception('Не удалось записать действие модерации в аудит')
    async def finish(self,i:discord.Interaction,action:str,target:Any,reason:str,message:str,metadata:dict[str,Any]|None=None)->None:
        await self.audit(i,action,target,reason,metadata);await self.modlog(i,action,target,reason,metadata)
        if i.response.is_done(): await i.followup.send(message,ephemeral=True)
        else: await i.response.send_message(message,ephemeral=True)
    async def modlog(self,i:discord.Interaction,action:str,target:Any,reason:str,metadata:dict[str,Any]|None=None)->None:
        cid=self.cfg.get('mod_log_channel_id'); channel=i.guild.get_channel(int(cid)) if cid else None
        if not isinstance(channel,discord.TextChannel):return
        e=discord.Embed(title=f'🛡️ Модерация • {action}',color=0xffb000);e.add_field(name='Цель',value=f'{target} (`{getattr(target,"id","—")}`)',inline=False);e.add_field(name='Модератор',value=f'{i.user} (`{i.user.id}`)',inline=False);e.add_field(name='Причина',value=cut(reason,1000),inline=False)
        try: await channel.send(embed=e,allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException: log.warning('Не удалось отправить mod-log')
    async def maybe_auto_timeout(self,member:discord.Member,count:int,reason:str,moderator:discord.Member)->None:
        auto=self.cfg.get('warnings',{}).get('auto_actions',{});
        if not auto.get('enabled',False):return
        minutes=int((auto.get('timeout_minutes',{}) or {}).get(count,0) or 0)
        if minutes>0:
            try: await member.timeout(timedelta(minutes=min(minutes,40320)),reason=f'Автодействие Quiddy: {count} предупреждений');log.info('Автотаймаут • %s • %d мин. • предупреждений=%d',member,minutes,count)
            except discord.HTTPException: log.exception('Не удалось выдать автоматический таймаут')

async def setup(ctx:PluginContext)->BasePlugin:return ModerationPlugin(ctx)
