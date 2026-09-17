from __future__ import annotations
import asyncio, io, json, logging, math, os, random, time, hashlib
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont, ImageOps
from quiddy.core.plugin import BasePlugin, PluginContext
log=logging.getLogger("quiddy.leveling")

def now(): return int(time.time())
def xp_for_level(level:int, base:int=110, exp:float=1.62)->int: return int(base*(level**exp)) if level>0 else 0
def level_from_xp(xp:int,base:int,exp:float)->int:
    return max(0,int((max(0,xp)/base)**(1/exp)))

def font(size:int,bold=False):
    candidates=["C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf","/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    for p in candidates:
        if os.path.exists(p): return ImageFont.truetype(p,size)
    return ImageFont.load_default()

class LevelStore:
    def __init__(self,path:Path): self.path=path; self.data={"users":{},"seasons":{}}; self.lock=asyncio.Lock()
    async def load(self):
        if self.path.exists():
            try: self.data=json.loads(await asyncio.to_thread(self.path.read_text,"utf-8"))
            except Exception: log.exception("Не смог прочитать базу уровней — начинаю с пустой, файл не трогаю")
    def user(self,gid:int,uid:int):
        g=self.data.setdefault("users",{}).setdefault(str(gid),{})
        return g.setdefault(str(uid),{"xp":0,"messages":0,"voice_minutes":0,"reactions":0,"streak":0,"last_active_day":"","daily":{},"title":"","badges":[],"created_at":now()})
    async def save(self):
        async with self.lock:
            self.path.parent.mkdir(parents=True,exist_ok=True); tmp=self.path.with_suffix('.tmp')
            payload=json.dumps(self.data,ensure_ascii=False,separators=(',',':'))
            await asyncio.to_thread(tmp.write_text,payload,"utf-8"); await asyncio.to_thread(os.replace,tmp,self.path)

class LeaderView(discord.ui.View):
    def __init__(self,p,guild,rows,page=0): super().__init__(timeout=90); self.p=p; self.guild=guild; self.rows=rows; self.page=page
    def embed(self):
        size=10; pages=max(1,math.ceil(len(self.rows)/size)); self.page=max(0,min(self.page,pages-1)); out=[]
        medals=['🥇','🥈','🥉']
        for idx,(uid,d) in enumerate(self.rows[self.page*size:(self.page+1)*size],self.page*size+1):
            m=self.guild.get_member(int(uid)); name=m.display_name if m else f'Участник {uid}'; out.append(f"{medals[idx-1] if idx<=3 else f'`#{idx}`'} **{name}** — уровень **{d['level']}** · `{d['xp']:,} XP`")
        e=discord.Embed(title='🏆 Лидерборд активности',description='\n'.join(out) or 'Пока здесь тихо. Самое время занять первое место.',color=0xffa31a); e.set_footer(text=f'Страница {self.page+1}/{pages} · Quiddy Levels'); return e
    @discord.ui.button(label='Назад',emoji='◀️',style=discord.ButtonStyle.secondary)
    async def prev(self,i,b): self.page-=1; await i.response.edit_message(embed=self.embed(),view=self)
    @discord.ui.button(label='Вперёд',emoji='▶️',style=discord.ButtonStyle.secondary)
    async def nxt(self,i,b): self.page+=1; await i.response.edit_message(embed=self.embed(),view=self)

class LevelCog(commands.GroupCog,group_name='level',group_description='Уровни, профиль и рейтинг активности'):
    def __init__(self,p): self.p=p
    @app_commands.command(name='profile',description='Открыть красивый профиль активности')
    async def profile(self,i:discord.Interaction,member:discord.Member|None=None):
        member=member or i.user; await i.response.defer(); f=await self.p.profile_card(member); await i.followup.send(file=f)
    @app_commands.command(name='rank',description='Показать место в рейтинге и прогресс уровня')
    async def rank(self,i:discord.Interaction,member:discord.Member|None=None):
        member=member or i.user; d=self.p.stats(i.guild.id,member.id); rank=self.p.rank(i.guild.id,member.id); e=discord.Embed(title=f'⚡ {member.display_name} · уровень {d["level"]}',description=f'**{d["xp"]:,} XP** · место **#{rank}**\nДо следующего уровня: **{d["remaining"]:,} XP**\nСерия активности: **{d["streak"]} дн.**',color=0xffa31a); e.set_thumbnail(url=member.display_avatar.url); await i.response.send_message(embed=e)
    @app_commands.command(name='top',description='Топ самых активных участников сервера')
    async def top(self,i:discord.Interaction):
        rows=self.p.rows(i.guild.id); v=LeaderView(self.p,i.guild,rows); await i.response.send_message(embed=v.embed(),view=v)
    @app_commands.command(name='title',description='Изменить подпись в своём профиле')
    async def title(self,i:discord.Interaction,text:app_commands.Range[str,1,48]):
        d=self.p.store.user(i.guild.id,i.user.id); d['title']=str(text).strip(); await self.p.store.save(); await i.response.send_message('✨ Подпись профиля обновлена. Теперь он чуть больше твой.',ephemeral=True)

class LevelingPlugin(BasePlugin):
    def __init__(self,ctx:PluginContext): super().__init__(ctx); self.store=LevelStore(ctx.root/str(ctx.config.get('storage','data/leveling.json'))); self.cooldowns={}; self.recent=defaultdict(lambda:deque(maxlen=4)); self.voice={}; self.dirty=False
    async def start(self):
        await self.store.load(); await self.add_cog(LevelCog(self)); self.ctx.bot.add_listener(self.on_message,'on_message'); self.ctx.bot.add_listener(self.on_voice_state_update,'on_voice_state_update'); self.ctx.bot.add_listener(self.on_reaction_add,'on_reaction_add'); self.tasks.interval(self.flush,20,name='levels-flush'); self.tasks.interval(self.voice_tick,60,name='levels-voice'); self.add_console_command('levels',self.console,'Статистика системы уровней',usage='levels [status]')
    async def stop(self):
        for f,n in [(self.on_message,'on_message'),(self.on_voice_state_update,'on_voice_state_update'),(self.on_reaction_add,'on_reaction_add')]: self.ctx.bot.remove_listener(f,n)
        await self.flush(force=True)
    async def console(self,args): log.info('Levels: guilds=%d users=%d',len(self.store.data.get('users',{})),sum(len(x) for x in self.store.data.get('users',{}).values()))
    def stats(self,gid,uid):
        d=self.store.user(gid,uid); base=int(self.ctx.config.get('level_curve',{}).get('base',110)); exp=float(self.ctx.config.get('level_curve',{}).get('exponent',1.62)); lv=level_from_xp(int(d['xp']),base,exp); nxt=xp_for_level(lv+1,base,exp); return {**d,'level':lv,'remaining':max(0,nxt-int(d['xp'])),'next_xp':nxt}
    def rows(self,gid):
        out=[]
        for uid,d in self.store.data.get('users',{}).get(str(gid),{}).items(): out.append((uid,self.stats(gid,int(uid))))
        return sorted(out,key=lambda x:x[1]['xp'],reverse=True)
    def rank(self,gid,uid): return next((n for n,(u,_) in enumerate(self.rows(gid),1) if int(u)==int(uid)),len(self.rows(gid))+1)
    async def add_xp(self,member,amount,source):
        if amount<=0:return
        d=self.store.user(member.guild.id,member.id); before=self.stats(member.guild.id,member.id)['level']; d['xp']+=int(amount); d[source]=int(d.get(source,0))+1; day=datetime.now(timezone.utc).date().isoformat()
        if d.get('last_active_day')!=day:
            try:
                old=datetime.fromisoformat(d.get('last_active_day')).date() if d.get('last_active_day') else None; delta=(datetime.now(timezone.utc).date()-old).days if old else 99
            except: delta=99
            d['streak']=int(d.get('streak',0))+1 if delta<=2 else 1; d['last_active_day']=day
        after=self.stats(member.guild.id,member.id)['level']; self.dirty=True
        if after>before: await self.level_up(member,after)
    async def level_up(self,m,level):
        rewards=self.ctx.config.get('rewards',{}); rid=(rewards.get('role_rewards',{}) or {}).get(str(level)); role=m.guild.get_role(int(rid)) if rid else None
        if role and m.guild.me and role<m.guild.me.top_role:
            try: await m.add_roles(role,reason=f'Quiddy Levels: уровень {level}')
            except discord.HTTPException: pass
        if rewards.get('announce',True):
            ch=m.guild.get_channel(int(rewards.get('announce_channel_id'))) if rewards.get('announce_channel_id') else None; ch=ch or m.guild.system_channel
            if ch:
                e=discord.Embed(title='⚡ Новый уровень!',description=f'{m.mention}, ты уже на **{level} уровне**. Не останавливайся — дальше награды интереснее.',color=0xffa31a); e.set_thumbnail(url=m.display_avatar.url)
                try: await ch.send(embed=e)
                except discord.HTTPException: pass
    async def on_message(self,msg):
        if not isinstance(msg.author,discord.Member) or msg.author.bot or not msg.guild or not self.ctx.config.get('enabled',True): return
        if msg.channel.id in set(map(int,self.ctx.config.get('excluded_channel_ids',[]) or [])) or any(r.id in set(map(int,self.ctx.config.get('excluded_role_ids',[]) or [])) for r in msg.author.roles): return
        text=(msg.content or '').strip(); mc=self.ctx.config.get('message_xp',{}); af=self.ctx.config.get('anti_farm',{})
        if len(text)<int(mc.get('min_chars',12)) or any(text.startswith(x) for x in af.get('ignore_prefixes',[])): return
        words={w.casefold() for w in text.split() if len(w)>1}
        if len(words)<int(af.get('min_unique_words',2)): return
        key=(msg.guild.id,msg.author.id); t=time.monotonic(); cd=float(mc.get('cooldown_seconds',45))
        if t-self.cooldowns.get(key,0)<cd:return
        h=hashlib.blake2s(' '.join(text.casefold().split()).encode(),digest_size=8).hexdigest()
        if h in self.recent[key]: return
        self.recent[key].append(h); self.cooldowns[key]=t; d=self.store.user(msg.guild.id,msg.author.id); day=datetime.now(timezone.utc).date().isoformat(); daily=d.setdefault('daily',{}); used=int(daily.get(day,0)); cap=int(mc.get('daily_soft_cap',450)); amount=random.randint(int(mc.get('min',8)),int(mc.get('max',14))); amount=max(1,int(amount*(1-min(0.65,used/max(1,cap)*0.65)))) if used>cap else amount; daily.clear(); daily[day]=used+amount; await self.add_xp(msg.author,amount,'messages')
    async def on_reaction_add(self,reaction,user):
        if user.bot or not reaction.message.guild or not self.ctx.config.get('reactions',{}).get('enabled',True): return
        author=reaction.message.author
        if isinstance(author,discord.Member) and not author.bot and author.id!=user.id: await self.add_xp(author,int(self.ctx.config.get('reactions',{}).get('received_xp',2)),'reactions')
    async def on_voice_state_update(self,m,before,after):
        self.voice[(m.guild.id,m.id)]=bool(after.channel)
    async def voice_tick(self):
        vc=self.ctx.config.get('voice_xp',{});
        if not vc.get('enabled',True):return
        for g in self.ctx.bot.guilds:
            for ch in g.voice_channels:
                humans=[m for m in ch.members if not m.bot]
                if len(humans)<int(vc.get('min_humans',2)) or (g.afk_channel and ch.id==g.afk_channel.id): continue
                for m in humans: await self.add_xp(m,int(vc.get('per_minute',2)),'voice_minutes')
    async def flush(self,force=False):
        if self.dirty or force: await self.store.save(); self.dirty=False
    async def profile_card(self,m):
        d=self.stats(m.guild.id,m.id); rank=self.rank(m.guild.id,m.id); http=self.ctx.services.get('http'); avatar=b''
        try:
            async with http.session.get(str(m.display_avatar.with_size(256).url)) as r: avatar=await r.read()
        except: pass
        def render():
            W,H=1000,340; im=Image.new('RGB',(W,H),(16,18,25)); dr=ImageDraw.Draw(im); dr.rounded_rectangle((24,24,W-24,H-24),28,fill=(27,30,40)); dr.rounded_rectangle((300,240,940,268),14,fill=(49,53,68)); base=int(self.ctx.config.get('level_curve',{}).get('base',110)); exp=float(self.ctx.config.get('level_curve',{}).get('exponent',1.62)); cur=xp_for_level(d['level'],base,exp); span=max(1,d['next_xp']-cur); prog=max(0,min(1,(d['xp']-cur)/span)); dr.rounded_rectangle((300,240,300+int(640*prog),268),14,fill=(255,163,26));
            if avatar:
                av=Image.open(io.BytesIO(avatar)).convert('RGB').resize((190,190)); mask=Image.new('L',(190,190)); ImageDraw.Draw(mask).ellipse((0,0,190,190),fill=255); im.paste(av,(70,70),mask)
            dr.text((300,70),m.display_name,font=font(42,True),fill='white'); dr.text((300,125),d.get('title') or 'Участник QuiddyNetwork',font=font(22),fill=(174,179,194)); dr.text((300,175),f'УРОВЕНЬ {d["level"]}   •   #{rank} В РЕЙТИНГЕ',font=font(24,True),fill=(255,177,55)); dr.text((300,280),f'{d["xp"]:,} XP   •   серия {d["streak"]} дн.   •   {d["messages"]} сообщений',font=font(19),fill=(205,209,220)); out=io.BytesIO(); im.save(out,'PNG',optimize=True); out.seek(0); return out
        out=await asyncio.to_thread(render); return discord.File(out,filename=f'quiddy-profile-{m.id}.png')

async def setup(ctx: PluginContext) -> BasePlugin:
    return LevelingPlugin(ctx)
