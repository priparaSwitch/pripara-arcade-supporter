"""Local HK MyTicket creation, matching the existing public MyChara codec."""
from __future__ import annotations
import copy,io,json,secrets,sys,uuid
from pathlib import Path
from ticket_codec import pack_body,decode_ticket,PLAYER_CODE_XOR,MAX_MAKE_ID
from protocol_delivery import _write_radix255
from ipc_files import atomic_json,atomic_bytes
from rank_service import apply_native_rank

TOTAL_LIKES=20_000_000
MAX_NUMBER=255**4-1
CATEGORIES=(('face','눈매'),('hair','헤어스타일'),('hairColor','헤어컬러'),('eye','아이컬러'),('skin','피부색'),('glasses','안경'),('accent','원포인트'))
_NAME={'!':413,'?':414,'★':415,'☆':416,'♪':412}
_NAME.update((chr(65+i),201+i) for i in range(26));_NAME.update((chr(97+i),301+i) for i in range(26));_NAME.update((str(i),400+i) for i in range(10))

def _integer(value,maximum,label,minimum=0):
    if type(value) is not int or not minimum<=value<=maximum:raise ValueError(label+' 값이 올바른 정수 범위가 아닙니다.')
    return value
def validate_name(name):
    if not isinstance(name,str) or not 1<=len(name)<=6 or any(char not in _NAME for char in name):raise ValueError('이름은 영문·숫자·! ? ★ ☆ ♪를 사용해 1~6자로 입력해 주세요.')
    return [_NAME[char] for char in name]+[0]*(6-len(name))
def parse_likes(text):
    if not isinstance(text,str) or not text.strip().isascii() or not text.strip().isdigit():raise ValueError('좋아요는 0 이상의 정수로 입력해 주세요.')
    return _integer(int(text.strip()),MAX_NUMBER,'좋아요')
def normalize_player_code(code):
    if not isinstance(code,str) or not code.strip().isascii() or not code.strip().isdigit() or not 1<=len(code.strip())<=12:raise ValueError('플레이어 코드는 숫자 1~12자리로 입력해 주세요.')
    number=int(code.strip())
    if not 0<number<=0xffffff:raise ValueError('홍콩판에서 유지할 수 있는 번호를 입력하거나 임의생성을 눌러 주세요.')
    make=number^PLAYER_CODE_XOR
    if not 0<make<=MAX_MAKE_ID:raise ValueError('게임에서 신규 캐릭터로 초기화되지 않는 다른 번호를 사용해 주세요.')
    return f'{number:012d}'
def random_player_code(exclude=''):
    for _ in range(1000):
        number=(secrets.randbelow(MAX_MAKE_ID)+1)^PLAYER_CODE_XOR;code=f'{number:012d}'
        if number and code!=exclude:return code
    raise ValueError('사용할 수 있는 번호를 만들지 못했습니다.')
def create_identity():return {'profileId':secrets.randbelow(MAX_NUMBER)+1,'playerCode':random_player_code()}

def build_payload(*,name,appearance,outfit,profileId,playerCode,likes=TOTAL_LIKES,aux=None):
    """Outfit/aux order follows the web app: head, top, bottom, shoes."""
    codes=validate_name(name)
    if not isinstance(appearance,(list,tuple)) or len(appearance)!=9:raise ValueError('외형 값은 9개가 필요합니다.')
    if not isinstance(outfit,(list,tuple)) or len(outfit)!=4:raise ValueError('의상 값은 4개가 필요합니다.')
    for value in appearance:_integer(value,254,'외형')
    for value in outfit:_integer(value,65024,'의상')
    if aux is None:aux=[0]*4
    if not isinstance(aux,(list,tuple)) or len(aux)!=4:raise ValueError('의상 보조값은 4개가 필요합니다.')
    for value in aux:_integer(value,254,'의상 보조값')
    _integer(profileId,MAX_NUMBER,'내부 식별자',1);_integer(likes,MAX_NUMBER,'좋아요')
    make=int(normalize_player_code(playerCode))^PLAYER_CODE_XOR
    body=bytearray([1]*121);body[:4]=b'UI\x04\x03'
    for index,slot in enumerate((1,2,3,0)):
        _write_radix255(body,4+index*3,2,outfit[slot]);_write_radix255(body,6+index*3,1,aux[slot])
    for offset,width,value in ((16,4,profileId),(20,4,make),(26,3,1),(29,4,likes),(33,4,likes)):_write_radix255(body,offset,width,value)
    body[48:51]=bytes((27,11,30));body[58]=94
    apply_native_rank(body,likes)
    for index,value in enumerate(appearance):body[66+index]=value+1
    for index,value in enumerate(codes):_write_radix255(body,105+index*2,2,value)
    return pack_body(body)

