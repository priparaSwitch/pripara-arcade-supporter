"""Single QR reader, outfit remote and local ticket library."""
import sys, time, json, threading, shutil, traceback, math
from pathlib import Path
import camera_output as pyvirtualcam
import numpy as np
from camera_broker import Broker,run_helper
from single_instance import InstanceLock,notify_primary,listen_for_activation,event
from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QShortcut,QKeySequence
from PySide6.QtWidgets import QApplication,QMainWindow,QWidget,QVBoxLayout,QHBoxLayout,QPushButton,QLabel,QTabWidget,QMessageBox,QCheckBox
from app import ROOT,Window,configure,atomic_json
from ticket_widgets import TicketWorker,settings
from ticket_browser import TicketBrowser
from ticket_store import TicketStore
from ticket_index import IndexWorker
from remote_catalog import Catalog
from remote_widget import RemoteWidget
from friend_dialog import FriendGenerationDialog
from friend_service import FriendService
from friend_preview import FriendPreview
from character_widget import CharacterWidget
from portrait_service import PortraitService
from converter_widget import ConverterWidget
from friend_to_my import FromFriendDialog

VERSION='2.3.1'
DEVICES={'lower':'PriPara QR Camera'}

class Stream(QThread):
    status=Signal(str);failed=Signal(str)
    def __init__(self,frame,device,broker,role):
        super().__init__();self._lock=threading.Lock();self._frame=frame;self._generation=1;self._force=False;self.device=device;self.broker=broker;self.role=role;self.stop_event=threading.Event();self.frames=0;self.last_frame_at=0;self.error=None;self.last_revision=None;self.refresh_count=0
    @property
    def frame(self):
        with self._lock:return self._frame
    @frame.setter
    def frame(self,frame):self.submit_frame(frame)
    def submit_frame(self,frame,force=False):
        with self._lock:self._frame=frame;self._generation+=1;self._force=force
    def snapshot(self):
        with self._lock:return self._generation,self._frame,self._force
    def run(self):
        try:
            h,w,_=self.frame.shape;sent=0;announced=False;black_until=None;publish_failed_at=None
            self.status.emit('송출 도우미 연결 중 · '+self.device)
            while not self.stop_event.is_set():
                generation,frame,force=self.snapshot()
                try:
                    if black_until is not None and time.monotonic()>=black_until:
                        self.last_revision=self.broker.request(self.role,self.device,frame);sent=generation;black_until=None
                    elif black_until is None and generation!=sent:
                        if force:
                            self.last_revision=self.broker.request(self.role,self.device,np.zeros_like(frame));black_until=time.monotonic()+.20;self.refresh_count+=1
                        else:self.last_revision=self.broker.request(self.role,self.device,frame);sent=generation
                    publish_failed_at=None
                except OSError:
                    if publish_failed_at is None:publish_failed_at=time.monotonic()
                    if time.monotonic()-publish_failed_at>5:raise
                producer=self.broker.status()
                if producer.get('stopped'):raise RuntimeError('송출 도우미가 종료되었습니다. 이미지 카메라 앱을 다시 실행해 주세요.')
                if producer.get('at') and time.time()-producer['at']>10:raise RuntimeError('송출 도우미의 응답이 멈췄습니다. 이미지 카메라 앱을 다시 실행해 주세요.')
                state=producer.get('channels',{}).get(self.role,{})
                self.frames=state.get('frames',0)
                if state.get('running'):
                    self.last_frame_at=time.time()
                    if not announced:self.status.emit(f'송출 중 · {self.device} · {w}×{h} / 30 fps');announced=True
                if state.get('error'):raise RuntimeError(state['error'])
                self.stop_event.wait(0.05)
        except Exception as e:self.error=str(e);self.failed.emit(str(e))
        finally:
            try:self.broker.request(self.role,self.device,self.frame,running=False)
            except OSError:pass
    def stop(self):self.stop_event.set();self.wait(3000)

