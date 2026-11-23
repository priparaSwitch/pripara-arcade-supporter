"""Incremental, local-only ticket index. Per-operation SQLite connections."""
from __future__ import annotations
import copy,hashlib,json,os,re,sqlite3,threading,time,math
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime
from email.utils import parsedate_to_datetime
from ticket_codec import decode_ticket,TicketDecodeError
from ticket_trash import TicketTrash,TicketTrashError

_APP_ID=0x50545431
_IMAGE_SUFFIXES={'.png','.bmp','.jpg','.jpeg','.webp'}
_FULL_TICKET=re.compile(r'myticket_\d{4}-\d\d-\d\d_\d{6}$',re.IGNORECASE)
_PRINT_STAMP=re.compile(r'(?:^|_)(\d{4}-\d{2}-\d{2})[_T](\d{6})(?=_|[ (]|$)')
_PNG_TIMES=('Creation Time','pripara_created_at','created_at','creation_time','printed_at')

def _valid_timestamp(value):
    try:
        timestamp=float(value)
        if not math.isfinite(timestamp) or timestamp<=0:return 0.
        moment=datetime.fromtimestamp(timestamp)
        return timestamp if 1970<=moment.year<=9999 else 0.
    except (ValueError,TypeError,OverflowError,OSError):return 0.

def ticket_timestamp(path,mtime_ns=0,*,image_path=None):
    """Actual print time only: filename, PNG creation metadata, then file mtime.

    QR profile dates are compatibility fields and are never consulted here.
    image_path differs only while a recycled image retains its original name.
    """
    path=Path(path);match=_PRINT_STAMP.search(path.stem)
    if match:
        try:timestamp=_valid_timestamp(datetime.strptime(match[1]+'_'+match[2],'%Y-%m-%d_%H%M%S').timestamp())
        except (ValueError,OverflowError,OSError):timestamp=0.
        if timestamp:return timestamp,'filename'
    image_path=Path(image_path) if image_path else path
    if image_path.suffix.casefold()=='.png':
        try:
            from PIL import Image
            with Image.open(image_path) as picture:metadata={key:picture.info.get(key) for key in _PNG_TIMES}
            for key in _PNG_TIMES:
                value=metadata.get(key)
                if not isinstance(value,(str,int,float)) or isinstance(value,bool):continue
                timestamp=_valid_timestamp(value)
                if not timestamp and isinstance(value,str):
                    try:timestamp=_valid_timestamp(datetime.fromisoformat(value.strip().replace('Z','+00:00')).timestamp())
                    except (ValueError,OverflowError,OSError):pass
                if not timestamp and isinstance(value,str):
                    try:timestamp=_valid_timestamp(parsedate_to_datetime(value).timestamp())
                    except (ValueError,TypeError,OverflowError,OSError):pass
                if timestamp:return timestamp,'png'
        except (OSError,ValueError,SyntaxError):pass
    timestamp=_valid_timestamp(float(mtime_ns)/1_000_000_000) if isinstance(mtime_ns,(int,float)) and not isinstance(mtime_ns,bool) else 0.
    return (timestamp,'mtime') if timestamp else (0.,'unknown')

