"""Local KR friendship-ticket conversion with externally supplied key material.

No encryption keys or personal fixtures are included. Import and construction
work without cryptography or a key file. The optional legacy decoder is loaded
only when a user explicitly converts a KR image.
"""
from pathlib import Path
from functools import reduce
import io,json,operator,sys,uuid
from protocol_delivery import read_radix255,_write_radix255
from ticket_codec import pack_body,decode_ticket

ALPHABET='0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:'
MAX_PIXELS=20_000_000
MAX_BYTES=64*1024*1024

class ConversionError(ValueError):pass
class MissingKeyMaterial(ConversionError):pass

def load_key_material(path):
    """Read an explicitly configured, runtime-only decoder file; never log it."""
    path=Path(path)
    try:
        if not 0<path.stat().st_size<=16384:raise ValueError()
        value=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value,dict):raise ValueError()
        if value.get('format')!='pripara-kr-decoder' or value.get('version')!=1:raise ValueError()
        keys=[bytes.fromhex(value[field]) for field in ('start_key_hex','end_key_hex')]
        if any(len(key) not in (16,24,32) for key in keys):raise ValueError()
        return tuple(keys)
    except FileNotFoundError as exc:raise MissingKeyMaterial('한국판 변환 자료가 필요합니다. 변환 자료 파일을 선택해 주세요.') from exc
    except (OSError,ValueError,KeyError,TypeError) as exc:raise MissingKeyMaterial('한국판 변환 자료 파일을 확인해 주세요.') from exc

def _crypt(data,key,*,decrypt=True):
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher,modes
        from cryptography.hazmat.decrepit.ciphers.algorithms import Camellia
    except ImportError as exc:raise ConversionError('한국판 변환 구성 요소가 없습니다. 변환 기능이 포함된 앱을 사용해 주세요.') from exc
    context=Cipher(Camellia(key),modes.ECB())
    transform=context.decryptor() if decrypt else context.encryptor()
    return transform.update(data)+transform.finalize()

def parse_kr(raw,keys):
    if not isinstance(raw,(bytes,bytearray,memoryview)) or len(raw)!=122:raise ConversionError('지원하는 한국판 우정티켓 QR이 아닙니다.')
    data=bytearray(raw);count=len(data)//16*16;remainder=len(data)%16
    data[remainder:remainder+count]=_crypt(bytes(data[remainder:remainder+count]),keys[1])
    data[:count]=_crypt(bytes(data[:count]),keys[0])
    if reduce(operator.xor,data,0):raise ConversionError('QR 검증에 실패했습니다. 올바른 변환 자료와 선명한 원본 이미지를 확인해 주세요.')
    body=bytes(data[:-1])
    if body[:3] not in (b'K21',b'K31') or body[46:48]!=b'LS':raise ConversionError('아직 지원하지 않는 한국판 티켓 구조입니다.')
    try:
        for offset,width in ((3,3),(6,3),(9,1),(10,1),(11,3),(14,3),(17,3),(20,3),(48,9)):
            read_radix255(body,offset,width)
        codes=[ALPHABET.index(chr(body[offset]))*45+ALPHABET.index(chr(body[offset+1])) for offset in range(101,113,2)]
    except ValueError as exc:raise ConversionError('QR의 캐릭터 정보를 확인하지 못했습니다.') from exc
    return dict(body=body,codes=codes,source_format=body[:3].decode('ascii'))

def convert_kr(ticket):
    """Preserve proven source identifiers, rank, nine parts and four items/aux.

    KR total likes have not been identified in this format. The HK unused-like
    field is therefore zero, not a claim that the original account has 0 likes.
    Rank remains the independently decoded original field.
    """
    source=ticket['body'];body=bytearray([1]*121);body[:4]=b'UI\x04\x04'
    body[4:16]=source[11:23]
    for source_offset,target_offset in ((3,16),(6,20)):_write_radix255(body,target_offset,4,read_radix255(source,source_offset,3))
    body[24]=source[10];body[25]=source[9];body[66:75]=source[48:57];body[58]=94
    for index,code in enumerate(ticket['codes']):_write_radix255(body,105+index*2,2,code)
    raw=pack_body(body);profile=decode_ticket(raw)
    profile.update(source_format=ticket['source_format'],likes_recovered=False,
        conversion_note='원래 등급을 유지했습니다. 한국판의 좋아요 총합은 확인되지 않은 형식입니다.')
    return raw,profile

