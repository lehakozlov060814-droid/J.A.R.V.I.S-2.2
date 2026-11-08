"""Nexora personal identity and quota policy. Python 3.11, no network calls."""
import contextvars
import hashlib
import hmac
import json
import math
import re
import sqlite3
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass,field

FLASH = 'deepseek/deepseek-v4.1-flash'
PRO = 'deepseek/deepseek-v4-pro'
MODELS = {FLASH: {'name':'DeepSeek V4.1 Flash','description':'На каждый день. Общение и фотографии.','tier':'free'},
          PRO: {'name':'DeepSeek V4 Pro','description':'Для вдумчивых задач. Фотографии обрабатываются отдельным зрительным маршрутом.','tier':'pro'}}
# Sliding windows; independent model buckets. Absolute bot/group USD caps remain in main.py.
PLANS = {'free': {'quotas': {FLASH:(10,18000)}, 'daily_usd':.20, 'memory_cap':20},
         'pro': {'quotas': {FLASH:(40,18000), PRO:(25,3600)}, 'daily_usd':.50, 'memory_cap':100}}
SYSTEM_DAILY_USD=.02
REQUEST = contextvars.ContextVar('personal_request',default=None)

class PersonalError(Exception):
    def __init__(self, code, message, status=400, retry_at=0):
        self.code,self.message,self.status,self.retry_at=code,message,status,retry_at
        super().__init__(code)  # Never log names, initData, notes or prompts.

@dataclass
class Request:
    uid:int
    cid:int
    mid:int
    model:str=''
    reserved:bool=False
    success:bool=False
    uncertain:bool=False
    notice:str=''
    usage_ids:list=field(default_factory=list)

@contextmanager
def scope(uid,cid,mid):
    req=Request(uid,cid,mid)
    token=REQUEST.set(req)
    try:yield req
    finally:REQUEST.reset(token)

def migrate(db):
    if not db.one("SELECT value FROM single_meta WHERE key='personal1100'"):
        if db.one('SELECT id FROM groups LIMIT 1'):
            backup=db.path.with_name(db.path.name+'.pre-1.1.0.bak')
            if not backup.exists():
                with sqlite3.connect(backup) as target:db.c.backup(target)
        with db.tx():
            if 'user_id' not in {r['name'] for r in db.all('PRAGMA table_info(usage)')}:
                db.c.execute('ALTER TABLE usage ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0')
            db.c.execute("INSERT INTO single_meta VALUES('personal1100','1')")
    db.c.executescript('''
CREATE TABLE IF NOT EXISTS profiles(user_id INTEGER PRIMARY KEY,name TEXT NOT NULL DEFAULT '',model TEXT NOT NULL,tier TEXT NOT NULL DEFAULT 'free',pro_until REAL NOT NULL DEFAULT 0,notice TEXT NOT NULL DEFAULT '',preferences TEXT NOT NULL DEFAULT '{}',cross_chat INTEGER NOT NULL DEFAULT 0,onboarded INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS personal_jobs(user_id INTEGER,chat_id INTEGER,message_id INTEGER,model TEXT,created REAL,state TEXT,PRIMARY KEY(user_id,chat_id,message_id));
CREATE INDEX IF NOT EXISTS personal_window ON personal_jobs(user_id,model,created,state);
CREATE INDEX IF NOT EXISTS personal_spending ON usage(user_id,created);
CREATE TABLE IF NOT EXISTS personal_notes(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL,value TEXT NOT NULL,created REAL NOT NULL,UNIQUE(user_id,value));
CREATE TABLE IF NOT EXISTS pro_audit(id INTEGER PRIMARY KEY,actor INTEGER,user_id INTEGER,expires REAL,created REAL);
''')
    if 'notice' not in {r['name'] for r in db.all('PRAGMA table_info(profiles)')}:db.c.execute("ALTER TABLE profiles ADD COLUMN notice TEXT NOT NULL DEFAULT ''")
    # Crash outcomes remain held for their original window, never replayed.
    db.c.execute("UPDATE personal_jobs SET state='uncertain' WHERE state='pending'")

