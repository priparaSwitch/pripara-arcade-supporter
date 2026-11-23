"""Saved friend ticket preview; loading the QR always needs a user click."""
from PySide6.QtCore import Qt,Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QLabel,QPushButton

class FriendPreview(QDialog):
    load_qr=Signal(str)
    def __init__(self,path,parent=None,*,title='우정티켓 생성 완료',message='우정티켓을 저장했습니다.'):
        super().__init__(parent);self.setWindowTitle(title);self.path=str(path)
        layout=QVBoxLayout(self);image=QLabel();image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        image.setPixmap(QPixmap(self.path).scaled(756,508,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation));layout.addWidget(image)
        label=QLabel(message);label.setWordWrap(True);layout.addWidget(label)
        row=QHBoxLayout();layout.addLayout(row)
        load=QPushButton('QR 리더에 불러오기');load.clicked.connect(lambda:self.load_qr.emit(self.path));row.addWidget(load)
        close=QPushButton('닫기');close.clicked.connect(self.accept);row.addWidget(close)