class CharacterCatalog:
    def __init__(self,assets=None):
        self.root=Path(assets) if assets is not None else Path(getattr(sys,'_MEIPASS',Path(__file__).parent))/'character_assets'
        self.data=json.loads((self.root/'parts.json').read_text(encoding='utf-8'))
        self.parts=self.data['parts'];self.defaults=self.data['defaults'];self.default_outfit=self.data['defaultOutfit']
        if len(self.default_outfit)!=4:raise ValueError('기본 코디 정보가 올바르지 않습니다.')
        for category,_ in CATEGORIES:
            if not self.parts.get(category) or not any(row['id']==self.defaults.get(category) for row in self.parts[category]):raise ValueError('캐릭터 파츠 목록을 확인해 주세요.')
    def row(self,parts,category):
        return next(row for row in self.parts[category] if row['id']==parts[category])
    def image(self,row):
        relative=Path(row.get('image',''))
        if relative.is_absolute() or '..' in relative.parts:return None
        path=self.root/relative
        return path if path.is_file() and path.resolve().is_relative_to(self.root.resolve()) else None
    def compatible(self,eye,face):return not eye.get('supportedFaceModels') or face.get('model') in eye['supportedFaceModels']
    def validate_parts(self,parts):
        for category,_ in CATEGORIES:self.row(parts,category)
        if not self.compatible(self.row(parts,'eye'),self.row(parts,'face')):raise ValueError('선택한 눈매와 아이컬러는 함께 사용할 수 없습니다.')
    def appearance(self,parts):
        self.validate_parts(parts);value=lambda category:self.row(parts,category)['hkValue']
        return [value('face'),value('hair'),value('hairColor'),value('skin'),value('eye'),value('glasses'),0,value('accent'),0]