def profile(db,uid):
    if type(uid) is not int or not 0<uid<2**52:raise PersonalError('identity','Не удалось подтвердить пользователя.',401)
    now=time.time()
    db.c.execute('INSERT OR IGNORE INTO profiles(user_id,model,created,updated) VALUES(?,?,?,?)',(uid,FLASH,now,now))
    row=db.one('SELECT * FROM profiles WHERE user_id=?',(uid,))
    tier='pro' if row['tier']=='pro' and row['pro_until']>now else 'free'
    notice=row['notice']
    if row['model']==PRO and tier!='pro':
        notice='Доступ Pro завершился. Переключилась на DeepSeek V4.1 Flash.'
        db.c.execute('UPDATE profiles SET model=?,notice=?,updated=? WHERE user_id=?',(FLASH,notice,now,uid))
        row['model']=FLASH
    row['effective_tier']=tier;row['notice']=notice
    return row

def suggestion(db,uid):
    names=set()
    for r in db.all('SELECT preferences FROM participants WHERE user_id=?',(uid,)):
        try:
            name=json.loads(r['preferences']).get('nickname','')
            if isinstance(name,str) and 1<=len(name)<=40:names.add(name)
        except (ValueError,TypeError):pass
    return next(iter(names)) if len(names)==1 else ''

def quota(db,uid,model=None,now=None):
    p=profile(db,uid);mid=model or p['model'];now=time.time() if now is None else now
    rule=PLANS[p['effective_tier']]['quotas'].get(mid)
    if not rule:return {'limit':0,'remaining':0,'used':0,'held':0,'reset_at':0,'window_seconds':0}
    cap,window=rule
    rows=db.all("SELECT created,state FROM personal_jobs WHERE user_id=? AND model=? AND created>? AND state IN ('pending','success','uncertain') ORDER BY created",(uid,mid,now-window))
    used=sum(r['state']=='success' for r in rows);held=len(rows)-used
    # First slot recovery, including a plan downgrade with more than the new limit used.
    reset=rows[max(0,len(rows)-cap)]['created']+window if rows else 0
    return dict(limit=cap,remaining=max(0,cap-len(rows)),used=used,held=held,reset_at=reset,window_seconds=window)

def reserve(db):
    req=REQUEST.get()
    if req is None or req.reserved:return
    with db.tx():
        p=profile(db,req.uid);req.model=p['model'];req.notice=p['notice']
        if req.model not in MODELS:raise PersonalError('model_unavailable','Выбранная модель недоступна. Открой настройки Nexora.',409)
        if db.one('SELECT state FROM personal_jobs WHERE user_id=? AND chat_id=? AND message_id=?',(req.uid,req.cid,req.mid)):
            raise PersonalError('duplicate','Этот запрос уже обрабатывался.',409)
        q=quota(db,req.uid,req.model)
        if not q['remaining']:
            mins=max(1,math.ceil((q['reset_at']-time.time())/60));h,m=divmod(mins,60)
            wait=(f'{h} ч ' if h else '')+f'{m} мин'
            raise PersonalError('quota','Лимит твоих запросов сейчас занят. Место освободится через '+wait+'.',429,q['reset_at'])
        db.c.execute("INSERT INTO personal_jobs VALUES(?,?,?,?,?,'pending')",(req.uid,req.cid,req.mid,req.model,time.time()))
        req.reserved=True

def finish(db,req):
    if not req.reserved:return
    state='success' if req.success else 'uncertain' if req.uncertain else 'failed'
    db.c.execute('UPDATE personal_jobs SET state=? WHERE user_id=? AND chat_id=? AND message_id=? AND state=\'pending\'',(state,req.uid,req.cid,req.mid))

def check_money(db,reserve):
    req=REQUEST.get()
    if req is None:return
    p=profile(db,req.uid)
    now=time.time();day=now//86400*86400
    spent=db.one('SELECT coalesce(sum(cost),0) n FROM usage WHERE user_id=? AND created>=?',(req.uid,day))['n']
    if not math.isfinite(float(spent)) or spent<0 or spent+reserve>PLANS[p['effective_tier']]['daily_usd']+1e-12:
        raise PersonalError('user_budget','На сегодня твой денежный лимит Nexora занят. Он обновится в 00:00 UTC.',429,(day+86400))

def own_usage(db,rid):
    req=REQUEST.get()
    if req:
        db.c.execute('UPDATE usage SET user_id=? WHERE id=?',(req.uid,rid))
        req.usage_ids.append(rid)

def preferences(db,uid,cid):
    p=profile(db,uid)
    if cid!=uid and not p['cross_chat']:return {}
    prefs=json.loads(p['preferences']);out={k:v for k,v in prefs.items() if k in {'tone','length','humor','profanity_level'}}
    if p['name']:out['nickname']=p['name']
    return out

def notes(db,uid):return db.all('SELECT id,value,created FROM personal_notes WHERE user_id=? ORDER BY id DESC',(uid,))

