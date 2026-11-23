"""Local KR-to-HK friend conversion; choosing a photo never transmits it."""
from pathlib import Path
from PySide6.QtCore import Qt,Signal,QThread
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QPushButton,QLabel,QFileDialog,QApplication
from conversion_service import ConversionService,ConversionError

class _ConversionWorker(QThread):
    completed=Signal(dict);failed=Signal(str)
    def __init__(self,service,path,parent=None):super().__init__(parent);self.service=service;self.path=path
    def run(self):
        try:self.completed.emit(self.service.convert_file(self.path))
        except ConversionError as exc:self.failed.emit(str(exc))
        except Exception:self.failed.emit('티켓을 변환하지 못했습니다. 이미지와 변환 자료를 확인해 주세요.')

class ConverterWidget(QWidget):
    converted=Signal(dict)
    load_qr=Signal(str)
    def __init__(self,root,store,catalog=None,parent=None,*,directory=None,key_path=None):
        super().__init__(parent);self.service=ConversionService(root,store,catalog,directory=directory,key_path=key_path)
        self.output=None;self.worker=None;self.setAcceptDrops(True)
        layout=QVBoxLayout(self);title=QLabel('한국판 우정티켓 → 홍콩판 우정티켓');title.setStyleSheet('font-size:20px;font-weight:700');layout.addWidget(title)
        description=QLabel('한국판 우정티켓 이미지를 선택하거나 이곳으로 끌어 놓으세요. 변환한 티켓은 우정티켓 보관함에 자동으로 저장됩니다.');description.setWordWrap(True);layout.addWidget(description)
        controls=QHBoxLayout();self.open_button=QPushButton('한국판 티켓 이미지 선택');self.key_button=QPushButton('변환 자료 선택');controls.addWidget(self.open_button);controls.addWidget(self.key_button);layout.addLayout(controls)
        self.material_status=QLabel();self.material_status.setWordWrap(True);self.material_status.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.material_status)
        self.preview=QLabel('티켓 이미지를 선택해 주세요.');self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter);self.preview.setMinimumHeight(310);layout.addWidget(self.preview,1)
        self.info=QLabel();self.info.setWordWrap(True);self.info.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.info)
        self.status=QLabel();self.status.setWordWrap(True);self.status.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.status)
        actions=QHBoxLayout();self.reader_button=QPushButton('QR 리더에 표시');self.save_button=QPushButton('PNG 저장');self.copy_button=QPushButton('이미지 복사')
        for button in (self.reader_button,self.save_button,self.copy_button):button.setEnabled(False);actions.addWidget(button)
        layout.addLayout(actions)
        self.open_button.clicked.connect(self.open_file);self.key_button.clicked.connect(self.choose_material)
        self.reader_button.clicked.connect(lambda:self.load_qr.emit(self.output['path']) if self.output else None)
        self.save_button.clicked.connect(self.save_image);self.copy_button.clicked.connect(self.copy_image);self.update_material()
    def update_material(self):self.material_status.setText(self.service.key_status()['reason'])
    def choose_material(self):
        path,_=QFileDialog.getOpenFileName(self,'한국판 변환 자료 선택','','변환 자료 (*.json)')
        if path:self.service.set_key_path(path);self.update_material()
    def open_file(self):
        path,_=QFileDialog.getOpenFileName(self,'한국판 우정티켓 이미지 선택','','이미지 (*.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff)')
        if path:self.convert_path(path)
    def convert_path(self,path):
        if self.worker and self.worker.isRunning():return
        self.open_button.setEnabled(False);self.key_button.setEnabled(False);self.status.setText('한국판 QR을 읽고 변환하는 중…')
        self.worker=_ConversionWorker(self.service,str(path),self);self.worker.completed.connect(self.finished)
        self.worker.failed.connect(self.failed);self.worker.finished.connect(self.worker_stopped);self.worker.start()
    def worker_stopped(self):self.open_button.setEnabled(True);self.key_button.setEnabled(True)
    def failed(self,message):self.status.setText(message)
    def finished(self,output):
        self.output=output;profile=output['profile'];picture=QPixmap(output['path'])
        self.preview.setPixmap(picture.scaled(700,470,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))
        self.info.setText((profile.get('nickname') or '이름 없음')+' · 유저 ID '+(profile.get('uid') or '미확인')+'\n원본 등급 '+str(profile['rank'])+'을 유지했습니다. 원본 좋아요는 이 형식에서 아직 확인되지 않아 복원하지 않습니다.')
        self.status.setText('홍콩판 우정티켓을 만들어 보관함에 저장했습니다.')
        for button in (self.reader_button,self.save_button,self.copy_button):button.setEnabled(True)
        self.converted.emit(output)
    def save_image(self):
        if not self.output:return
        path,_=QFileDialog.getSaveFileName(self,'홍콩판 우정티켓 PNG 저장','HK_friend.png','PNG 이미지 (*.png)')
        if path:
            try:
                from ipc_files import atomic_bytes
                atomic_bytes(Path(path),Path(self.output['path']).read_bytes());self.status.setText('PNG를 저장했습니다.')
            except OSError:self.status.setText('PNG를 저장하지 못했습니다.')
    def copy_image(self):
        if self.output:QApplication.clipboard().setPixmap(QPixmap(self.output['path']));self.status.setText('이미지를 복사했습니다.')
    def dragEnterEvent(self,event):
        urls=event.mimeData().urls()
        if len(urls)==1 and urls[0].isLocalFile():event.acceptProposedAction()
    def dropEvent(self,event):
        urls=event.mimeData().urls()
        if len(urls)==1 and urls[0].isLocalFile():self.convert_path(urls[0].toLocalFile());event.acceptProposedAction()
    def close(self):
        # A local decoder job is finite; never destroy its running QThread.
        if self.worker and self.worker.isRunning():self.worker.wait(40000)
        return super().close()
