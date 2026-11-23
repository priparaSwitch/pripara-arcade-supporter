"""Key-free, explicit conversion of a friend profile into a login QR."""
from pathlib import Path
import io,uuid
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog,QVBoxLayout,QFormLayout,QLabel,QLineEdit,QPushButton,QHBoxLayout
from ticket_codec import decode_ticket,build_my
from character_creator import parse_likes,render_qr
from ticket_capture import publish_new

class MyFromFriendService:
    def __init__(self,root,store,catalog=None,*,directory=None):
        self.root=Path(root);self.store=store;self.catalog=catalog
        self.directory=Path(directory) if directory else self.root/'data/generated/friend_to_my'
    def create(self,profile,likes):
        raw=build_my(profile,likes);checked=decode_ticket(raw)
        image=render_qr(raw);buffer=io.BytesIO();image.save(buffer,'PNG')
        self.directory.mkdir(parents=True,exist_ok=True)
        path=self.directory/('my_from_friend_'+uuid.uuid4().hex+'.png');publish_new(path,buffer.getvalue())
        record=self.store.register_generated(path,raw=raw,kind='my')
        original=decode_ticket(profile.get('raw',profile.get('template')) if isinstance(profile,dict) else profile)
        return dict(path=str(path),raw=raw,profile=checked,record=record,
                    source_uid=original['uid'],target_uid=checked['uid'],identity_changed=original['uid']!=checked['uid'])

class FriendToMyDialog(QDialog):
    def __init__(self,root,profile,store,catalog=None,parent=None,*,portraits=None,directory=None):
        super().__init__(parent);self.setWindowTitle('이 캐릭터로 마이티켓 만들기')
        self.created=None;self.profile=profile;self.service=MyFromFriendService(root,store,catalog,directory=directory)
        layout=QVBoxLayout(self);self.face=QLabel();self.face.setAlignment(Qt.AlignmentFlag.AlignCenter);layout.addWidget(self.face)
        info=QFormLayout();layout.addLayout(info)
        try:
            checked=decode_ticket(profile.get('raw',profile.get('template')))
            if checked['kind']!='tomo':raise ValueError('우정티켓을 선택해 주세요.')
            self.profile=checked
            name=QLabel(checked['nickname'] or '이름 없음');name.setTextFormat(Qt.TextFormat.PlainText)
            info.addRow('닉네임',name);info.addRow('유저 ID',QLabel(checked['uid'] or '미확인'))
            self.target_uid=QLabel(checked['player_code']);info.addRow('새 마이티켓 ID',self.target_uid)
            self.likes_input=QLineEdit(str(checked['likes'] or 20_000_000));self.likes_input.setObjectName('friendMyLikes')
            self.likes_input.setMaxLength(10);info.addRow('좋아요 ♥',self.likes_input)
            if portraits:
                try:
                    path=portraits.request(checked)
                    if path:self.face.setPixmap(QPixmap(str(path)).scaled(128,128,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))
                except (OSError,ValueError,KeyError,TypeError):pass
            valid=True
        except (OSError,ValueError,KeyError,TypeError):
            self.likes_input=QLineEdit('20000000');info.addRow('좋아요 ♥',self.likes_input);valid=False
        text='선택한 우정티켓의 닉네임·외형·코디를 유지하고, 입력한 좋아요에 맞는 등급으로 마이티켓을 만듭니다.'
        if valid and checked['manager']:
            text+='\n게임의 로그인 형식에서는 우정티켓의 ID 구분값을 유지할 수 없어, 위에 표시된 새 마이티켓 ID로 생성됩니다.'
        note=QLabel(text);note.setObjectName('friendMyIdentityNotice');note.setWordWrap(True);layout.addWidget(note)
        self.error=QLabel('' if valid else '우정티켓 정보를 확인하지 못했습니다.');self.error.setWordWrap(True);self.error.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.error)
        buttons=QHBoxLayout();self.generate_button=QPushButton('마이티켓 생성하기');self.generate_button.setEnabled(valid)
        self.cancel_button=QPushButton('취소');buttons.addWidget(self.generate_button);buttons.addWidget(self.cancel_button);layout.addLayout(buttons)
        self.generate_button.clicked.connect(self.accept);self.cancel_button.clicked.connect(self.reject);self.resize(430,360)
    def accept(self):
        try:self.created=self.service.create(self.profile,parse_likes(self.likes_input.text()))
        except ValueError as exc:
            self.error.setText(str(exc));return
        except (OSError,KeyError,TypeError):
            self.error.setText('마이티켓을 만들지 못했습니다. 좋아요와 원본 우정티켓 정보를 확인해 주세요.');return
        super().accept()

FromFriendDialog=FriendToMyDialog