class Channel(Window):
    def __init__(self,root,role,broker=None):
        self.role=role;self.device=DEVICES[role];self.broker=broker
        # Window.__init__ may restore an image and call this class's refresh.
        self._direct_ready=False;self._direct_decode_error=None;self._direct_count=0;self._reader_active=False
        self._direct_connection_state={};self._direct_producer_at=None;self._direct_mode_restored=False
        super().__init__(root)
        self.paste_shortcut.setEnabled(False);self.open_shortcut.setEnabled(False)
        if role=='lower':
            self.res.blockSignals(True);self.res.clear();self.res.addItem('640×480');self.res.blockSignals(False)
            self.res.setToolTip('원본 QR 리더 호환: 640×480 YUY2')
        self.setWindowFlags(Qt.WindowType.Widget)
        self.findChild(QLabel,'title').setText('QR 리더')
        self.status.setText('송출 대기 · '+self.device)
        if role=='lower':
            self.resend.setText('QR 다시 보내기')
            self.resend.setToolTip('선택한 티켓의 QR을 한 번 보냅니다. 게임이 아직 읽기를 요청하지 않았다면 요청할 때까지 대기합니다.')
            bar=QWidget(self);row=QHBoxLayout(bar);row.setContentsMargins(0,0,0,0)
            self.direct_enabled=QCheckBox('게임 직접 연결');self.direct_enabled.setChecked(True)
            self.direct_enabled.setToolTip('QR 이미지의 원래 데이터를 게임으로 보냅니다. 끄면 기존 가상카메라 방식으로 사용합니다.')
            row.addWidget(self.direct_enabled)
            self.direct_light=QLabel();self.direct_light.setObjectName('qrConnectionLight');self.direct_light.setFixedSize(20,20)
            self.direct_light.setAccessibleName('QR 리더 연결 상태');row.addWidget(self.direct_light)
            self.direct_status=QLabel('QR 이미지를 불러와 주세요.');self.direct_status.setWordWrap(True);row.addWidget(self.direct_status,1)
            self.centralWidget().layout().insertWidget(2,bar)
            self.direct_enabled.toggled.connect(self.direct_mode_changed)
            self._direct_ready=True;self.publish_direct_qr()
    def refresh(self,*args,force=False):
        super().refresh(*args,force=force)
        # Image selection and the explicit resend button use force=True.
        # Fit/mirror/preview changes must not repeatedly decode the same source.
        if self.role=='lower' and self._direct_ready and force:self.publish_direct_qr(send_requested=True)
    def direct_mode_changed(self,*_):
        self._direct_mode_restored=True
        if self.broker:
            try:self.broker.set_direct_mode(self.direct_enabled.isChecked())
            except (AttributeError,OSError,ValueError):self._direct_decode_error='직접 연결 설정을 전달하지 못했습니다.'
        self.publish_direct_qr()
    def publish_direct_qr(self,*,send_requested=False):
        enabled=self.direct_enabled.isChecked() and self._reader_active;codes=[];self._direct_decode_error=None
        if enabled and self.image is not None:
            try:
                from direct_reader import decode_image
                codes=decode_image(self.image)
            except Exception:
                # No raw QR or potentially identifying decoder text in status.
                self._direct_decode_error='QR을 읽지 못했습니다. 다른 이미지나 기존 카메라 방식을 사용해 주세요.'
        self._direct_count=len(codes)
        if self.broker:
            try:
                from ticket_item import captured_item
                item_id=captured_item(self.broker.root,self.entry,self.image) if enabled else None
                self.broker.request_qr(codes,time.time_ns(),enabled,send_requested=send_requested and enabled,item_id=item_id)
            except (OSError,ValueError,TypeError):
                self._direct_decode_error='게임 연결 도우미에 전달하지 못했습니다. QR 다시 보내기를 눌러 주세요.'
        self.update_direct_status()
    def update_direct_status(self,state=None,producer_at=None):
        if not self._direct_ready:return
        if state is not None:
            self._direct_connection_state=dict(state) if isinstance(state,dict) else {}
            self._direct_producer_at=producer_at
        state=self._direct_connection_state
        try:
            age=time.time()-float(self._direct_producer_at)
            fresh=math.isfinite(age) and -1<=age<3
        except (TypeError,ValueError,OverflowError):fresh=False
        if fresh and not self._direct_mode_restored and type(state.get('direct_mode')) is bool:
            self._direct_mode_restored=True
            self.direct_enabled.blockSignals(True);self.direct_enabled.setChecked(state['direct_mode']);self.direct_enabled.blockSignals(False)
            self.publish_direct_qr()
        color=state.get('traffic_light')
        label=state.get('traffic_label')
        if not fresh:
            color='red';label='연결 확인 실패 · 도우미 응답 없음'
        elif color not in ('green','orange','red') or not isinstance(label,str) or not label:
            color='red';label='연결 상태를 확인하지 못했습니다.'
        colors={'green':('#24a75b','#1a7842'),'orange':('#efa332','#b87513'),'red':('#d84d4d','#a83434')}
        fill,border=colors[color]
        self.direct_light.setStyleSheet('QLabel#qrConnectionLight{background-color:'+fill+';border:1px solid '+border+';border-radius:10px;}')
        self.direct_light.setProperty('connectionState',color)
        self.direct_light.setAccessibleDescription(label)
        kind={'direct':'직접 연결','legacy':'기존 QR 리더','none':'리더 연결 없음'}.get(state.get('connection_kind'),'연결 확인 중')
        tooltip=kind+' · '+label+'\n초록: 게임이 QR 읽기를 요청 중\n주황: 게임의 읽기 요청 대기\n빨강: 연결 실패 또는 상태 응답 없음'
        self.direct_light.setToolTip(tooltip);self.direct_status.setToolTip(tooltip)
        if not self._reader_active:detail='QR 리더 중지'
        elif not self.direct_enabled.isChecked():detail='직접 연결 꺼짐 · 기존 가상카메라 모드'
        elif self._direct_decode_error:detail=self._direct_decode_error
        elif self.image is None:detail='QR 이미지를 불러와 주세요.'
        elif self._direct_count==0:detail='이미지에서 QR을 찾지 못했습니다.'
        else:
            detail=state.get('message')
            if not isinstance(detail,str) or not detail:detail='QR '+str(self._direct_count)+'개 준비'
        self.direct_status.setText(label if detail==label else label+' · '+detail)
    def toggle_camera(self):
        if self.camera:
            self._reader_active=False;self.publish_direct_qr()
            self.camera.stop();self.camera=None;self.start.setText('QR 리더 시작');self.status.setText('송출 중지 · '+self.device);self.res.setEnabled(True)
        else:
            self.camera=Stream(self.output(),self.device,self.broker,self.role);self.camera.status.connect(self.status.setText);self.camera.failed.connect(self.camera_error)
            self._reader_active=True;self.publish_direct_qr()
            self.camera.start();self.start.setText('QR 리더 중지');self.res.setEnabled(False)
    def camera_error(self,message):
        self._reader_active=False;self.publish_direct_qr()
        if self.camera:self.camera.wait(1000);self.camera=None
        self.start.setText('가상 카메라 시작');self.res.setEnabled(True);self.status.setText('송출 실패 · '+self.device);self.error(message)

