"""Validated, key-free HK app tickets and identity-preserving friend creation.

Only the 121-byte HK app format is supported here. Legacy encrypted tickets
remain unclassified; this module never embeds their encryption keys.
"""
from __future__ import annotations
import hashlib,json,sys
from pathlib import Path
from protocol_delivery import _SWAPS,read_radix255,_write_radix255

PLAYER_CODE_XOR=0x1D4C5E
MAX_MAKE_ID=255**3-1
SLOTS=('top','bottom','shoes','hair')
PART_KEYS=('face','hair_style','hair_color','skin','eye_color','glasses','extra1','extra2','extra3')

class TicketDecodeError(ValueError):
    """The supplied bytes are not a validated supported HK app ticket."""

def _family(data):
    selector=data[58]-2
    return (data[selector]-1)%10 if 92<=selector<=94 and data[selector] else 0

def _valid_body(body):
    return len(body)==121 and bytes(body[:3])==b'UI\x04' and body[3] in (3,4) and 0 not in body

def unpack_payload(raw):
    if not isinstance(raw,(bytes,bytearray,memoryview)):
        raise TicketDecodeError('지원하는 HK 앱 티켓 데이터가 아닙니다.')
    body=bytearray(raw)
    if len(body)!=121 or 0 in body:
        raise TicketDecodeError('지원하는 HK 앱 티켓은 121바이트입니다.')
    for x,y in reversed(_SWAPS[_family(body)]):body[x-1],body[y-1]=body[y-1],body[x-1]
    if not _valid_body(body) or body[119]!=sum(body[:60])%255+1 or body[120]!=sum(body[60:119])%255+1:
        raise TicketDecodeError('HK 앱 티켓의 형식이나 검증값이 맞지 않습니다.')
    return bytes(body)

def pack_body(body):
    if not isinstance(body,(bytes,bytearray,memoryview)) or not _valid_body(body):
        raise TicketDecodeError('지원하는 HK 앱 티켓 본문이 아닙니다.')
    output=bytearray(body)
    output[119]=sum(output[:60])%255+1;output[120]=sum(output[60:119])%255+1
    for x,y in _SWAPS[_family(output)]:output[x-1],output[y-1]=output[y-1],output[x-1]
    return bytes(output)

def _character_table():
    chars={0:'',412:'♪',413:'!',414:'?',415:'★',416:'☆'}
    chars.update((201+i,chr(65+i)) for i in range(26))
    chars.update((301+i,chr(97+i)) for i in range(26))
    chars.update((400+i,str(i)) for i in range(10))
    paths=[Path(__file__).with_name('ticket_assets')/'name_characters.json']
    if getattr(sys,'frozen',False):paths.insert(0,Path(sys._MEIPASS)/'ticket_assets/name_characters.json')
    for path in paths:
        try:
            source=json.loads(path.read_text(encoding='utf-8'))
            if isinstance(source,dict):
                chars.update({int(key):value for key,value in source.items() if str(key).isdigit() and isinstance(value,str)})
                break
        except (OSError,ValueError,TypeError):pass
    return chars

_CHARS=_character_table()

def decode_ticket(raw):
    """Use the ticket's printed code as the library UID, preserving raw identity.

    profile_id/internal_uid retain the separate original four-byte field.
    No identifier or original payload byte is changed by this presentation rule.
    Unknown printed codes are never fabricated or grouped by nickname.
    """
    source=bytes(raw) if isinstance(raw,(bytes,bytearray,memoryview)) else raw
    body=unpack_payload(source)
    num=lambda offset,width:read_radix255(body,offset,width)
    profile_id=num(16,4);make_id=num(20,4);manager=num(24,1)
    internal_uid=f'{profile_id:012d}' if profile_id else None
    normalized_make=min(make_id,MAX_MAKE_ID)
    player_number=normalized_make^PLAYER_CODE_XOR if normalized_make else 0
    player_code=f'{player_number:012d}'
    native_serial=f'{manager*10_000_000+player_number:012d}'
    uid=native_serial if make_id else None
    identity_key='ticket:'+uid if uid else ('profile:'+internal_uid if internal_uid else 'unknown:'+hashlib.sha256(source).hexdigest())
    codes=[num(105+i*2,2) for i in range(6)]
    nickname=''.join(_CHARS.get(code,'[문자 '+str(code)+']') for code in codes)
    items={slot:num(4+i*3,2) for i,slot in enumerate(SLOTS)}
    aux={slot:num(6+i*3,1) for i,slot in enumerate(SLOTS)}
    items['head']=items['hair'];aux['head']=aux['hair']
    appearance=[num(66+i,1) for i in range(9)]
    kind='my' if num(3,1)==2 else 'tomo'
    return dict(raw=source,template=source,body=body,uid=uid,identity_key=identity_key,
                profile_id=profile_id,internal_uid=internal_uid,make_id=make_id,manager=manager,
                player_code=player_code,native_serial=native_serial,ticket_code=uid,
                nickname=nickname,name_codes=codes,unknown_name_codes=sorted({c for c in codes if c not in _CHARS}),
                appearance=appearance,parts=dict(zip(PART_KEYS,appearance)),items=items,aux=aux,
                top_id=items['top'],qr_type=num(3,1),version=num(2,1),kind=kind,
                likes=num(29,4),rank=num(25,1),total_plays=num(26,3),rank_up_like_point=num(33,4),last_rank=num(43,1),can_generate=bool(profile_id or make_id),
                decoded=True,checksum_valid=True)

