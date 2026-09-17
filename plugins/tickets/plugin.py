from __future__ import annotations
import asyncio,json,logging,os,re,time,html
from pathlib import Path
import discord
from discord import app_commands
from discord.ext import commands
from quiddy.core.plugin import BasePlugin,PluginContext
log=logging.getLogger('quiddy.tickets')
def slug(s): return re.sub(r'[^a-z0-9а-яё-]+','-',s.casefold()).strip('-')[:24] or 'user'
class Store:
 def __init__(self,p): self.p=p;self.d={'next':1,'tickets':{},'ratings':[]};self.lock=asyncio.Lock()
 async def load(self):
  if self.p.exists():
   try:self.d=json.loads(await asyncio.to_thread(self.p.read_text,'utf-8'))
   except:log.exception('Не смог прочитать состояние тикетов')
 async def save(self):
  async with self.lock:
   self.p.parent.mkdir(parents=True,exist_ok=True);tmp=self.p.with_suffix('.tmp');await asyncio.to_thread(tmp.write_text,json.dumps(self.d,ensure_ascii=False,indent=2),'utf-8');await asyncio.to_thread(os.replace,tmp,self.p)
class TicketModal(discord.ui.Modal,title='Новое обращение'):
 subject=discord.ui.TextInput(label='Коротко: что случилось?',placeholder='Например: не выдалась привилегия после оплаты',max_length=100)
 details=discord.ui.TextInput(label='Расскажи подробнее',style=discord.TextStyle.paragraph,placeholder='Что произошло, что уже пробовал, есть ли ошибка? Чем больше контекста — тем быстрее разберёмся.',max_length=1500)
 def __init__(self,p,kind='general'): super().__init__();self.p=p;self.kind=kind
 async def on_submit(self,i): await self.p.create_ticket(i,self.kind,str(self.subject),str(self.details))
class TypeSelect(discord.ui.Select):
 def __init__(self,p):
  self.p=p;opts=[]
  for k,v in (p.ctx.config.get('types',{}) or {}).items():opts.append(discord.SelectOption(label=str(v.get('label',k))[:100],value=k,description=str(v.get('description',''))[:100],emoji=v.get('emoji')))
  super().__init__(placeholder='Выбери тему обращения…',min_values=1,max_values=1,options=opts[:25],custom_id='quiddy:tickets:type')
 async def callback(self,i): await i.response.send_modal(TicketModal(self.p,self.values[0]))
class PanelView(discord.ui.View):
 def __init__(self,p):super().__init__(timeout=None);self.add_item(TypeSelect(p))
class TicketView(discord.ui.View):
 def __init__(self,p):super().__init__(timeout=None);self.p=p
 @discord.ui.button(label='Взять тикет',emoji='🙋',style=discord.ButtonStyle.primary,custom_id='quiddy:tickets:claim')
 async def claim(self,i,b):await self.p.claim(i)
 @discord.ui.button(label='Добавить участника',emoji='➕',style=discord.ButtonStyle.secondary,custom_id='quiddy:tickets:add')
 async def add(self,i,b):await i.response.send_message('Используй `/ticket add @участник` — так я точно не добавлю не того человека.',ephemeral=True)
 @discord.ui.button(label='Закрыть',emoji='🔒',style=discord.ButtonStyle.danger,custom_id='quiddy:tickets:close')
 async def close(self,i,b):await self.p.close(i,'Закрыто через кнопку')