def erase_private(db,uid):
    # Financial and replay protection survives deletion of conversational data.
    with db.tx():
        db.erase(uid)
        db.c.execute('DELETE FROM personal_notes WHERE user_id=?',(uid,))
        db.c.execute('DELETE FROM messages WHERE chat_id=?',(uid,))
        db.c.execute('DELETE FROM dialogues WHERE chat_id=?',(uid,))
        db.c.execute('DELETE FROM memories WHERE chat_id=?',(uid,))
        for table in ('nexora_images','nexora_edit_tasks','image_versions','search_cache','search_topics'):
            db.c.execute(f'DELETE FROM {table} WHERE user_id=?',(uid,))
        db.c.execute("UPDATE profiles SET name='',preferences='{}',cross_chat=0,onboarded=0,updated=? WHERE user_id=?",(time.time(),uid))

def grant_pro(db,actor,owner,uid,hours):
    if not owner or actor!=owner:raise PersonalError('forbidden','Нет доступа.',403)
    if type(hours) is not int or not 0<=hours<=168:raise PersonalError('invalid','Срок: от 0 до 168 часов.')
    profile(db,uid);expires=time.time()+hours*3600
    with db.tx():
        db.c.execute("UPDATE profiles SET tier=?,pro_until=?,updated=? WHERE user_id=?",('pro' if hours else 'free',expires,time.time(),uid))
        db.c.execute('INSERT INTO pro_audit(actor,user_id,expires,created) VALUES(?,?,?,?)',(actor,uid,expires,time.time()))

def verify_init_data(raw,token,now=None):
    try:
        if not token or not isinstance(raw,str) or len(raw)>12000:raise ValueError
        pairs=urllib.parse.parse_qsl(raw,keep_blank_values=True,strict_parsing=True,max_num_fields=40)
        if len(dict(pairs))!=len(pairs):raise ValueError
        data=dict(pairs);given=data.pop('hash');stamp=int(data['auth_date'])
        now=time.time() if now is None else now
        if not now-3600<=stamp<=now+30 or not re.fullmatch('[0-9a-f]{64}',given):raise ValueError
        secret=hmac.new(b'WebAppData',token.encode(),hashlib.sha256).digest()
        check='\n'.join(k+'='+v for k,v in sorted(data.items()))
        expected=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected,given):raise ValueError
        user=json.loads(data['user']);uid=user['id']
        if type(uid) is not int or not 0<uid<2**52 or user.get('is_bot'):raise ValueError
        return uid
    except (ValueError,TypeError,KeyError,OverflowError):
        raise PersonalError('unauthorized','Открой панель заново из Telegram.',401) from None


def revalidate(db,req):
    profile_row=profile(db,req.uid)
    if req.model==PRO and profile_row['effective_tier']!='pro':
        with db.tx():
            q=quota(db,req.uid,FLASH)
            if not q['remaining']:raise PersonalError('quota','Лимит доступной модели сейчас занят.',429,q['reset_at'])
            db.c.execute("UPDATE personal_jobs SET model=? WHERE user_id=? AND chat_id=? AND message_id=? AND state='pending'",(FLASH,req.uid,req.cid,req.mid))
            req.model=FLASH;req.notice=profile_row['notice']
    return req.model


def available(model):
    if not isinstance(model,dict) or not model.get('rates') or model.get('deprecated'):return False
    if str(model.get('status','')).lower() in {'retired','disabled','unavailable','deprecated'}:return False
    from datetime import datetime,timezone
    if model.get('expiration_date') and str(model['expiration_date'])[:10]<=datetime.now(timezone.utc).date().isoformat():return False
    arch=model.get('architecture') or {}
    return 'text' in arch.get('input_modalities',[]) and set(arch.get('output_modalities',[]))=={'text'}


def uncertain_charge(db,req):
    for rid in req.usage_ids:
        row=db.one('SELECT cost,estimated,state FROM usage WHERE id=?',(rid,))
        if row and row['cost']>0 and row['estimated'] and row['state'] in {'reserved','error'}:return True
    return False


def context_notes(db,uid,question):
    """Private-only caller; rank explicit notes without adding every note to every turn."""
    import telegram_context as context
    rows=notes(db,uid)
    if re.search(r'^что помнишь[?!. ]*$|что .{0,15}(?:помнишь|знаешь) (?:обо )?мне|моя память|мои предпочтения',question,re.I):
        return rows[:8]
    return context.select_memories(rows,question,uid,limit=8,budget=2000)