class SuiteWindow(QMainWindow):
    def __init__(self,root=ROOT,broker=None,*,background=True,portrait_service=None):
        super().__init__();self.root=Path(root);root=self.root;self.broker=broker
        self.setWindowTitle('프리파라HK 도우미 v'+VERSION)
        center=QWidget();self.setCentralWidget(center);layout=QVBoxLayout(center)
        top=QHBoxLayout();layout.addLayout(top)
        top.addWidget(QLabel('프리파라HK 도우미'),1)
        version=QLabel('v'+VERSION);version.setObjectName('suiteVersion');top.addWidget(version)
        start=QPushButton('QR 리더 시작');start.clicked.connect(self.start_reader);top.addWidget(start)
        stop=QPushButton('QR 리더 중지');stop.clicked.connect(self.stop_reader);top.addWidget(stop)
        self.tabs=QTabWidget();layout.addWidget(self.tabs);self.channels={}
        channel_root=root/'channels/lower'
        if not (channel_root/'data/library.json').exists() and (root/'data/library.json').exists():
            shutil.copytree(root/'data',channel_root/'data',dirs_exist_ok=True)
        channel=Channel(channel_root,'lower',broker);channel.start.setText('QR 리더 시작')
        self.channels['lower']=channel;self.tabs.addTab(channel,'QR 리더')
        self.catalog=Catalog(root)
        self.remote=RemoteWidget(self.catalog,broker,self);self.tabs.addTab(self.remote,'코디 리모컨')
        self.remote.create_friend.connect(lambda outfit:self.open_generation(outfit=outfit))
        self.store=TicketStore(root/'data/tickets.sqlite3',catalog=self.catalog)
        self.portraits=portrait_service if portrait_service is not None else (PortraitService(root,self) if background else None)
        self._creator_portrait_key=None
        self._last_auto_print=None
        self._portrait_refresh_timer=QTimer(self);self._portrait_refresh_timer.setSingleShot(True)
        self._portrait_refresh_timer.timeout.connect(self.refresh_portrait_galleries)
        self.directory=settings(root)[1];self.galleries={}
        for kind,label in [('my','마이티켓'),('tomo','우정티켓')]:
            gallery=TicketBrowser(root,kind,self.store,self.catalog,self);gallery.set_directory(self.directory)
            gallery.send_image.connect(self.show_ticket)
            gallery.generation_requested.connect(lambda profile:self.open_generation(profile=profile))
            gallery.refresh_requested.connect(self.refresh_index)
            gallery.trash_changed.connect(self.ticket_collection_changed)
            gallery.note_changed.connect(self.index_changed)
            gallery.make_my_requested.connect(self.open_friend_to_my)
            self.galleries[kind]=gallery;self.tabs.addTab(gallery,label)
        self.character=CharacterWidget(root,self.catalog,self);self.tabs.addTab(self.character,'캐릭터 생성')
        self.character.load_qr.connect(self.show_ticket)
        self.character.profile_created.connect(self.character_created)
        self.character.portrait_requested.connect(self.preview_character)
        self.converter=ConverterWidget(root,self.store,self.catalog,self);self.tabs.addTab(self.converter,'우정티켓 변환기')
        self.converter.converted.connect(self.ticket_collection_changed);self.converter.load_qr.connect(self.show_ticket)
        if self.portraits:
            self.portraits.ready.connect(self.portrait_ready);self.portraits.failed.connect(self.portrait_failed)
            status_changed=getattr(self.portraits,'status_changed',None)
            if status_changed is not None:status_changed.connect(self.portrait_status_changed)
            for gallery in self.galleries.values():
                gallery.set_portrait_provider(self.portraits.request,status=getattr(self.portraits,'status',None),retry=getattr(self.portraits,'retry',None))
        self.tabs.setCurrentIndex(0)
        self.paste_shortcut=QShortcut(QKeySequence.StandardKey.Paste,self,activated=lambda:self.active_channel().paste())
        self.open_shortcut=QShortcut(QKeySequence.StandardKey.Open,self,activated=lambda:self.active_channel().open_file())
        self.ticket_status=QLabel('티켓 감시 시작 중');self.ticket_status.setWordWrap(True);layout.addWidget(self.ticket_status)
        self.ticket_state={};self.ticket_worker=None;self.index_worker=None;self.background=background
        if background:
            self.index_worker=IndexWorker(self.store,self.directory);self.index_worker.changed.connect(self.index_changed)
            self.index_worker.failed.connect(lambda message:self.ticket_status.setText(message));self.index_worker.start()
            self.ticket_worker=TicketWorker(root);self.ticket_worker.status.connect(self.update_ticket_status)
            self.ticket_worker.saved.connect(self.ticket_saved);self.ticket_worker.start()
        else:self.ticket_status.setText('티켓 보관함 준비됨')
        self.timer=QTimer(self);self.timer.timeout.connect(self.write_status)
        if background:self.timer.start(1000)
        self.schedule_portraits()
        self.resize(1240,900)
    def active_channel(self):return self.channels['lower']
    def show_ticket(self,path,role='lower'):
        channel=self.channels['lower'];channel.read_path(path);self.tabs.setCurrentWidget(channel)
    def ticket_saved(self,entry):
        self.refresh_index();self.ticket_status.setText('티켓 저장 완료 · '+Path(entry['full']).name)
        # A newly completed print is an explicit automatic image selection.
        # Repeated index updates and replayed capture events never reselect it.
        path=Path(entry.get('my',''));stamp=entry.get('stamp')
        try:time.strptime(stamp,'%Y-%m-%d_%H%M%S')
        except (ValueError,TypeError):return
        if not path.is_file() or self._last_auto_print is not None and stamp<=self._last_auto_print:return
        self._last_auto_print=stamp;self.show_ticket(str(path))
        self.ticket_status.setText('새 마이티켓을 QR 리더에 준비했습니다.')
    def refresh_index(self):
        if self.index_worker:self.index_worker.request_refresh()
        else:self.index_changed({})
    def index_changed(self,result):
        self.schedule_portraits()
        for gallery in self.galleries.values():gallery.reload()
    def schedule_portraits(self):
        """Schedule every known active user, including hidden library tabs.

        These requests only create local images. They never load a QR into the
        reader or arm a transmission, and the service deduplicates appearances.
        """
        if not self.portraits:return
        try:users=self.store.users()
        except Exception:return
        for profile in users:
            try:self.portraits.request(dict(profile))
            except Exception:pass  # Each gallery exposes its safe status category.
    def portrait_status_changed(self,*_):
        # Coalesce worker progress without re-entering reload during request().
        if not self._portrait_refresh_timer.isActive():self._portrait_refresh_timer.start(0)
    def refresh_portrait_galleries(self):
        for gallery in self.galleries.values():gallery.reload()
    def ticket_collection_changed(self,result):
        self.index_changed(result);self.refresh_index()
    def character_created(self,profile):
        try:
            self.store.register_generated(profile['path'],raw=profile['raw'],kind='my')
            self.index_changed({})
        except (OSError,ValueError,KeyError,TypeError):
            self.character.status_label.setText('QR은 생성됐지만 티켓 보관함에 등록하지 못했어요. PNG 저장은 사용할 수 있어요.')
    def preview_character(self,profile):
        if not self.portraits:
            self.character.set_portrait_status('얼굴 미리보기를 사용할 수 없습니다.');return
        try:
            self._creator_portrait_key=self.portraits.key(profile)
            path=self.portraits.request(profile)
            state=self.portraits.status(profile) if hasattr(self.portraits,'status') else {}
            if not path and state.get('state') in ('failed','retrying') and hasattr(self.portraits,'retry'):
                path=self.portraits.retry(profile);state=self.portraits.status(profile)
            if path:self.character.set_portrait_image(path)
            elif not self.portraits.available:self.character.set_portrait_status(self.portraits.reason)
            elif state.get('state') in ('failed','unavailable'):self.character.set_portrait_status(state.get('reason') or '미리보기를 만들지 못했습니다.')
            else:self.character.set_portrait_status('캐릭터 미리보기 생성 중…')
        except (OSError,ValueError,KeyError,TypeError):self.character.set_portrait_status('선택한 파츠의 미리보기 자료가 없습니다.')
    def portrait_ready(self,key,path):
        self.refresh_portrait_galleries()
        if key==self._creator_portrait_key:
            try:
                if self.portraits.key(self.character.current_profile())==key:self.character.set_portrait_image(path)
            except (ValueError,KeyError,TypeError):pass
    def portrait_failed(self,key,message):
        self.refresh_portrait_galleries()
        if key==self._creator_portrait_key:
            try:
                if self.portraits.key(self.character.current_profile())==key:self.character.set_portrait_status(message)
            except (ValueError,KeyError,TypeError):pass
    def open_generation(self,*,profile=None,outfit=None):
        dialog=FriendGenerationDialog(self.store,self.catalog,self,profile=profile,outfit=outfit,portrait_service=self.portraits)
        if dialog.exec()!=dialog.DialogCode.Accepted or not dialog.selection:return
        try:
            selection=dialog.selection
            created=FriendService(self.directory,self.store,self.catalog).create(selection['profile'],selection['outfit'])
        except Exception as error:
            QMessageBox.warning(self,'우정티켓 생성',str(error));return
        self.index_changed({});self.refresh_index()
        preview=FriendPreview(created['path'],self)
        preview.load_qr.connect(lambda path:self.show_ticket(path));preview.exec()
    def open_friend_to_my(self,profile):
        dialog=FromFriendDialog(self.root,profile,self.store,self.catalog,self,portraits=self.portraits)
        if dialog.exec()!=dialog.DialogCode.Accepted or not dialog.created:return
        self.index_changed({});self.refresh_index()
        preview=FriendPreview(dialog.created['path'],self,title='마이티켓 생성 완료',message='선택한 우정티켓의 캐릭터로 마이티켓을 만들었습니다.')
        preview.load_qr.connect(self.show_ticket);preview.exec()
    def update_ticket_status(self,state):
        self.ticket_state=state;output=state.get('output_dir')
        if output and Path(output)!=self.directory:
            self.directory=Path(output)
            for gallery in self.galleries.values():gallery.set_directory(self.directory)
            if self.index_worker:self.index_worker.set_directory(self.directory)
        if state.get('error'):text='티켓 처리 대기 · '+state['error']
        elif not state.get('running'):text='티켓 감시 폴더 대기 · '+state.get('input_dir','')
        else:text='BMP 자동 저장 · '+state.get('input_dir','')+' → '+state.get('output_dir','')
        self.ticket_status.setText(text)
    def start_reader(self):
        channel=self.channels['lower']
        if not channel.camera:channel.toggle_camera()
    def stop_reader(self):
        channel=self.channels['lower']
        if channel.camera:channel.toggle_camera()
        self.write_status()
    def write_status(self):
        data={'timestamp':time.time(),'version':VERSION,'ui_pid':__import__('os').getpid(),'single_instance':True,'tickets':self.ticket_state,'channels':{},'producer':self.broker.status() if self.broker else {},'ui_elevated':bool(__import__('ctypes').windll.shell32.IsUserAnAdmin())}
        self.channels['lower'].update_direct_status(data['producer'].get('direct_qr',{}),data['producer'].get('at'))
        for role,c in self.channels.items():
            cam=c.camera
            data['channels'][role]={'device':c.device,'running':bool(cam and cam.isRunning()),'frames_sent':cam.frames if cam else 0,'last_frame_at':cam.last_frame_at if cam else None,'requested_revision':cam.last_revision if cam else None,'refresh_count':cam.refresh_count if cam else 0,'size':list(c.output().shape[:2][::-1]),'fit':c.mode.currentText(),'mirror':c.mirror.isChecked(),'rotation':c.rotation.currentIndex(),'error':cam.error if cam else None}
        try:atomic_json(self.root/'dual_status.json',data)
        except OSError:pass
    def closeEvent(self,event):
        self.timer.stop();self._portrait_refresh_timer.stop();self.remote.close();self.converter.close()
        self.character.save_settings()
        if self.portraits:self.portraits.close()
        for gallery in self.galleries.values():gallery.timer.stop()
        if self.ticket_worker:self.ticket_worker.stop()
        if self.index_worker:self.index_worker.stop()
        self.stop_reader();self.store.close();self.catalog.close()
        if self.broker:self.broker.close()
        event.accept()