def validate_hk_support(profile,catalog):
    """Reject unsupported native parts instead of substituting default parts."""
    root=Path(getattr(sys,'_MEIPASS',Path(__file__).parent))/'ticket_assets/parts.json'
    try:parts=json.loads(root.read_text(encoding='utf-8'))['parts']
    except (OSError,ValueError,KeyError,TypeError) as exc:raise ConversionError('홍콩판 파츠 자료를 불러오지 못했습니다.') from exc
    values=profile['appearance'];labels=('눈매','헤어스타일','헤어컬러','피부색','아이컬러','안경')
    categories=('face','hair','hair_color','skin','eye_color','glasses')
    for index,(label,category) in enumerate(zip(labels,categories)):
        row=parts.get(category,{}).get(str(values[index]),{})
        if row.get('supported') is not True:raise ConversionError(label+' 파츠가 홍콩판에 없어 변환할 수 없습니다.')
    eye=parts['eye_color'][str(values[4])]
    if eye.get('faceIds') is not None and values[0] not in eye['faceIds']:raise ConversionError('홍콩판에서 함께 사용할 수 없는 눈매와 아이컬러입니다.')
    if any(values[6:]):raise ConversionError('아직 지원하지 않는 원포인트 파츠가 들어 있습니다.')
    if profile['rank']>38:raise ConversionError('홍콩판에서 지원하지 않는 캐릭터 등급입니다.')
    if catalog is None:raise ConversionError('홍콩판 코디 목록이 필요합니다.')
    for slot in ('top','bottom','shoes','hair'):
        item=profile['items'][slot];expected='head' if slot=='hair' else slot
        record=catalog.items.get(item,{})
        if not item and slot in ('hair','bottom'):continue
        if record.get('category')!=expected:raise ConversionError('코디가 홍콩판 목록에 없거나 부위가 맞지 않습니다.')
    dress=catalog.items.get(profile['items']['top'],{}).get('isDress',False)
    if bool(profile['items']['bottom'])==bool(dress):raise ConversionError('원피스와 하의의 구성 정보가 맞지 않습니다.')

def read_kr_image(path,keys):
    from PIL import Image,ImageOps,UnidentifiedImageError
    import zxingcpp
    path=Path(path)
    try:
        if not 0<path.stat().st_size<=MAX_BYTES:raise ConversionError('이미지 파일의 크기를 확인해 주세요.')
        with Image.open(path) as opened:
            if opened.width*opened.height>MAX_PIXELS:raise ConversionError('2천만 화소 이하의 이미지를 선택해 주세요.')
            image=ImageOps.exif_transpose(opened).convert('RGB');image.thumbnail((2800,2800),Image.Resampling.LANCZOS)
    except (OSError,ValueError,UnidentifiedImageError) as exc:
        if isinstance(exc,ConversionError):raise
        raise ConversionError('이미지 파일을 읽지 못했습니다.') from exc
    found={};had_qr=False;last=None
    attempts=[image]
    if max(image.size)<700:attempts.append(image.resize((image.width*3,image.height*3),Image.Resampling.NEAREST))
    for image in attempts:
        for barcode in zxingcpp.read_barcodes(image,formats=zxingcpp.BarcodeFormat.QRCode,try_invert=True):
            if not barcode.valid:continue
            had_qr=True
            try:found[bytes(barcode.bytes)]=parse_kr(bytes(barcode.bytes),keys)
            except ConversionError as exc:last=exc
        if found:break
    if len(found)>1:raise ConversionError('서로 다른 QR이 여러 개 있습니다. 변환할 우정티켓 하나만 선택해 주세요.')
    if not found:
        if had_qr:raise last or ConversionError('지원하는 한국판 우정티켓 QR이 아닙니다.')
        raise ConversionError('QR을 찾지 못했습니다. 코드 전체가 선명한 사진을 선택해 주세요.')
    return next(iter(found.values()))

class ConversionService:
    def __init__(self,root,store,catalog,*,key_path=None,directory=None):
        self.root=Path(root);self.store=store;self.catalog=catalog
        self.key_path=Path(key_path) if key_path else self.root/'data/private/kr_decoder.json'
        self.directory=Path(directory) if directory else self.root/'data/generated/converted'
    def key_status(self):
        try:
            load_key_material(self.key_path)
            try:
                from cryptography.hazmat.primitives.ciphers import Cipher,modes
                from cryptography.hazmat.decrepit.ciphers.algorithms import Camellia
            except ImportError:return dict(available=False,reason='한국판 변환 구성 요소가 없습니다. 변환 기능이 포함된 앱을 사용해 주세요.')
            return dict(available=True,reason='한국판 우정티켓 이미지를 선택하면 자동으로 변환하고 보관합니다.')
        except MissingKeyMaterial as exc:return dict(available=False,reason=str(exc))
    def set_key_path(self,path):self.key_path=Path(path)
    def convert_file(self,path):
        keys=load_key_material(self.key_path);ticket=read_kr_image(path,keys);raw,profile=convert_kr(ticket)
        validate_hk_support(profile,self.catalog)
        from friend_render import render_friend
        from ticket_capture import publish_new
        name='변환한 우정티켓';outfit={'name':name}
        image=render_friend(raw,profile,outfit,self.catalog);buffer=io.BytesIO();image.save(buffer,'PNG')
        self.directory.mkdir(parents=True,exist_ok=True)
        output=self.directory/('hk_friend_'+uuid.uuid4().hex+'.png');publish_new(output,buffer.getvalue())
        record=self.store.register_generated(output,raw=raw,kind='tomo')
        return dict(path=str(output),raw=raw,profile=profile,record=record)