class TicketStore:
    def __init__(self,dbpath,catalog=None,*,decoder=None):
        self.dbpath=Path(dbpath);self.dbpath.parent.mkdir(parents=True,exist_ok=True)
        self.catalog=catalog;self.decoder=decoder;self._lock=threading.RLock();self._coord_cache={};self._decoded_cache=OrderedDict();self._epochs={}
        self._trash=TicketTrash(self.dbpath.resolve().parent/'ticket-trash')
        with self._connect() as db:
            app=db.execute('PRAGMA application_id').fetchone()[0]
            version=db.execute('PRAGMA user_version').fetchone()[0]
            tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            if tables and (app!=_APP_ID or version not in (1,2,3,4)):raise ValueError('지원하는 티켓 보관함 데이터베이스가 아닙니다.')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS tickets (
                    id INTEGER PRIMARY KEY,path TEXT NOT NULL,path_key TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('my','tomo')),
                    size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,digest TEXT,
                    uid TEXT,identity_key TEXT NOT NULL,nickname TEXT NOT NULL,
                    player_code TEXT,top_id INTEGER NOT NULL DEFAULT 0,
                    profile_json TEXT,raw BLOB,decoded INTEGER NOT NULL,
                    issue TEXT,generated INTEGER NOT NULL DEFAULT 0,
                    missing INTEGER NOT NULL DEFAULT 0,updated REAL NOT NULL,
                    UNIQUE(path_key,kind));
                CREATE INDEX IF NOT EXISTS tickets_kind_identity ON tickets(kind,identity_key);
                CREATE TABLE IF NOT EXISTS profiles (
                    identity_key TEXT PRIMARY KEY,uid TEXT,profile_id INTEGER,
                    make_id INTEGER,manager INTEGER,nickname TEXT NOT NULL,
                    aliases_json TEXT NOT NULL,template BLOB NOT NULL,
                    latest_ticket INTEGER,source_mtime_ns INTEGER NOT NULL DEFAULT 0,updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS ticket_trash (
                    id INTEGER PRIMARY KEY,original_path TEXT NOT NULL,trash_path TEXT NOT NULL,
                    ticket_ids TEXT NOT NULL,kinds TEXT NOT NULL,deleted_at REAL NOT NULL,
                    state TEXT NOT NULL DEFAULT 'active',records_json TEXT);
                CREATE TABLE IF NOT EXISTS user_notes (
                    uid TEXT PRIMARY KEY,note TEXT NOT NULL,updated REAL NOT NULL);
            ''')
            columns={r[1] for r in db.execute('PRAGMA table_info(profiles)')}
            if 'source_mtime_ns' not in columns:db.execute('ALTER TABLE profiles ADD COLUMN source_mtime_ns INTEGER NOT NULL DEFAULT 0')
            columns={r[1] for r in db.execute('PRAGMA table_info(tickets)')}
            if 'trashed' not in columns:db.execute('ALTER TABLE tickets ADD COLUMN trashed INTEGER NOT NULL DEFAULT 0')
            if 'trash_id' not in columns:db.execute('ALTER TABLE tickets ADD COLUMN trash_id INTEGER')
            if 'printed_timestamp' not in columns:db.execute('ALTER TABLE tickets ADD COLUMN printed_timestamp REAL NOT NULL DEFAULT 0')
            if 'time_source' not in columns:db.execute("ALTER TABLE tickets ADD COLUMN time_source TEXT NOT NULL DEFAULT 'unknown'")
            if 'ticket_note' not in columns:db.execute("ALTER TABLE tickets ADD COLUMN ticket_note TEXT NOT NULL DEFAULT ''")
            if 'records_json' not in {r[1] for r in db.execute('PRAGMA table_info(ticket_trash)')}:db.execute('ALTER TABLE ticket_trash ADD COLUMN records_json TEXT')
            if version<2:self._migrate_identity(db)
            if version<3:
                for row in db.execute('SELECT t.id,t.path,t.mtime_ns,r.original_path FROM tickets t LEFT JOIN ticket_trash r ON r.id=t.trash_id').fetchall():
                    timestamp,source=ticket_timestamp(row['original_path'] or row['path'],row['mtime_ns'],image_path=row['path'])
                    db.execute('UPDATE tickets SET printed_timestamp=?,time_source=? WHERE id=?',(timestamp,source,row['id']))
            db.execute('PRAGMA application_id='+str(_APP_ID));db.execute('PRAGMA user_version=4')
    @contextmanager
    def _connect(self):
        db=sqlite3.connect(str(self.dbpath),timeout=5)
        db.row_factory=sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback();raise
        finally:db.close()
    def set_catalog(self,catalog):
        with self._lock:self.catalog=catalog;self._coord_cache.clear()
    def _decode(self,raw):
        raw=bytes(raw)
        with self._lock:
            profile=self._decoded_cache.pop(raw,None)
            if profile is None:profile=decode_ticket(raw)
            self._decoded_cache[raw]=profile
            while len(self._decoded_cache)>512:self._decoded_cache.popitem(last=False)
            return copy.deepcopy(profile)
    def _remember_profile(self,db,profile,mtime_ns,latest_ticket,aliases=()):
        if not profile['can_generate']:return
        identity=profile['identity_key'];now=time.time()
        old=db.execute('SELECT * FROM profiles WHERE identity_key=?',(identity,)).fetchone()
        known=json.loads(old['aliases_json']) if old else []
        for name in (*aliases,profile['nickname']):
            if name not in known:known.append(name)
        if old and (old['source_mtime_ns'],old['latest_ticket'] or 0)>(mtime_ns,latest_ticket or 0):
            db.execute('UPDATE profiles SET aliases_json=? WHERE identity_key=?',(json.dumps(known,ensure_ascii=False),identity));return
        db.execute('''INSERT INTO profiles(identity_key,uid,profile_id,make_id,manager,nickname,aliases_json,template,latest_ticket,source_mtime_ns,updated)
            VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(identity_key) DO UPDATE SET uid=excluded.uid,profile_id=excluded.profile_id,make_id=excluded.make_id,manager=excluded.manager,nickname=excluded.nickname,aliases_json=excluded.aliases_json,template=excluded.template,latest_ticket=excluded.latest_ticket,source_mtime_ns=excluded.source_mtime_ns,updated=excluded.updated''',
            (identity,profile['uid'],profile['profile_id'],profile['make_id'],profile['manager'],profile['nickname'],json.dumps(known,ensure_ascii=False),profile['raw'],latest_ticket,mtime_ns,now))
    def _migrate_identity(self,db):
        """Reclassify metadata from original bytes; never rewrite QR payloads."""
        old_profiles=list(db.execute('SELECT * FROM profiles'));db.execute('DELETE FROM profiles')
        for row in old_profiles:
            try:profile=self._decode(row['template'])
            except (ValueError,TypeError):continue
            self._remember_profile(db,profile,row['source_mtime_ns'],row['latest_ticket'],json.loads(row['aliases_json']))
        for row in db.execute('SELECT * FROM tickets WHERE decoded=1 AND raw IS NOT NULL').fetchall():
            try:profile=self._decode(row['raw'])
            except (ValueError,TypeError):continue
            clean={key:value for key,value in profile.items() if key not in ('raw','template','body')}
            db.execute('UPDATE tickets SET uid=?,identity_key=?,nickname=?,player_code=?,profile_json=? WHERE id=?',
                (profile['uid'],profile['identity_key'],profile['nickname'],profile['player_code'],json.dumps(clean,ensure_ascii=False),row['id']))
            self._remember_profile(db,profile,row['mtime_ns'],row['id'])
    @staticmethod
    def _key(path):return str(Path(path).resolve()).casefold() if os.name=='nt' else str(Path(path).resolve())
    def _codes(self,path):
        if self.decoder is not None:return [bytes(x) for x in self.decoder(Path(path))]
        from PIL import Image
        from direct_reader import decode_image
        with Image.open(path) as image:
            image.load();return decode_image(image)
    @staticmethod
    def _hint(path):
        name=path.stem.casefold()
        if name.endswith('_tomo') or name.startswith(('friend_','tomo_')) or 'tomo' in [p.casefold() for p in path.parts[:-1]]:return 'tomo'
        if name.endswith('_my'):return 'my'
        return None
    def _catalog_info(self,profile):
        items=profile.get('items',{});ids=tuple(int(items.get(k,0)) for k in ('top','bottom','shoes','hair'))
        cache=self._coord_cache.get(ids)
        if cache is not None:return cache
        records=getattr(self.catalog,'items',{}) if self.catalog is not None else {}
        names=[];search=[];top_id=ids[0]
        if sum(bool(i) for i in ids)==1:
            record=records.get(next(i for i in ids if i),{}) if isinstance(records,dict) else {}
            if record.get('category') not in (None,'top'):top_id=0
        for item in ids:
            if not item:continue
            record=records.get(item,{}) if isinstance(records,dict) else {}
            name=record.get('name')
            if not name and self.catalog is not None:
                try:name=self.catalog.name(item)
                except (KeyError,ValueError,AttributeError):pass
            name=name or '아이템 '+str(item)
            names.append(name);search.extend([str(item),name,str(record.get('jpName',''))])
        coord_name=' / '.join(dict.fromkeys(names)) or '코디 정보 없음'
        sets=getattr(self.catalog,'sets',[]) if self.catalog is not None else []
        for outfit in sets:
            pieces=outfit.get('pieces',{}) if isinstance(outfit,dict) else {}
            proposed=(pieces.get('top',0),pieces.get('bottom',0),pieces.get('shoes',0),pieces.get('head',pieces.get('hair',0)))
            if proposed==ids:
                coord_name=outfit.get('name') or coord_name;search.append(coord_name);break
        top_name=records.get(top_id,{}).get('name','') if isinstance(records,dict) and top_id else ''
        if not top_name and top_id and self.catalog is not None:
            try:top_name=self.catalog.name(top_id)
            except (KeyError,ValueError,AttributeError):pass
        result=dict(top_id=top_id,top_name=top_name or '상의 정보 없음',coord_name=coord_name,coord_search=' '.join(search).casefold())
        self._coord_cache[ids]=result
        return result
    def _row(self,row):
        data=dict(row)
        try:profile=self._decode(row['raw']) if row['decoded'] else None
        except (ValueError,TypeError):profile=None
        if profile:
            data.update(profile);data.update(self._catalog_info(profile))
        else:
            data.update(uid=None,decoded=False,appearance=[],parts={},items={},aux={},raw=None,body=None,template=None,can_generate=False,coord_name='해석하지 못한 티켓')
        data['decoded']=bool(profile);data['generated']=bool(row['generated']);data['missing']=bool(row['missing'])
        data['error']=data.get('issue') or ''
        data['timestamp']=_valid_timestamp(data.get('printed_timestamp')) or _valid_timestamp(row['mtime_ns']/1_000_000_000)
        data['printed_timestamp']=data['timestamp']
        return data
    def _upsert(self,path,kind,profile,issue,stat,*,generated=False,expected_epoch=None):
        key=self._key(path);now=time.time()
        raw=profile['raw'] if profile else None
        digest=hashlib.sha256(raw).hexdigest() if raw else None
        identity=profile['identity_key'] if profile else 'unknown-file:'+hashlib.sha256((key+'\0'+kind).encode()).hexdigest()
        clean={k:v for k,v in (profile or {}).items() if k not in ('raw','template','body')}
        printed_timestamp,time_source=ticket_timestamp(path,stat.st_mtime_ns)
        with self._lock,self._connect() as db:
            if expected_epoch is not None and self._epochs.get(key,0)!=expected_epoch:return None
            try:current=path.stat()
            except OSError:return None
            if (current.st_size,current.st_mtime_ns)!=(stat.st_size,stat.st_mtime_ns) or self._trash.contains(path):return None
            db.execute('''INSERT INTO tickets(path,path_key,kind,size,mtime_ns,digest,uid,identity_key,nickname,player_code,top_id,profile_json,raw,decoded,issue,generated,missing,updated)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)
                ON CONFLICT(path_key,kind) DO UPDATE SET path=excluded.path,size=excluded.size,mtime_ns=excluded.mtime_ns,digest=excluded.digest,uid=excluded.uid,identity_key=excluded.identity_key,nickname=excluded.nickname,player_code=excluded.player_code,top_id=excluded.top_id,profile_json=excluded.profile_json,raw=excluded.raw,decoded=excluded.decoded,issue=excluded.issue,generated=MAX(tickets.generated,excluded.generated),missing=0,updated=excluded.updated''',
                (str(path.resolve()),key,kind,stat.st_size,stat.st_mtime_ns,digest,profile['uid'] if profile else None,identity,profile['nickname'] if profile else '',profile['player_code'] if profile else None,profile['top_id'] if profile else 0,json.dumps(clean,ensure_ascii=False),raw,int(bool(profile)),issue,int(generated),now))
            db.execute('UPDATE tickets SET printed_timestamp=?,time_source=? WHERE path_key=? AND kind=?',
                       (printed_timestamp,time_source,key,kind))
            row=db.execute('SELECT * FROM tickets WHERE path_key=? AND kind=?',(key,kind)).fetchone()
            if profile:self._remember_profile(db,profile,stat.st_mtime_ns,row['id'])
        return self._row(row)
    def scan(self,directory,*,stop_requested=lambda:False):
        directory=Path(directory)
        result=dict(indexed=0,unknown=0,changed=0,unchanged=0,files=0,canceled=False)
        if not directory.is_dir():return result
        paths=sorted(p for p in directory.rglob('*') if p.is_file() and not p.is_symlink() and not self._trash.contains(p) and p.suffix.casefold() in _IMAGE_SUFFIXES and not _FULL_TICKET.fullmatch(p.stem))
        with self._lock,self._connect() as db:
            previous={row['path_key']:[] for row in db.execute('SELECT path_key FROM tickets WHERE trashed=0')}
            for row in db.execute('SELECT path_key,kind,size,mtime_ns,decoded,missing FROM tickets WHERE trashed=0'):previous[row['path_key']].append(dict(row))
        seen=set()
        for path in paths:
            if stop_requested():result['canceled']=True;break
            try:stat=path.stat()
            except OSError:continue
            key=self._key(path);seen.add(key);result['files']+=1;old=previous.get(key,[])
            with self._lock:epoch=self._epochs.get(key,0)
            if old and all(row['size']==stat.st_size and row['mtime_ns']==stat.st_mtime_ns for row in old):
                if any(row['missing'] for row in old):
                    with self._lock,self._connect() as db:db.execute('UPDATE tickets SET missing=0 WHERE path_key=? AND trashed=0',(key,))
                result['unchanged']+=1;result['indexed']+=sum(row['decoded'] for row in old);result['unknown']+=sum(not row['decoded'] for row in old);continue
            hint=self._hint(path);decoded={};issue='QR을 해석하지 못했습니다.'
            try:
                codes=self._codes(path)
                if not codes:issue='이미지에서 QR을 찾지 못했습니다.'
                for raw in codes:
                    try:
                        profile=self._decode(raw)
                        decoded.setdefault(profile['kind'],profile)
                    except TicketDecodeError:continue
            except (OSError,ValueError,RuntimeError):issue='이미지나 QR을 읽지 못했습니다.'
            kinds=list(decoded) or [hint or 'tomo']
            for kind in kinds:
                profile=decoded.get(kind)
                if self._upsert(path,kind,profile,None if profile else issue,stat,expected_epoch=epoch) is None:continue
                result['indexed' if profile else 'unknown']+=1
            if old:
                with self._lock,self._connect() as db:
                    db.execute('UPDATE tickets SET missing=1 WHERE path_key=? AND trashed=0 AND kind NOT IN ('+','.join('?' for _ in kinds)+')',(key,*kinds))
            result['changed']+=1
        if not result['canceled']:
            prefix=self._key(directory)+os.sep
            with self._lock,self._connect() as db:
                for row in db.execute('SELECT id,path_key,path FROM tickets WHERE trashed=0'):
                    # A file may have been restored after this scan took its
                    # directory snapshot. Do not hide that newly active file.
                    if row['path_key'].startswith(prefix) and row['path_key'] not in seen and not Path(row['path']).is_file():
                        db.execute('UPDATE tickets SET missing=1 WHERE id=?',(row['id'],))
        return result
    def tickets(self,kind,sort='name',search='',search_by='all',uid=None):
        if kind not in ('my','tomo'):raise ValueError('티켓 종류가 올바르지 않습니다.')
        if sort not in ('name','uid','top_id','top','id','recent'):raise ValueError('정렬 기준이 올바르지 않습니다.')
        if search_by not in ('all','name','coord','note'):raise ValueError('검색 기준이 올바르지 않습니다.')
        with self._lock,self._connect() as db:
            rows=list(db.execute('SELECT * FROM tickets WHERE kind=? AND missing=0 AND trashed=0',(kind,)))
            notes={row['uid']:row['note'] for row in db.execute('SELECT uid,note FROM user_notes')}
        records=[self._row(row) for row in rows]
        for record in records:record['user_note']=notes.get(record.get('uid'),'')
        if uid is not None:
            if uid=='__unknown__':records=[r for r in records if r['uid'] is None]
            else:records=[r for r in records if r['uid']==str(uid) or r['identity_key']==str(uid)]
        query=str(search).strip().casefold()
        if query:
            def searchable(record):
                if search_by=='name':return record['nickname']
                if search_by=='coord':return record.get('coord_search','')
                memo='\n'.join((record.get('user_note',''),record.get('ticket_note','')))
                if search_by=='note':return memo
                return '\n'.join((record['nickname'],record.get('coord_search',''),str(record.get('uid') or ''),memo))
            records=[r for r in records if query in searchable(r).casefold()]
        def latest(r):return (-r['printed_timestamp'],-r['mtime_ns'],-r['id'])
        def name_key(r):return (not r['decoded'],r['nickname'].casefold(),*latest(r))
        if sort=='name':records.sort(key=name_key)
        elif sort=='recent':records.sort(key=latest)
        elif sort in ('uid','id'):records.sort(key=lambda r:(r['uid'] is None,r['uid'] or r['identity_key'],*latest(r)))
        else:records.sort(key=lambda r:(r.get('top_id',0)==0,r.get('top_id',0),*latest(r)))
        return records
    def users(self,kind=None):
        if kind not in (None,'my','tomo'):raise ValueError('티켓 종류가 올바르지 않습니다.')
        with self._lock,self._connect() as db:
            rows=list(db.execute('SELECT * FROM profiles'))
            notes={row['uid']:row['note'] for row in db.execute('SELECT uid,note FROM user_notes')}
            counts={r['identity_key']:r['n'] for r in db.execute('SELECT identity_key,COUNT(*) n FROM tickets WHERE missing=0 AND trashed=0'+(' AND kind=?' if kind else '')+' GROUP BY identity_key',(() if kind is None else (kind,)))}
        result=[]
        for row in rows:
            if not counts.get(row['identity_key']):continue
            try:profile=self._decode(row['template'])
            except (ValueError,TypeError):continue
            uid=profile['uid'];label=(profile['nickname'] or '이름 없음')+' · '+(uid or '티켓 코드 미확인')
            result.append(dict(key=row['identity_key'],identity_key=row['identity_key'],uid=uid,nickname=profile['nickname'],label=label,aliases=json.loads(row['aliases_json']),count=counts.get(row['identity_key'],0),player_code=profile['player_code'],appearance=profile['appearance'],parts=profile['parts'],can_generate=True,note=notes.get(uid,'')))
        result.sort(key=lambda r:(r['nickname'].casefold(),r['uid'] or r['identity_key']))
        return result
    @staticmethod
    def _note_uid(uid):
        if not isinstance(uid,str) or not re.fullmatch(r'[0-9]{12}',uid) or uid=='000000000000':
            raise ValueError('확인된 유저 ID가 필요합니다.')
        return uid
    def user_note(self,uid):
        uid=self._note_uid(uid)
        with self._lock,self._connect() as db:row=db.execute('SELECT note FROM user_notes WHERE uid=?',(uid,)).fetchone()
        return row['note'] if row else ''
    def set_user_note(self,uid,note):
        uid=self._note_uid(uid)
        if not isinstance(note,str) or '\0' in note:raise ValueError('메모 내용을 확인해 주세요.')
        note=note.replace('\r\n','\n').replace('\r','\n')
        if not note.strip():note=''
        with self._lock,self._connect() as db:
            if note:db.execute('INSERT INTO user_notes(uid,note,updated) VALUES(?,?,?) ON CONFLICT(uid) DO UPDATE SET note=excluded.note,updated=excluded.updated',(uid,note,time.time()))
            else:db.execute('DELETE FROM user_notes WHERE uid=?',(uid,))
        return note
    @staticmethod
    def _ticket_id(value):
        if isinstance(value,dict):value=value.get('id')
        if isinstance(value,bool) or not isinstance(value,int) or value<=0:
            raise ValueError('보관함의 티켓을 선택해 주세요.')
        return value
    def ticket_note(self,ticket):
        ticket_id=self._ticket_id(ticket)
        with self._lock,self._connect() as db:
            row=db.execute('SELECT ticket_note FROM tickets WHERE id=?',(ticket_id,)).fetchone()
        if row is None:raise ValueError('보관함에서 티켓을 찾지 못했습니다.')
        return row['ticket_note']
    def set_ticket_note(self,ticket,note):
        ticket_id=self._ticket_id(ticket)
        if not isinstance(note,str) or '\0' in note:raise ValueError('메모 내용을 확인해 주세요.')
        note=note.replace('\r\n','\n').replace('\r','\n')
        if not note.strip():note=''
        with self._lock,self._connect() as db:
            cursor=db.execute('UPDATE tickets SET ticket_note=? WHERE id=?',(note,ticket_id))
            if cursor.rowcount!=1:raise ValueError('보관함에서 티켓을 찾지 못했습니다.')
        return note
    def profile(self,uid):
        value=str(uid)
        with self._lock,self._connect() as db:
            row=db.execute('SELECT * FROM profiles WHERE identity_key=? OR uid=? ORDER BY updated DESC LIMIT 1',(value,value)).fetchone()
        if not row:return None
        try:profile=self._decode(row['template'])
        except (ValueError,TypeError):return None
        profile.update(key=profile['identity_key'],aliases=json.loads(row['aliases_json']),count=0)
        return profile
    def register_generated(self,path,raw=None,kind='tomo'):
        if kind not in ('my','tomo'):raise ValueError('티켓 종류가 올바르지 않습니다.')
        path=Path(path);codes=self._codes(path)
        if raw is not None:
            raw=bytes(raw)
            if raw not in codes:raise ValueError('생성 이미지와 QR 데이터가 일치하지 않습니다.')
            profile=self._decode(raw)
        else:
            profile=None
            for candidate in codes:
                try:
                    decoded=self._decode(candidate)
                    if decoded['kind']==kind:profile=decoded;break
                except TicketDecodeError:continue
            if profile is None:raise ValueError('생성 이미지의 QR을 읽지 못했습니다.')
        if profile['kind']!=kind:raise ValueError('생성한 QR의 티켓 종류가 다릅니다.')
        return self._upsert(path,kind,profile,None,path.stat(),generated=True)
    @staticmethod
    def _identity(value):
        if isinstance(value,dict):value=value.get('trash_id') or value.get('id')
        if type(value) is not int or value<1:raise ValueError('티켓 번호가 올바르지 않습니다.')
        return value
    @staticmethod
    def _snapshot(rows):
        result=[]
        for row in rows:
            record=dict(row)
            if record.get('raw') is not None:record['raw']=bytes(record['raw']).hex()
            result.append(record)
        return json.dumps(result,ensure_ascii=False)
    @staticmethod
    def _saved_rows(entry):
        rows=json.loads(entry['records_json'] or '[]')
        for row in rows:
            if row.get('raw') is not None:row['raw']=bytes.fromhex(row['raw'])
        return rows
    def _entry(self,db,entry):
        ids=json.loads(entry['ticket_ids']);rows=[]
        if ids:rows=db.execute('SELECT * FROM tickets WHERE id IN ('+','.join('?' for _ in ids)+') AND trashed=1',ids).fetchall()
        rows=rows or self._saved_rows(entry)
        first=self._row(rows[0]) if rows else {}
        return dict(id=entry['id'],trash_id=entry['id'],path=entry['trash_path'],original_path=entry['original_path'],
            kinds=json.loads(entry['kinds']),ticket_count=len(rows),nickname=first.get('nickname',''),uid=first.get('uid'),
            coord_name=first.get('coord_name','코디 미확인'),deleted_at=entry['deleted_at'],timestamp=entry['deleted_at'],
            pending=entry['state']=='pending')
    def trash(self,ticket_id):
        """Recycle an entire image, including all My/Friend rows in that file."""
        ticket_id=self._identity(ticket_id);moved=None
        with self._lock:
            try:
                with self._connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    chosen=db.execute('SELECT * FROM tickets WHERE id=? AND trashed=0',(ticket_id,)).fetchone()
                    if chosen is None:raise ValueError('보관함에서 티켓을 찾지 못했습니다.')
                    source=Path(chosen['path']);rows=db.execute('SELECT * FROM tickets WHERE path_key=? AND trashed=0',(chosen['path_key'],)).fetchall()
                    destination=self._trash.destination(source);kinds=sorted({row['kind'] for row in rows});ids=[row['id'] for row in rows]
                    cursor=db.execute('INSERT INTO ticket_trash(original_path,trash_path,ticket_ids,kinds,deleted_at,records_json) VALUES(?,?,?,?,?,?)',
                        (str(source),str(destination),json.dumps(ids),json.dumps(kinds),time.time(),self._snapshot(rows)))
                    trash_id=cursor.lastrowid
                    self._trash.move(source,destination);moved=(destination,source)
                    db.execute('UPDATE tickets SET path=?,path_key=?,trashed=1,trash_id=?,missing=0 WHERE path_key=? AND trashed=0',
                        (str(destination),self._key(destination),trash_id,chosen['path_key']))
                    result=self._entry(db,db.execute('SELECT * FROM ticket_trash WHERE id=?',(trash_id,)).fetchone())
                self._epochs[self._key(source)]=self._epochs.get(self._key(source),0)+1
                return result
            except Exception:
                if moved:self._trash.move(*moved)
                raise
    def trashed(self,kind=None):
        if kind not in (None,'my','tomo'):raise ValueError('티켓 종류가 올바르지 않습니다.')
        with self._lock,self._connect() as db:
            rows=db.execute("SELECT * FROM ticket_trash WHERE state IN ('active','pending') ORDER BY deleted_at DESC,id DESC").fetchall()
            return [self._entry(db,row) for row in rows if (kind is None or kind in json.loads(row['kinds'])) and Path(row['trash_path']).is_file()]
    def trash_tickets(self,kind=None):return self.trashed(kind)
    def restore(self,trash_id):
        trash_id=self._identity(trash_id);moved=None
        with self._lock:
            try:
                with self._connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    entry=db.execute("SELECT * FROM ticket_trash WHERE id=? AND state IN ('active','pending')",(trash_id,)).fetchone()
                    if not entry:raise ValueError('휴지통에서 티켓을 찾지 못했습니다.')
                    source=self._trash.validate_entry(entry['trash_path']);original=Path(entry['original_path'])
                    destination=self._trash.free_destination(original)
                    while db.execute('SELECT 1 FROM tickets WHERE path_key=? AND trashed=0',(self._key(destination),)).fetchone():
                        destination=self._trash.free_destination(destination.with_name(destination.stem+' (복원)'+destination.suffix))
                    ids=json.loads(entry['ticket_ids']);rows=[]
                    if ids:rows=db.execute('SELECT * FROM tickets WHERE id IN ('+','.join('?' for _ in ids)+') AND trashed=1',ids).fetchall()
                    rows=rows or self._saved_rows(entry)
                    self._trash.move(source,destination);moved=(destination,source);information=destination.stat();restored=[]
                    for row in rows:
                        # Keep the indexed stat signature. If a source image
                        # changed before recycling, the next scan must decode
                        # its new pixels rather than cache stale QR metadata.
                        values=dict(row);identity=values.pop('id');values.update(path=str(destination),path_key=self._key(destination),trashed=0,trash_id=None,missing=0,updated=time.time())
                        if db.execute('SELECT 1 FROM tickets WHERE id=? AND trashed=1',(identity,)).fetchone():
                            db.execute('UPDATE tickets SET '+','.join(key+'=?' for key in values)+' WHERE id=?',(*values.values(),identity))
                        else:
                            cursor=db.execute('INSERT INTO tickets('+','.join(values)+') VALUES('+','.join('?' for _ in values)+')',tuple(values.values()));identity=cursor.lastrowid
                        restored.append(identity)
                        if values.get('raw'):
                            try:profile=self._decode(values['raw'])
                            except ValueError:continue
                            self._remember_profile(db,profile,values['mtime_ns'],identity)
                    db.execute("UPDATE ticket_trash SET state='restored',records_json=NULL WHERE id=?",(trash_id,))
                for path in (original,destination):
                    key=self._key(path);self._epochs[key]=self._epochs.get(key,0)+1
                return dict(id=trash_id,trash_id=trash_id,path=str(destination),restored_count=len(restored),collision_renamed=destination!=original)
            except Exception:
                if moved:self._trash.move(*moved)
                raise
    def empty_trash(self,kind=None):
        """Permanently remove selected recycled files; mixed files stay atomic.

        A verified quarantine copy survives any database rollback. A committed
        deletion intent retains row snapshots until the final unlink succeeds,
        so an unlink failure remains visible and can still be restored.
        """
        if kind not in (None,'my','tomo'):raise ValueError('티켓 종류가 올바르지 않습니다.')
        count=0
        with self._lock:
            with self._connect() as db:entries=db.execute("SELECT * FROM ticket_trash WHERE state IN ('active','pending') ORDER BY id").fetchall()
            for old in entries:
                if kind is not None and kind not in json.loads(old['kinds']):continue
                moved=None
                try:
                    with self._connect() as db:
                        db.execute('BEGIN IMMEDIATE')
                        entry=db.execute("SELECT * FROM ticket_trash WHERE id=? AND state IN ('active','pending')",(old['id'],)).fetchone()
                        if entry is None:continue
                        source=self._trash.validate_entry(entry['trash_path'],exists=False)
                        ids=json.loads(entry['ticket_ids']);rows=[]
                        if ids:rows=db.execute('SELECT * FROM tickets WHERE id IN ('+','.join('?' for _ in ids)+') AND trashed=1',ids).fetchall()
                        snapshot=self._snapshot(rows) if rows else entry['records_json']
                        if source.exists():
                            destination=self._trash.destination(source).with_suffix('.pending')
                            self._trash.move(source,destination);moved=(destination,source)
                        else:destination=source
                        if ids:db.execute('DELETE FROM tickets WHERE id IN ('+','.join('?' for _ in ids)+') AND trashed=1',ids)
                        db.execute("UPDATE ticket_trash SET state='pending',trash_path=?,records_json=? WHERE id=?",(str(destination),snapshot,entry['id']))
                except Exception:
                    if moved:self._trash.move(*moved)
                    raise
                # Intent and snapshot are durable before this irreversible step.
                existed=destination.exists();self._trash.erase(destination)
                try:
                    with self._connect() as db:db.execute("UPDATE ticket_trash SET state='deleted',records_json=NULL WHERE id=?",(old['id'],))
                except sqlite3.Error:
                    # The committed intent already authorizes the completed
                    # deletion. Keep the durable pending marker for retry;
                    # cleanup failure must not report an intact file to UI.
                    pass
                count+=int(existed)
        return count
    def close(self):
        """Connections are scoped to each operation; there is no open handle."""
        return None