# Retained as an import alias for existing command-line launchers.
DualWindow=SuiteWindow

def main():
    if len(sys.argv)==3 and sys.argv[1]=='--offline-self-test':
        from offline_check import run
        run(Path(sys.argv[2]));return
    if len(sys.argv)==3 and sys.argv[1]=='--offline-portrait-self-test':
        from offline_check import run_portrait
        run_portrait(Path(sys.argv[2]));return
    if len(sys.argv)==3 and sys.argv[1]=='--offline-portrait-suite-test':
        from offline_check import run_portrait_suite
        run_portrait_suite(Path(sys.argv[2]));return
    if len(sys.argv)==3 and sys.argv[1]=='--offline-conversion-self-test':
        from conversion_selftest import run
        run(Path(sys.argv[2]));return
    if len(sys.argv)==5 and sys.argv[1]=='--local-kr-conversion-check':
        from conversion_selftest import run_supplied
        run_supplied(Path(sys.argv[2]),Path(sys.argv[3]),Path(sys.argv[4]));return
    if len(sys.argv)>=3 and sys.argv[1]=='--stream-helper':
        run_helper(sys.argv[2],ROOT);return
    instance=InstanceLock('Editor')
    app=QApplication(sys.argv);configure(app)
    if not instance.primary:
        activated=notify_primary();event(ROOT,'duplicate_editor_blocked',activated=activated);return
    try:
        broker=Broker(ROOT);win=SuiteWindow(broker=broker);server=listen_for_activation(win,ROOT)
        event(ROOT,'editor_started');win.show();QTimer.singleShot(300,win.start_reader);sys.exit(app.exec())
    except Exception:
        (ROOT/'error.log').write_text(traceback.format_exc(),encoding='utf-8');QMessageBox.critical(None,'프리파라 프로그램 시작 오류',traceback.format_exc())
    finally:instance.close()

if __name__=='__main__':main()