class TicketCog(commands.GroupCog,group_name='ticket',group_description='Поддержка QuiddyNetwork'):
 def __init__(self,p):self.p=p
 @app_commands.command(name='panel',description='Опубликовать красивую панель открытия тикетов')
 @app_commands.checks.has_permissions(manage_guild=True)
 async def panel(self,i):
  c=self.p.ctx.config.get('panel',{});e=discord.Embed(title=c.get('title'),description=c.get('description'),color=int(c.get('color',16753920)));e.add_field(name='Как это работает',value='**1.** Выбираешь тему.\n**2.** Описываешь вопрос в форме.\n**3.** Получаешь приватный канал с поддержкой.',inline=False);e.set_footer(text='Quiddy Support • один вопрос — один тикет');await i.channel.send(embed=e,view=PanelView(self.p));await i.response.send_message('✨ Панель поддержки опубликована. Она переживёт перезапуск бота.',ephemeral=True)
 @app_commands.command(name='add',description='Добавить участника в текущий тикет')
 async def add(self,i,member:discord.Member):
  if not self.p.is_ticket(i.channel_id):return await i.response.send_message('Это не тикет.',ephemeral=True)
  await i.channel.set_permissions(member,view_channel=True,send_messages=True,read_message_history=True);await i.response.send_message(f'➕ {member.mention} добавлен в обращение.')
 @app_commands.command(name='remove',description='Убрать участника из текущего тикета')
 async def remove(self,i,member:discord.Member):
  if not self.p.is_staff(i.user):return await i.response.send_message('Эта команда только для команды поддержки.',ephemeral=True)
  await i.channel.set_permissions(member,overwrite=None);await i.response.send_message(f'➖ {member.mention} убран из обращения.')
 @app_commands.command(name='rename',description='Переименовать текущий тикет')
 async def rename(self,i,name:app_commands.Range[str,2,40]):
  if not self.p.is_staff(i.user):return await i.response.send_message('Эта команда только для команды поддержки.',ephemeral=True)
  await i.channel.edit(name='тикет-'+slug(str(name)));await i.response.send_message('✏️ Готово.',ephemeral=True)
 @app_commands.command(name='close',description='Закрыть тикет с причиной')
 async def close(self,i,reason:str='Вопрос решён'):await self.p.close(i,reason)
