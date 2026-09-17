from __future__ import annotations
import asyncio, json, os, time
from pathlib import Path
from typing import Any

class WarningStore:
    def __init__(self, path: Path, executor=None) -> None:
        self.path = path
        self.executor = executor
        self._lock = asyncio.Lock()
        self.data: dict[str, list[dict[str, Any]]] = {}

    async def load(self) -> None:
        if not self.path.exists(): return
        loop=asyncio.get_running_loop()
        def read():
            try: return json.loads(self.path.read_text('utf-8'))
            except Exception: return {}
        self.data = await loop.run_in_executor(self.executor, read)

    async def _save(self) -> None:
        payload=json.dumps(self.data,ensure_ascii=False,separators=(',',':'))
        path=self.path
        def write():
            path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_suffix(path.suffix+'.tmp')
            with tmp.open('w',encoding='utf-8') as f: f.write(payload); f.flush(); os.fsync(f.fileno())
            os.replace(tmp,path)
        await asyncio.get_running_loop().run_in_executor(self.executor,write)

    async def add(self,guild_id:int,user_id:int,moderator_id:int,reason:str)->dict[str,Any]:
        async with self._lock:
            key=f'{guild_id}:{user_id}'; rows=self.data.setdefault(key,[])
            item={'id':(rows[-1]['id']+1 if rows else 1),'moderator_id':moderator_id,'reason':reason,'created_at':int(time.time())}
            rows.append(item); await self._save(); return item

    def get(self,guild_id:int,user_id:int)->list[dict[str,Any]]: return list(self.data.get(f'{guild_id}:{user_id}',[]))

    async def remove(self,guild_id:int,user_id:int,warn_id:int)->bool:
        async with self._lock:
            key=f'{guild_id}:{user_id}'; rows=self.data.get(key,[]); before=len(rows)
            self.data[key]=[x for x in rows if int(x.get('id',0))!=warn_id]
            if len(self.data[key])==before:return False
            await self._save(); return True

    async def clear(self,guild_id:int,user_id:int)->int:
        async with self._lock:
            key=f'{guild_id}:{user_id}'; n=len(self.data.get(key,[])); self.data.pop(key,None); await self._save(); return n
