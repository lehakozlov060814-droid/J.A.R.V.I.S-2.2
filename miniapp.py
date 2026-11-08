"""Same-process Mini App server. Bounded HTTP/1.1 behind a trusted HTTPS proxy.
SQLite and Telegram calls stay on the bot's event loop. No thread/second poller.
"""
import asyncio
import contextlib
import json
import logging
import os
import re
import time
import urllib.parse
from collections import defaultdict,deque
from pathlib import Path
import personal as p
LOG=logging.getLogger('nexora.web')
ROOT=Path(__file__).parent/'web'

class API:
    def __init__(self,app):self.app=app;self.db=app.db
    async def dispatch(self,uid,method,path,body):
        if not isinstance(body,dict):raise p.PersonalError('invalid','Неверные данные.')
        profile=p.profile(self.db,uid)
        if path=='/api/me' and method=='GET':
            models=[]
            try:
                catalog=await self.app.brain.catalog()
                catalog_ok=True
            except Exception:
                catalog={};catalog_ok=False
            for mid,info in p.MODELS.items():
                item=catalog.get(mid)
                models.append(dict(id=mid,**info,available=p.available(item),locked=info['tier']=='pro' and profile['effective_tier']!='pro'))
            return dict(version='1.1.0',profile=self.public_profile(profile),suggested_name=p.suggestion(self.db,uid),
                models=models,catalog_checked=catalog_ok,quotas={mid:p.quota(self.db,uid,mid) for mid in p.MODELS},
                notes=p.notes(self.db,uid),memory_enabled=not self.db.opted(uid),test_mode=bool(getattr(self.app,'preview',False)))
        if path=='/api/profile' and method=='POST':
            allowed={'name','model','cross_chat','preferences','onboarded','notice_seen'}
            if not body or set(body)-allowed:raise p.PersonalError('invalid','Эту настройку нельзя изменить.')
            changes={}
            if 'notice_seen' in body:
                if body['notice_seen'] is not True:raise p.PersonalError('invalid','Неверная настройка.')
                changes['notice']=''
            if 'name' in body:
                name=body['name']
                if not isinstance(name,str) or not 1<=len(name.strip())<=40 or re.search(r'[\x00-\x1f<>]',name):raise p.PersonalError('invalid','Имя: от 1 до 40 символов.')
                changes['name']=name.strip()
            if 'model' in body:
                mid=body['model']
                if not isinstance(mid,str) or mid not in p.MODELS:raise p.PersonalError('model','Такой модели нет.')
                if p.MODELS[mid]['tier']=='pro' and profile['effective_tier']!='pro':raise p.PersonalError('pro_required','Эта модель доступна в Pro.',403)
                try:catalog=await self.app.brain.catalog()
                except Exception:raise p.PersonalError('catalog','Не удалось проверить доступность модели.',503) from None
                if mid not in catalog or not p.available(catalog[mid]):raise p.PersonalError('model_unavailable','Сейчас эта модель недоступна.',409)
                changes['model']=mid
            for flag in ('cross_chat','onboarded'):
                if flag in body:
                    if type(body[flag]) is not bool:raise p.PersonalError('invalid','Неверная настройка.')
                    changes[flag]=int(body[flag])
            if 'preferences' in body:
                prefs=body['preferences']
                choices={'tone':{'formal','balanced','casual'},'length':{'short','adaptive','detailed'},'humor':{'reserved','classic','meme'},'profanity_level':{'off','moderate','context'}}
                if not isinstance(prefs,dict) or any(k not in choices or not isinstance(v,str) or v not in choices[k] for k,v in prefs.items()):raise p.PersonalError('invalid','Неверные предпочтения.')
                changes['preferences']=json.dumps(prefs,ensure_ascii=False)
            if changes.get('onboarded') and not changes.get('name',profile['name']):raise p.PersonalError('invalid','Укажи имя или ник.')
            changes['updated']=time.time()
            self.db.c.execute('UPDATE profiles SET '+','.join(k+'=?' for k in changes)+' WHERE user_id=?',(*changes.values(),uid))
            return self.public_profile(p.profile(self.db,uid))
        if path=='/api/preferences/reset' and method=='POST':
            self.confirm(body)
            self.db.c.execute("UPDATE profiles SET preferences='{}',cross_chat=0,updated=? WHERE user_id=?",(time.time(),uid))
            return {'ok':True}
        if path=='/api/memory' and method=='POST':
            if set(body)!={'value'} or not isinstance(body['value'],str) or not 12<=len(body['value'].strip())<=400:raise p.PersonalError('invalid','Запись: от 12 до 400 символов.')
            value=body['value'].strip()
            # Reuse the mature privacy checks; no automatic extraction from private chat.
            from main import SENSITIVE,INJECTION,MEMORY_PRIVATE
            if SENSITIVE.search(value) or INJECTION.search(value) or MEMORY_PRIVATE.search(value):raise p.PersonalError('private','Не сохраняй здесь секреты, контакты и чувствительные сведения.')
            if self.db.opted(uid):raise p.PersonalError('memory_off','Сохранение твоих данных выключено. Сначала разреши память.',409)
            with self.db.tx():
                normalized=' '.join(value.split())
                # Repeated saves are idempotent, including at the plan's capacity.
                existing=p.notes(self.db,uid)
                if any(' '.join(r['value'].split()).casefold()==normalized.casefold() for r in existing):
                    return {'notes':existing}
                count=self.db.one('SELECT count(*) n FROM personal_notes WHERE user_id=?',(uid,))['n']
                if count>=p.PLANS[profile['effective_tier']]['memory_cap']:raise p.PersonalError('memory_limit','Лимит личных воспоминаний занят.',409)
                self.db.c.execute('INSERT OR IGNORE INTO personal_notes(user_id,value,created) VALUES(?,?,?)',(uid,normalized,time.time()))
            return {'notes':p.notes(self.db,uid)}
        if path=='/api/memory/delete' and method=='POST':
            self.confirm(body)
            if type(body.get('id')) is not int:raise p.PersonalError('invalid','Запись не найдена.')
            self.db.c.execute('DELETE FROM personal_notes WHERE id=? AND user_id=?',(body['id'],uid))
            return {'ok':True}
        if path=='/api/data/delete' and method=='POST':
            self.confirm(body);p.erase_private(self.db,uid);return {'ok':True}
        if path=='/api/memory/permission' and method=='POST':
            if set(body)!={'enabled'} or type(body['enabled']) is not bool:raise p.PersonalError('invalid','Неверная настройка.')
            if body['enabled']:self.db.c.execute('DELETE FROM privacy WHERE user_id=?',(uid,))
            else:self.db.c.execute('INSERT OR REPLACE INTO privacy VALUES(?,1)',(uid,))
            return {'ok':True}
        if path=='/api/groups' and method=='GET':
            groups=[];failed=False
            for row in self.db.all('SELECT id,title,active FROM groups WHERE id<0 ORDER BY title LIMIT 100'):
                try:await self.app.admin(row['id'],uid)
                except Exception as exc:
                    if getattr(exc,'code','')!='admin_required':failed=True
                    continue
                groups.append(row)
            return {'groups':groups,'partial':failed}
        match=re.fullmatch(r'/api/groups/(-\d+)(?:/(memory))?',path)
        if match:
            cid=int(match[1]);row=self.db.one('SELECT * FROM groups WHERE id=?',(cid,))
            if not row:raise p.PersonalError('missing','Группа больше недоступна.',404)
            await self.app.admin(cid,uid)  # Fresh check on EVERY operation, not cached UI trust.
            _,cfg=self.db.group(cid)
            if match[2]=='memory':return await self.group_memory(uid,cid,method,body)
            keys={'initiative','archive','background','humor','tone','length','profanity_level'}
            if method=='POST':
                if set(body)-{'settings','search_mode','images_enabled','active'}:raise p.PersonalError('invalid','Недоступная настройка группы.')
                settings=body.get('settings',{})
                if not isinstance(settings,dict) or set(settings)-keys:raise p.PersonalError('invalid','Личные модели и тарифы нельзя менять за участника.')
                for k,v in settings.items():
                    if k in {'initiative','archive','background'}:
                        if type(v) is not bool:raise p.PersonalError('invalid','Неверное значение.')
                    elif not isinstance(v,str) or v not in {'humor':{'reserved','classic','meme'},'tone':{'formal','balanced','casual'},'length':{'short','adaptive','detailed'},'profanity_level':{'off','moderate','context'}}[k]:raise p.PersonalError('invalid','Неверное значение.')
                from main import image_policy,search_policy
                if 'search_mode' in body and body['search_mode'] not in ('auto','manual','off'):raise p.PersonalError('invalid','Неверный режим.')
                for k in ('images_enabled','active'):
                    if k in body and type(body[k]) is not bool:raise p.PersonalError('invalid','Неверное значение.')
                with self.db.tx():
                    self.db.group(cid,**settings)
                    if 'archive' in settings:
                        self.db.c.execute('UPDATE groups SET epoch=epoch+1 WHERE id=?',(cid,))
                        if not settings['archive']:self.db.c.execute("UPDATE messages SET queue_state='skip' WHERE chat_id=? AND queue_state IN ('pending','processing')",(cid,))
                    if 'search_mode' in body:
                        search_policy(self.db,cid);self.db.c.execute('UPDATE search_policy SET mode=? WHERE chat_id=?',(body['search_mode'],cid))
                    if 'images_enabled' in body:
                        image_policy(self.db,cid);self.db.c.execute('UPDATE image_policy SET enabled=? WHERE chat_id=?',(int(body['images_enabled']),cid))
                    if 'active' in body:self.db.c.execute('UPDATE groups SET active=? WHERE id=?',(int(body['active']),cid))
            elif method!='GET':raise p.PersonalError('method','Метод недоступен.',405)
            from main import image_policy,search_policy
            g,cfg=self.db.group(cid)
            return dict(id=cid,title=g['title'],active=bool(g['active']),settings={k:cfg.get(k) for k in keys},images_enabled=bool(image_policy(self.db,cid)['enabled']),search_mode=search_policy(self.db,cid)['mode'])
        raise p.PersonalError('missing','Страница не найдена.',404)

    async def group_memory(self,uid,cid,method,body):
        from main import owned_memory
        if method=='GET':
            rows=self.db.all("SELECT * FROM memories WHERE chat_id=? AND valid_until>? AND status IN ('pending','confirmed') ORDER BY created DESC LIMIT 80",(cid,time.time()))
            return {'memories':[dict(id=r['id'],title=r['title'] or r['value'][:60],value=r['value'],status=r['status'],date=r['event_date'],sources=[{'message_id':s['message_id']} for s in json.loads(r['sources'])]) for r in rows if not json.loads(r['subject_ids']) or json.loads(r['subject_ids'])==[uid]]}
        if method!='POST' or body.get('action') not in {'confirm','delete'} or type(body.get('id')) is not int:raise p.PersonalError('invalid','Неверное действие.')
        row=owned_memory(self.app,cid,uid,body['id'])
        self.confirm(body)
        with self.db.tx():
            if body['action']=='delete':self.db.c.execute('DELETE FROM memories WHERE chat_id=? AND id=?',(cid,row['id']))
            else:
                self.db.c.execute("UPDATE memories SET status='confirmed' WHERE chat_id=? AND id=?",(cid,row['id']))
                if row['supersedes']:self.db.c.execute("UPDATE memories SET status='superseded' WHERE chat_id=? AND id=? AND kind=? AND subject_ids=?",(cid,row['supersedes'],row['kind'],row['subject_ids']))
        return {'ok':True}
    @staticmethod
    def confirm(body):
        if body.get('confirm') is not True:raise p.PersonalError('confirmation','Нужно подтверждение действия.',409)
    @staticmethod
    def public_profile(row):
        return dict(name=row['name'],model=row['model'],tier=row['effective_tier'],pro_until=row['pro_until'] if row['effective_tier']=='pro' else 0,cross_chat=bool(row['cross_chat']),onboarded=bool(row['onboarded']),preferences=json.loads(row['preferences']),notice=row['notice'])

