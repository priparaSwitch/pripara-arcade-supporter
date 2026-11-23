"""Replaceable, read-only PCR1 outfit catalog and optional image archive."""
from collections import OrderedDict
from contextlib import closing
from pathlib import Path
import io
import re
import sqlite3
import struct
import zipfile

SLOTS=('top','bottom','shoes','head')
CATEGORIES=set(SLOTS)

class CatalogError(ValueError):
    pass

def _integer(value,field,minimum=0,maximum=0x7fffffff):
    if type(value) is not int or not minimum<=value<=maximum:
        raise CatalogError(field+' 값이 올바른 정수가 아니에요.')
    return value

def _text(value,field,required=False):
    if not isinstance(value,str) or (required and not value.strip()):
        raise CatalogError(field+' 값이 올바른 문자열이 아니에요.')
    return value

def _boolean(value,field):
    return bool(_integer(value,field,0,1))

class Catalog:
    """No private defaults, no fixed release hash, and no writable DB handle."""
    def __init__(self,folder,strict=False):
        self.root=Path(folder);self.items={};self.sets=[];self.data_version=''
        self.available=False;self.error=None;self.images_available=False
        self.image_status='이미지 파일 없음 · 목록만 표시해요.'
        self._zip=None;self._entries={};self._cache=OrderedDict();self._unavailable=set()
        try:
            self._load(self.root/'data.db');self.available=True
            self._open_art(self.root/'images.bin')
        except (OSError,sqlite3.Error,CatalogError,ValueError) as error:
            self.items={};self.sets=[];self.error=str(error)
            if strict:raise CatalogError(str(error)) from error
    def _load(self,path):
        if not path.is_file():raise CatalogError("프로그램과 같은 폴더에 data.db 파일을 넣어 주세요.")
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row
            if db.execute('PRAGMA application_id').fetchone()[0]!=0x50435231:
                raise CatalogError('코디 리모컨용 데이터 파일이 아니에요.')
            if db.execute('PRAGMA user_version').fetchone()[0]!=1:
                raise CatalogError('지원하지 않는 코디 데이터 형식이에요.')
            tables={row[0]:row[1] for row in db.execute("SELECT name,type FROM sqlite_master WHERE name IN ('meta','items','sets','item_meta')")}
            if any(tables.get(name)!='table' for name in ('meta','items','sets')):
                raise CatalogError('필수 코디 데이터 테이블이 없어요.')
            meta={}
            for row in db.execute('SELECT key,value FROM meta'):
                key=_text(row[0],'meta.key');value=_text(row[1],'meta.value')
                if key in meta:raise CatalogError('코디 데이터 설정 키가 중복돼요.')
                meta[key]=value
            if meta.get('schema_version')!='1':raise CatalogError('지원하지 않는 코디 데이터 형식이에요.')
            if 'data_version' not in meta:raise CatalogError('코디 데이터 버전 정보가 없어요.')
            self.data_version=meta['data_version'];counts={}
            for name in ('item_count','set_count','image_count'):
                value=meta.get(name,'')
                if not re.fullmatch(r'[0-9]+',value):raise CatalogError('코디 데이터의 개수 정보가 올바르지 않아요.')
                counts[name]=int(value)
            for row in db.execute('SELECT id,name,jpName,brand,brandName,category,model,isDress,sourceRow,thumbnail,search FROM items ORDER BY id'):
                item=dict(row);identity=_integer(item['id'],'items.id',1)
                if identity in self.items:raise CatalogError('중복 의상 ID가 있어요.')
                for field in ('name','jpName','brandName','category','model','search'):
                    item[field]=_text(item[field],'items.'+field,field=='name')
                for field in ('brand','sourceRow'):item[field]=_integer(item[field],'items.'+field)
                for field in ('isDress','thumbnail'):item[field]=_boolean(item[field],'items.'+field)
                if item['category'] not in CATEGORIES or (item['isDress'] and item['category']!='top'):
                    raise CatalogError('의상 부위 정보가 올바르지 않아요.')
                item.update(grade=0,hidden=False,aux=None,aux_known=False)
                item['_search']=' '.join([str(identity),item['name'],item['jpName'],item['brandName'],item['model'],item['search']]).casefold()
                self.items[identity]=item
            if not self.items:raise CatalogError('의상 목록이 비어 있어요.')
            if tables.get('item_meta')=='table':
                columns={row[1] for row in db.execute('PRAGMA table_info(item_meta)')}
                if not {'id','grade','hidden'}<=columns:raise CatalogError('의상 추가 정보의 열이 올바르지 않아요.')
                sql='SELECT id,grade,hidden'+(',aux' if 'aux' in columns else '')+' FROM item_meta ORDER BY id'
                seen=set()
                for row in db.execute(sql):
                    identity=_integer(row['id'],'item_meta.id',1)
                    if identity in seen or identity not in self.items:raise CatalogError('의상 추가 정보의 ID가 올바르지 않아요.')
                    seen.add(identity);item=self.items[identity]
                    item['grade']=_integer(row['grade'],'item_meta.grade');item['hidden']=_boolean(row['hidden'],'item_meta.hidden')
                    if 'aux' in columns and row['aux'] is not None:
                        item['aux']=_integer(row['aux'],'item_meta.aux',0,254);item['aux_known']=True
            seen=set()
            for row in db.execute('SELECT id,name,brand,brandName,search,top,bottom,shoes,head FROM sets ORDER BY name,id'):
                source=dict(row);identity=_text(source['id'],'sets.id',True)
                if identity in seen:raise CatalogError('중복 세트 ID가 있어요.')
                seen.add(identity)
                record={key:_text(source[key],'sets.'+key,key=='name') for key in ('id','name','brandName','search')}
                record['brand']=_integer(source['brand'],'sets.brand');pieces={}
                for slot in SLOTS:
                    identity=_integer(source[slot],'sets.'+slot)
                    if identity and (identity not in self.items or self.items[identity]['category']!=slot):
                        raise CatalogError('세트의 의상 ID 또는 부위가 목록과 맞지 않아요.')
                    pieces[slot]=identity
                if not any(pieces.values()):raise CatalogError('의상이 없는 세트가 있어요.')
                self.validate_outfit(pieces)
                record['pieces']=pieces;record['items']=[pieces[slot] for slot in SLOTS]
                for slot in SLOTS:record[slot+'_id']=pieces[slot]
                record['hair_id']=pieces['head']
                record['_search']=' '.join([record['name'],record['brandName'],record['search'],record['id']]+[str(x) for x in pieces.values()]+[self.name(x) for x in pieces.values() if x]).casefold()
                self.sets.append(record)
            if len(self.items)!=counts['item_count'] or len(self.sets)!=counts['set_count']:
                raise CatalogError('코디 데이터의 목록 개수 정보가 일치하지 않아요.')
    def _open_art(self,path):
        if not path.is_file():return
        archive=None
        try:
            archive=zipfile.ZipFile(path,'r');entries={}
            for entry in archive.infolist():
                match=re.fullmatch(r'([0-9]+)\.png',entry.filename,re.IGNORECASE)
                if match and int(match[1]) in self.items and int(match[1]) not in entries:
                    entries[int(match[1])]=entry
            self._zip=archive;self._entries=entries;self.images_available=bool(entries)
            self.image_status='이미지 파일 연결됨' if entries else '이미지 파일에 표시할 그림이 없어요. 목록은 계속 사용할 수 있어요.'
        except (OSError,ValueError,zipfile.BadZipFile):
            if archive:archive.close()
            self.image_status='이미지 파일을 읽지 못해 목록만 표시해요.'
    def art_bytes(self,identity):
        if identity in self._cache:
            value=self._cache.pop(identity);self._cache[identity]=value;return value
        if not self._zip or identity in self._unavailable or identity not in self._entries:return None
        try:
            entry=self._entries[identity]
            if not 24<=entry.file_size<=8*1024*1024:raise ValueError('Artwork is too large')
            with self._zip.open(entry) as source:data=source.read(8*1024*1024+1)
            if len(data)!=entry.file_size or data[:8]!=b'\x89PNG\r\n\x1a\n' or data[12:16]!=b'IHDR':raise ValueError('Invalid artwork')
            width,height=struct.unpack('>II',data[16:24])
            if not 0<width<=4096 or not 0<height<=4096:raise ValueError('Invalid artwork size')
            from PIL import Image
            with Image.open(io.BytesIO(data)) as image:
                if image.format!='PNG':raise ValueError('Invalid artwork')
                image.verify()
            self._cache[identity]=data
            while len(self._cache)>320:self._cache.popitem(last=False)
            return data
        except (OSError,ValueError,RuntimeError,zipfile.BadZipFile):
            self._unavailable.add(identity);self.image_status='일부 이미지를 표시할 수 없어요. 목록은 계속 사용할 수 있어요.';return None
    def name(self,identity):
        return self.items.get(int(identity),{}).get('name',('아이템 '+str(identity)) if identity else '없음')
    def validate_outfit(self,outfit):
        if not isinstance(outfit,dict):raise CatalogError('코디 선택이 올바르지 않아요.')
        result={slot:_integer(outfit.get(slot,0),slot) for slot in SLOTS}
        if not any(result.values()):raise CatalogError('의상을 한 부위 이상 선택해 주세요.')
        for slot,identity in result.items():
            if identity and (identity not in self.items or self.items[identity]['category']!=slot):
                raise CatalogError('의상 부위가 맞지 않아요. 다시 선택해 주세요.')
        if result['top'] and self.items[result['top']]['isDress'] and result['bottom']:
            raise CatalogError('원피스에는 하의를 함께 선택할 수 없어요.')
        return result
    def list_items(self,category='all',query='',*,search=None,brand='',sort='id',descending=False):
        query=str(query if search is None else search).strip().casefold()
        rows=[x for x in self.items.values() if not x['hidden'] and (category=='all' or x['category']==category) and (not query or query in x['_search']) and (not brand or x['brandName']==brand)]
        key=(lambda x:(x['name'],x['id'])) if sort=='name' else (lambda x:(x['grade'],x['id'])) if sort=='grade' else (lambda x:x['id'])
        return sorted(rows,key=key,reverse=bool(descending))
    def list_sets(self,query='',*,search=None,brand='',sort='name',descending=False):
        query=str(query if search is None else search).strip().casefold()
        rows=[x for x in self.sets if (not query or query in x['_search']) and (not brand or x['brandName']==brand)]
        def identity(row):
            last=row['id'].rsplit('-',1)[-1]
            return int(last) if last.isdecimal() else 0x7fffffff
        key=(lambda x:(identity(x),x['id'])) if sort=='id' else (lambda x:(x['name'],x['id']))
        return sorted(rows,key=key,reverse=bool(descending))
    def sets_for_item(self,identity):
        item=self.items.get(identity)
        return [row for row in self.sets if item and row['pieces'][item['category']]==identity]
    def close(self):
        if self._zip:self._zip.close();self._zip=None
        self._cache.clear();self._entries.clear();self.images_available=False
    def __enter__(self):return self
    def __exit__(self,*unused):self.close()