class TicketsPlugin(BasePlugin):
 def __init__(self,ctx):super().__init__(ctx);self.store=Store(ctx.root/str(ctx.config.get('storage','data/tickets.json')));self.cool={}
 async def start(self):await self.store.load();await self.add_cog(TicketCog(self));self.ctx.bot.add_view(PanelView(self));self.ctx.bot.add_view(TicketView(self));self.add_console_command('tickets',self.console,'Состояние поддержки',usage='tickets [status]')
 async def console(self,args):log.info('Tickets: open=%d total=%d',sum(1 for x in self.store.d['tickets'].values() if x.get('status')=='open'),len(self.store.d['tickets']))
 def is_ticket(self,cid):return str(cid) in self.store.d['tickets'] and self.store.d['tickets'][str(cid)].get('status')=='open'
 def is_staff(self,m):return isinstance(m,discord.Member) and (m.guild_permissions.manage_messages or any(r.id in set(map(int,self.ctx.config.get('staff_role_ids',[]) or [])) for r in m.roles))
 async def category(self,g):
  c=self.ctx.config.get('category',{});cat=g.get_channel(int(c.get('id'))) if c.get('id') else None
  if isinstance(cat,discord.CategoryChannel):return cat
  return await g.create_category(str(c.get('name','╭・ПОДДЕРЖКА')),reason='Quiddy Tickets setup')
 async def create_ticket(self,i,kind,subject,details):
  cfg=self.ctx.config;opened=[x for x in self.store.d['tickets'].values() if x.get('guild_id')==i.guild.id and x.get('owner_id')==i.user.id and x.get('status')=='open']
  if len(opened)>=int(cfg.get('limits',{}).get('per_user_open',2)):return await i.response.send_message('У тебя уже есть открытые обращения. Давай сначала закончим их 🙂',ephemeral=True)
  t=time.monotonic();key=(i.guild.id,i.user.id);cd=int(cfg.get('limits',{}).get('create_cooldown_seconds',30))
  if t-self.cool.get(key,0)<cd:return await i.response.send_message('Секундочку 🙂 Не нужно создавать несколько тикетов подряд.',ephemeral=True)
  self.cool[key]=t;await i.response.defer(ephemeral=True);cat=await self.category(i.guild);num=int(self.store.d.get('next',1));self.store.d['next']=num+1
  overw={i.guild.default_role:discord.PermissionOverwrite(view_channel=False),i.user:discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True,attach_files=True,embed_links=True)}
  if i.guild.me:overw[i.guild.me]=discord.PermissionOverwrite(view_channel=True,send_messages=True,manage_channels=True,manage_messages=True,read_message_history=True)
  for rid in cfg.get('staff_role_ids',[]) or []:
   if r:=i.guild.get_role(int(rid)):overw[r]=discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True,manage_messages=True)
  name=str(cfg.get('channel_format','тикет-{number}-{user}')).format(number=f'{num:04d}',user=slug(i.user.display_name));ch=await i.guild.create_text_channel(name[:100],category=cat,overwrites=overw,topic=f'Quiddy Ticket #{num} • owner={i.user.id} • type={kind}',reason='Quiddy Tickets: новое обращение')
  item={'number':num,'guild_id':i.guild.id,'owner_id':i.user.id,'type':kind,'subject':subject,'status':'open','created_at':int(time.time()),'claimed_by':None};self.store.d['tickets'][str(ch.id)]=item;await self.store.save();typ=(cfg.get('types',{}) or {}).get(kind,{})
  e=discord.Embed(title=f'{typ.get("emoji","🎫")} Обращение #{num:04d} · {typ.get("label","Поддержка")}',description=f'Привет, {i.user.mention}! Я уже создал приватное место для разговора. Команда увидит обращение и подключится сюда.\n\n**Тема**\n{discord.utils.escape_markdown(subject)}\n\n**Что произошло**\n{discord.utils.escape_markdown(details)}',color=0xffa31a);e.add_field(name='Что можно сделать сейчас',value='Прикрепи скриншоты, чеки или ошибки, если они помогут. Не отправляй пароли, токены и другие секреты.',inline=False);e.set_footer(text=f'Quiddy Support • тикет #{num:04d}')
  ping=' '.join(f'<@&{x}>' for x in cfg.get('staff_role_ids',[]) or []) if cfg.get('notifications',{}).get('ping_staff_on_open',True) else '';await ch.send(content=(ping or None),embed=e,view=TicketView(self),allowed_mentions=discord.AllowedMentions(roles=True,users=True,everyone=False));await i.followup.send(f'🎫 Готово — {ch.mention}. Я перенёс туда всё нужное.',ephemeral=True)
 async def claim(self,i):
  item=self.store.d['tickets'].get(str(i.channel_id));
  if not item:return await i.response.send_message('Это не активный тикет.',ephemeral=True)
  if not self.is_staff(i.user):return await i.response.send_message('Взять тикет может только команда поддержки.',ephemeral=True)
  item['claimed_by']=i.user.id;item['claimed_at']=int(time.time());await self.store.save();await i.response.send_message(f'🙋 {i.user.mention} взял обращение. Теперь понятно, кто ведёт вопрос.')
 async def transcript(self,ch,item):
  if not self.ctx.config.get('transcripts',{}).get('enabled',True):return None
  lim=int(self.ctx.config.get('transcripts',{}).get('max_messages',1000));lines=[]
  async for m in ch.history(limit=lim,oldest_first=True): lines.append(f'[{m.created_at.isoformat()}] {m.author} ({m.author.id}): {m.clean_content}\n'+''.join(f'  attachment: {a.url}\n' for a in m.attachments))
  p=self.ctx.root/str(self.ctx.config.get('transcripts',{}).get('directory','data/transcripts'))/f'ticket-{item["number"]:04d}-{ch.id}.txt';p.parent.mkdir(parents=True,exist_ok=True);await asyncio.to_thread(p.write_text,'\n'.join(lines),'utf-8');return p
 async def close(self,i,reason):
  item=self.store.d['tickets'].get(str(i.channel_id));
  if not item or item.get('status')!='open':return await i.response.send_message('Это не активный тикет.',ephemeral=True)
  if i.user.id!=item['owner_id'] and not self.is_staff(i.user):return await i.response.send_message('Закрыть обращение может его автор или команда поддержки.',ephemeral=True)
  await i.response.defer(ephemeral=True);item['status']='closed';item['closed_by']=i.user.id;item['closed_at']=int(time.time());item['close_reason']=reason;p=await self.transcript(i.channel,item);await self.store.save();owner=i.guild.get_member(int(item['owner_id']));
  if owner and self.ctx.config.get('notifications',{}).get('dm_on_close',True):
   try:await owner.send(f'✅ Твоё обращение #{item["number"]:04d} закрыто. Причина: {reason}\nСпасибо, что написал в поддержку QuiddyNetwork.')
   except:pass
  await i.followup.send('🔒 Закрываю обращение. Транскрипт уже сохранён.' if p else '🔒 Закрываю обращение.',ephemeral=True);await asyncio.sleep(max(0,min(10,int(self.ctx.config.get('close',{}).get('delay_seconds',3)))));await i.channel.delete(reason=f'Quiddy Tickets: {reason}'[:512])
async def setup(ctx: PluginContext) -> BasePlugin:
    return TicketsPlugin(ctx)