class Server:
    def __init__(self,app):
        self.app=app;self.api=API(app);self.server=None;self.connections=set();self.slots=asyncio.Semaphore(16);self.rates=defaultdict(deque)
    async def start(self,host='0.0.0.0',port=8000):
        if any(not (ROOT/name).is_file() for name in ('index.html','app.js','style.css')):raise RuntimeError('Mini App files missing: deploy web/index.html, web/app.js and web/style.css together')
        self.server=await asyncio.start_server(self.handle,host,port,limit=24576)
        LOG.info('Mini App HTTP listening; port=%s; HTTPS proxy required',self.server.sockets[0].getsockname()[1])
        return self
    async def close(self):
        if self.server:self.server.close();await self.server.wait_closed()
        for writer in list(self.connections):writer.close()
    async def handle(self,reader,writer):
        self.connections.add(writer)
        try:
            if len(self.connections)>32:await self.respond(writer,503,{'error':'busy','message':'Сервис занят.'});return
            async with self.slots:
                await asyncio.wait_for(self.request(reader,writer),20)
        except (TimeoutError,ValueError,asyncio.LimitOverrunError,asyncio.IncompleteReadError,UnicodeError):
            with contextlib.suppress(Exception):await self.respond(writer,400,{'error':'request','message':'Неверный запрос.'})
        except Exception as exc:
            LOG.warning('Mini App request failed: exception=%s',type(exc).__name__)
            with contextlib.suppress(Exception):await self.respond(writer,503,{'error':'unavailable','message':'Не удалось загрузить данные. Попробуй позже.'})
        finally:
            writer.close()
            with contextlib.suppress(Exception):await writer.wait_closed()
            self.connections.discard(writer)
    async def request(self,reader,writer):
        raw=await reader.readuntil(b'\r\n\r\n')
        if len(raw)>20000:raise ValueError
        lines=raw.decode('ascii').split('\r\n');method,target,version=lines[0].split(' ')
        if version!='HTTP/1.1' or method not in {'GET','POST'}:raise ValueError
        headers={}
        for line in lines[1:]:
            if not line:continue
            key,sep,value=line.partition(':');key=key.lower()
            if not sep or key in headers or not re.fullmatch('[a-z0-9-]+',key):raise ValueError
            headers[key]=value.strip()
        if 'transfer-encoding' in headers or not target.startswith('/') or target.startswith('//'):raise ValueError
        size=int(headers.get('content-length','0'))
        if size<0 or size>16000:raise ValueError
        path=urllib.parse.urlsplit(target).path
        if not path.startswith('/api/'):
            if method!='GET':raise ValueError
            files={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8')}
            if path=='/health':await self.respond(writer,200,{'status':'up','version':'1.1.0'});return
            if path not in files:await self.respond(writer,404,{'message':'Не найдено.'});return
            name,mime=files[path];await self.respond(writer,200,(ROOT/name).read_bytes(),mime);return
        peer=writer.get_extra_info('peername');ratekey=str(peer[0] if peer else 'local');now=time.time()
        # No trust in arbitrary X-Forwarded-For; a proxy IP shares a generous outer ceiling.
        if len(self.rates)>10000:self.rates.clear()
        q=self.rates[ratekey]
        while q and q[0]<now-60:q.popleft()
        if len(q)>=600:await self.respond(writer,429,{'message':'Слишком много обращений. Подожди минуту.'});return
        q.append(now)
        try:
            auth=headers.get('authorization','')
            if not auth.startswith('tma '):raise p.PersonalError('unauthorized','Открой панель из Telegram.',401)
            uid=p.verify_init_data(auth[4:],self.app.token)
            q=self.rates['user:'+str(uid)]
            while q and q[0]<now-60:q.popleft()
            if len(q)>=90:raise p.PersonalError('rate','Слишком много изменений. Подожди минуту.',429)
            q.append(now)
            origin=headers.get('origin')
            expected=os.getenv('MINIAPP_URL','').rstrip('/')
            if origin and (not expected or origin!=expected):raise p.PersonalError('origin','Открой панель из Telegram.',403)
            body={}
            if method=='POST':
                if headers.get('content-type','').split(';')[0]!='application/json':raise ValueError
                body=json.loads((await reader.readexactly(size)).decode('utf-8'))
            result=await self.api.dispatch(uid,method,path,body)
            await self.respond(writer,200,result)
        except p.PersonalError as exc:
            await self.respond(writer,exc.status,{'error':exc.code,'message':exc.message,'retry_at':exc.retry_at})
        except Exception as exc:
            # Fault metadata only: never raw external bodies, URLs, names or initData.
            LOG.warning('Mini App API failed: exception=%s code=%s',type(exc).__name__,str(getattr(exc,'code','unknown'))[:50])
            await self.respond(writer,403 if getattr(exc,'code','') in {'admin_required','personal_memory'} else 503,{'error':'unavailable','message':'Нет доступа или не удалось проверить настройки. Обнови страницу.'})
    @staticmethod
    async def respond(writer,status,data,mime='application/json; charset=utf-8'):
        body=data if isinstance(data,bytes) else json.dumps(data,ensure_ascii=False).encode()
        reason={200:'OK',400:'Bad Request',401:'Unauthorized',403:'Forbidden',404:'Not Found',405:'Method Not Allowed',409:'Conflict',429:'Too Many Requests',503:'Unavailable'}.get(status,'Error')
        headers=(f'HTTP/1.1 {status} {reason}\r\nContent-Type: {mime}\r\nContent-Length: {len(body)}\r\nConnection: close\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\n'
            "Content-Security-Policy: default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; object-src 'none'; form-action 'none'\r\n\r\n")
        writer.write(headers.encode()+body);await writer.drain()
