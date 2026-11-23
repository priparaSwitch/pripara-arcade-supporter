"""Generate a native HK friend QR while retaining a known user's profile."""
from pathlib import Path
import io,uuid
from datetime import datetime
from PIL.PngImagePlugin import PngInfo
from ticket_codec import build_friend,decode_ticket,pack_body
from rank_service import correct_missing_rank
from friend_render import render_friend
from ticket_capture import publish_new

class FriendService:
    def __init__(self,directory,store,catalog):
        self.directory=Path(directory);self.store=store;self.catalog=catalog
    def create(self,profile,outfit):
        if not isinstance(profile,dict) or profile.get('can_generate') is not True:
            raise ValueError('먼저 읽을 수 있는 사용자 티켓을 등록해 주세요.')
        raw=build_friend(profile,outfit,catalog=self.catalog)
        decoded=decode_ticket(raw)
        body=bytearray(decoded['body'])
        if correct_missing_rank(body,decoded['likes']):
            raw=pack_body(body);decoded=decode_ticket(raw)
        original=decode_ticket(profile['raw'])
        if decoded['uid']!=original['uid'] or decoded['nickname']!=original['nickname'] or decoded['appearance']!=original['appearance']:
            raise ValueError('생성 중 사용자 정보가 달라졌습니다.')
        image=render_friend(raw,decoded,outfit,self.catalog)
        folder=self.directory/'generated'/'tomo'/(decoded.get('uid') or 'unknown')
        folder.mkdir(parents=True,exist_ok=True)
        path=folder/('friend_'+uuid.uuid4().hex+'.png')
        metadata=PngInfo();metadata.add_text('Creation Time',datetime.now().astimezone().isoformat(timespec='seconds'))
        buffer=io.BytesIO();image.save(buffer,'PNG',pnginfo=metadata);publish_new(path,buffer.getvalue())
        record=self.store.register_generated(path,raw=raw,kind='tomo')
        return dict(path=str(path),raw=raw,profile=decoded,record=record)