class CharacterCreator:
    def __init__(self,root,catalog=None,*,assets=None,identity_factory=create_identity):
        self.root=Path(root);self.catalog=CharacterCatalog(assets);self.outfit_catalog=catalog
        self.settings_path=self.root/'data/character_creator.json';self.identity=identity_factory();self.code_mode='random';self.issued=None;self.output=None
        try:saved=json.loads(self.settings_path.read_text(encoding='utf-8'))
        except (OSError,ValueError):saved={}
        if not isinstance(saved,dict):saved={}
        if type(saved.get('profileId')) is int and 0<saved['profileId']<=MAX_NUMBER:self.identity['profileId']=saved['profileId']
        self.name=saved.get('name','') if isinstance(saved.get('name',''),str) else '';self.name=self.name[:6]
        self.likes=saved['likes'] if type(saved.get('likes')) is int and 0<=saved['likes']<=MAX_NUMBER else TOTAL_LIKES
        previous=saved.get('parts',{});previous=previous if isinstance(previous,dict) else {}
        self.parts={category:previous.get(category) if any(row['id']==previous.get(category) for row in self.catalog.parts[category]) else self.catalog.defaults[category] for category,_ in CATEGORIES}
        self.ensure_eye()
    def ensure_eye(self):
        if self.catalog.compatible(self.catalog.row(self.parts,'eye'),self.catalog.row(self.parts,'face')):return False
        replacement=next(row for row in self.catalog.parts['eye'] if self.catalog.compatible(row,self.catalog.row(self.parts,'face')))
        self.parts['eye']=replacement['id'];return True
    def select(self,category,identity):
        if category not in dict(CATEGORIES) or not any(row['id']==identity for row in self.catalog.parts[category]):raise ValueError('사용할 수 없는 파츠입니다.')
        proposed=dict(self.parts);proposed[category]=identity
        if category=='eye' and not self.catalog.compatible(self.catalog.row(proposed,'eye'),self.catalog.row(proposed,'face')):raise ValueError('이 눈매에서는 선택할 수 없는 아이컬러입니다.')
        self.parts=proposed;changed=self.ensure_eye();self.output=None;return changed
    def set_code(self,text):self.identity['playerCode']=text;self.code_mode='manual';self.output=None
    def randomize_code(self):self.identity['playerCode']=random_player_code(self.identity['playerCode']);self.code_mode='random';self.issued=None;self.output=None;return self.identity['playerCode']
    def settings(self):return {'name':self.name,'likes':self.likes,'parts':dict(self.parts),'profileId':self.identity['profileId']}
    def save_settings(self):
        self.settings_path.parent.mkdir(parents=True,exist_ok=True);atomic_json(self.settings_path,self.settings(),ensure_ascii=False)
    def current_profile(self):
        result=dict(name=self.name,nickname=self.name,likes=self.likes,appearance=self.catalog.appearance(self.parts),parts=dict(self.parts),
                    selected={category:copy.deepcopy(self.catalog.row(self.parts,category)) for category,_ in CATEGORIES},
                    identity=dict(self.identity),playerCode=self.identity['playerCode'],uid=None,raw=None)
        if self.output:result.update(raw=self.output['raw'],uid=self.output['profile']['uid'],path=self.output['path'])
        return result
    def _default_aux(self):
        result=[]
        for index,item in enumerate(self.catalog.default_outfit):
            if not item:result.append(0);continue
            if self.outfit_catalog is None:result.append(0);continue
            record=self.outfit_catalog.items.get(item,{})
            expected=('head','top','bottom','shoes')[index]
            if record.get('category')!=expected or record.get('aux')!=0:raise ValueError('기본 레슨 코디의 데이터 버전이 맞지 않습니다.')
            result.append(0)
        return result
    def generate(self):
        validate_name(self.name);_integer(self.likes,MAX_NUMBER,'좋아요');code=normalize_player_code(self.identity['playerCode'])
        if self.code_mode=='random' and self.issued==code:code=random_player_code(code)
        raw=build_payload(name=self.name,appearance=self.catalog.appearance(self.parts),outfit=self.catalog.default_outfit,aux=self._default_aux(),profileId=self.identity['profileId'],playerCode=code,likes=self.likes)
        image=render_qr(raw);folder=self.root/'data/generated/mychara';folder.mkdir(parents=True,exist_ok=True)
        path=folder/('mychara_'+uuid.uuid4().hex+'.png');buffer=io.BytesIO();image.save(buffer,'PNG');atomic_bytes(path,buffer.getvalue())
        checked=decode_ticket(raw)
        if checked['uid']!=code or checked['profile_id']!=self.identity['profileId']:raise ValueError('생성한 식별 정보를 확인하지 못했습니다.')
        self.identity['playerCode']=code;self.issued=code;self.save_settings()
        self.output=dict(path=str(path),raw=raw,profile=checked);return self.output

def render_qr(raw):
    import numpy as np
    import zxingcpp
    from PIL import Image
    if decode_ticket(raw)['kind']!='my':raise ValueError('로그인용 마이티켓이 아닙니다.')
    barcode=zxingcpp.create_barcode(bytes(raw),zxingcpp.BarcodeFormat.QRCode,ec_level='M',eci=0,version=7)
    image=Image.fromarray(np.asarray(barcode.to_image(scale=12,add_quiet_zones=True))).convert('RGB')
    matches=zxingcpp.read_barcodes(image,formats=zxingcpp.BarcodeFormat.QRCode,try_invert=True)
    if image.size!=(636,636) or bytes(raw) not in [bytes(item.bytes) for item in matches if item.valid]:raise ValueError('생성된 PNG의 QR 검증에 실패했습니다.')
    return image
