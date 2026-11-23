"""A text-based 756x508 friend ticket with the native QR position."""
from pathlib import Path
import io,json,os,sys
import numpy as np
from PIL import Image,ImageDraw,ImageFont,ImageOps

SIZE=(756,508)
QR_ORIGIN=(110,73)
QR_SIDE=180

def _font(size,bold=False):
    directory=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'
    candidates=['malgunbd.ttf','malgun.ttf','gulim.ttc'] if bold else ['malgun.ttf','gulim.ttc']
    for name in candidates:
        path=directory/name
        if path.is_file():return ImageFont.truetype(str(path),size)
    return ImageFont.load_default(size=size)

def _parts():
    root=Path(getattr(sys,'_MEIPASS',Path(__file__).parent))/'ticket_assets/parts.json'
    try:return json.loads(root.read_text(encoding='utf-8')).get('parts',{})
    except (OSError,ValueError):return {}

def _wrap(draw,text,font,width):
    lines=[];line=''
    for char in str(text):
        if char=='\n' or (line and draw.textlength(line+char,font=font)>width):
            lines.append(line);line='' if char=='\n' else char
        else:line+=char
    if line or not lines:lines.append(line)
    return lines

def _text(draw,xy,text,size,width,max_lines=1,bold=False,fill='#30283d'):
    for chosen in range(size,11,-1):
        font=_font(chosen,bold);lines=_wrap(draw,text,font,width)
        if len(lines)<=max_lines:break
    lines=lines[:max_lines]
    if len(_wrap(draw,text,font,width))>max_lines:
        last=lines[-1]
        while last and draw.textlength(last+'…',font=font)>width:last=last[:-1]
        lines[-1]=last+'…'
    x,y=xy
    for line in lines:draw.text((x,y),line,font=font,fill=fill);y+=chosen+4
    return y

def render_friend(raw,profile,outfit,catalog=None):
    import zxingcpp
    from ticket_codec import decode_ticket
    checked=decode_ticket(raw)
    if checked['kind']!='tomo':raise ValueError('우정티켓 QR이 아닙니다.')
    picture=Image.new('RGB',SIZE,'#fcf8ff');draw=ImageDraw.Draw(picture)
    draw.rounded_rectangle((4,4,751,503),18,outline='#a65b9f',width=3)
    draw.rounded_rectangle((19,14,291,46),12,fill='#b13e87')
    _text(draw,(94,16),'우정티켓',22,180,bold=True,fill='white')
    # Native raw bytes need no ECI marker. Without this option the encoder's
    # binary ECI header can push121 bytes into version8. Fix version7/M and
    # verify the pixels below so every profile keeps the original45-module size.
    code=zxingcpp.create_barcode(bytes(raw),zxingcpp.BarcodeFormat.QRCode,ec_level='M',eci=0,version=7)
    matrix=Image.fromarray(np.asarray(code.to_image(scale=4,add_quiet_zones=False))).convert('L')
    if matrix.size!=(QR_SIDE,QR_SIDE):raise ValueError('지원하는 우정티켓 QR 크기가 아닙니다.')
    panel=(QR_ORIGIN[0]-16,QR_ORIGIN[1]-16,QR_ORIGIN[0]+QR_SIDE+16,QR_ORIGIN[1]+QR_SIDE+16)
    draw.rectangle(panel,fill='black');picture.paste(ImageOps.invert(matrix).convert('RGB'),QR_ORIGIN)
    _text(draw,(67,282),'QR 리더에서 읽어 주세요',16,240)
    uid=checked.get('uid') or '미확인'
    _text(draw,(58,311),'UID  '+uid,17,255,bold=True)
    _text(draw,(324,24),checked['nickname'] or '이름 미확인',34,403,bold=True,fill='#92296f')
    parts=_parts();values=checked['appearance']
    rows=[('눈매','face',0),('헤어스타일','hair',1),('헤어컬러','hair_color',2),
          ('아이컬러','eye_color',4),('피부색','skin',3),('안경','glasses',5)]
    for i,(label,key,index) in enumerate(rows):
        value=values[index];name=parts.get(key,{}).get(str(value),{}).get('name')
        _text(draw,(324,78+i*28),label+' · '+(name or '정보 없음 ('+str(value)+')'),17,405)
    point='없음' if not any(values[6:9]) else ' / '.join(str(x) for x in values[6:9])
    _text(draw,(324,246),'원포인트 · '+point,17,405)
    draw.line((324,279,729,279),fill='#e2ccdf',width=2)
    set_name=outfit.get('name') or '선택한 코디'
    _text(draw,(324,288),set_name,21,402,max_lines=2,bold=True,fill='#7f337b')
    for index,(slot,label) in enumerate([('top','상의·원피스'),('bottom','하의'),('shoes','슈즈'),('head','헤어 액세서리')]):
        item=checked['items'].get(slot,checked['items'].get('hair',0) if slot=='head' else 0)
        row=catalog.items.get(item,{}) if catalog else {}
        name=row.get('name') or ('없음' if not item else '아이템 '+str(item))
        _text(draw,(324,353+index*28),label+' · '+name,15,402)
        if item and catalog:
            try:
                data=catalog.art_bytes(item)
                if data:
                    with Image.open(io.BytesIO(data)) as thumb:
                        thumb=ImageOps.contain(thumb.convert('RGB'),(56,56))
                        picture.paste(thumb,(42+index*65,381))
            except (OSError,ValueError):pass
    _text(draw,(34,466),'닉네임과 외형은 선택한 사용자 정보로 유지됩니다.',14,680,fill='#806d83')
    # Verify the actual saved pixels, not just an encoder/decoder object.
    decoded=zxingcpp.read_barcodes(picture,formats=zxingcpp.BarcodeFormat.QRCode,try_invert=True)
    if bytes(raw) not in [bytes(x.bytes) for x in decoded if x.valid]:raise ValueError('생성 이미지의 QR 검증에 실패했습니다.')
    return picture