def _slot_map(value,label):
    if isinstance(value,dict):
        value=value.get('pieces',value)
        if not isinstance(value,dict):raise ValueError(label+' 형식이 올바르지 않습니다.')
        return {slot:value.get(slot,value.get('head',0) if slot=='hair' else 0) for slot in SLOTS}
    if isinstance(value,(list,tuple)) and len(value)==4:return dict(zip(SLOTS,value))
    raise ValueError(label+'는 상의·하의·슈즈·헤어 액세서리 4개 값이 필요합니다.')

def _integer(value,maximum,label):
    if type(value) is not int or not 0<=value<=maximum:raise ValueError(label+' 값의 범위가 맞지 않습니다.')
    return value

def build_friend(profile_or_raw,outfit,piece_aux=None,*,catalog=None):
    """Keep one user's original identity/appearance and replace only four pieces.

    The outfit order is top, bottom, shoes, hair/head. Fresh pieces require known
    native auxiliary metadata. An unchanged piece may preserve its source aux.
    No nickname, UID, appearance, progress, or reserved byte is invented.
    """
    if isinstance(profile_or_raw,dict):
        raw=profile_or_raw.get('raw',profile_or_raw.get('template'))
    else:raw=profile_or_raw
    original=decode_ticket(raw)
    if not original['can_generate']:raise ValueError('사용자 ID를 확인할 수 없는 티켓으로는 생성할 수 없습니다.')
    pieces=_slot_map(outfit,'코디')
    for slot in SLOTS:pieces[slot]=_integer(pieces[slot],255**2-1,'코디')
    if not any(pieces.values()):raise ValueError('비어 있는 코디로 우정티켓을 만들 수 없습니다.')
    explicit=None
    if piece_aux is not None:
        if isinstance(piece_aux,(list,tuple)):explicit=_slot_map(piece_aux,'코디 부가값')
        elif isinstance(piece_aux,dict):explicit=piece_aux
        else:raise ValueError('코디 부가값 형식이 올바르지 않습니다.')
    records=getattr(catalog,'items',{}) if catalog is not None else {}
    auxiliaries={}
    for slot,item in pieces.items():
        value=None
        if not item:value=0
        elif explicit is not None:
            for key in (slot,'head' if slot=='hair' else slot,item,str(item)):
                if key in explicit:value=explicit[key];break
        record=records.get(item) if isinstance(records,dict) else None
        if catalog is not None and item and not isinstance(record,dict):
            raise ValueError('코디 목록에 없는 아이템입니다: '+str(item))
        if value is None and isinstance(record,dict):value=record.get('aux')
        if isinstance(record,dict):
            category=record.get('category')
            expected='head' if slot=='hair' else slot
            if category and category!=expected:raise ValueError('아이템의 부위와 코디 슬롯이 맞지 않습니다.')
            if record.get('isDress') and slot=='top' and pieces['bottom']:
                raise ValueError('원피스와 하의를 함께 지정할 수 없습니다.')
        if value is None and original['items'][slot]==item:value=original['aux'][slot]
        if value is None:raise ValueError('아이템 부가값을 확인할 수 없습니다: '+str(item))
        auxiliaries[slot]=_integer(value,254,'코디 부가값')
    body=bytearray(original['body']);body[3]=4
    for i,slot in enumerate(SLOTS):
        _write_radix255(body,4+i*3,2,pieces[slot]);_write_radix255(body,6+i*3,1,auxiliaries[slot])
    from rank_service import correct_missing_rank
    correct_missing_rank(body,original['likes'])
    return pack_body(body)


def build_my(profile_or_raw,likes,*,edition=0):
    """Clone a friend appearance into the native login-ticket identity range.

    Native MyReader clears the friend display-manager unconditionally, so My
    uses manager0 explicitly. A nonzero source manager changes the printed UID;
    nickname, profile/make IDs, appearance, outfit and other fields survive.
    A zero play count is normalized to one because the native first-play path
    otherwise resets identity; positive history is retained unchanged.
    """
    from rank_service import apply_native_rank
    raw=profile_or_raw.get('raw',profile_or_raw.get('template')) if isinstance(profile_or_raw,dict) else profile_or_raw
    original=decode_ticket(raw)
    if original['kind']!='tomo':raise ValueError('우정티켓을 선택해 주세요.')
    if not original['make_id'] or not original['uid']:raise ValueError('로그인에 필요한 티켓 번호를 확인할 수 없습니다.')
    body=bytearray(original['body']);body[3]=3;_write_radix255(body,24,1,0)
    if not original['total_plays']:_write_radix255(body,26,3,1)
    apply_native_rank(body,likes,edition=edition)
    result=pack_body(body);checked=decode_ticket(result)
    if checked['manager']!=0 or checked['uid']!=original['player_code']:raise ValueError('마이티켓 번호를 확인하지 못했습니다.')
    for key in ('profile_id','make_id','nickname','appearance','items','aux'):
        if checked[key]!=original[key]:raise ValueError('변환 중 사용자 정보가 달라졌습니다.')
    return result
