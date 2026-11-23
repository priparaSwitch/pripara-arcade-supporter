"""Explicit offline bundle check using real hidden Qt and synthetic tickets.

A temporary profile and fake broker isolate all writes. Forbidden native game,
camera and background entry points fail closed. DLL access is version-only.
"""
from contextlib import ExitStack,contextmanager
import hashlib,json,os,sys,tempfile
from pathlib import Path
from unittest.mock import patch

@contextmanager
def _profile(root):
    names=('HOME','USERPROFILE','APPDATA','LOCALAPPDATA','TEMP','TMP','QT_QPA_PLATFORM')
    old={name:os.environ.get(name) for name in names};folder=root/'empty-profile';folder.mkdir()
    try:
        for name in names[:-1]:os.environ[name]=str(folder)
        os.environ['QT_QPA_PLATFORM']='offscreen';yield
    finally:
        for name,value in old.items():
            if value is None:os.environ.pop(name,None)
            else:os.environ[name]=value

def _body(kind,uid,outfit,catalog):
    from protocol_delivery import _write_radix255
    from ticket_codec import pack_body
    body=bytearray([1]*121);body[:4]=bytes([85,73,4,kind+1]);body[58]=94
    _write_radix255(body,16,4,uid);_write_radix255(body,20,4,100000+uid)
    _write_radix255(body,24,1,1);_write_radix255(body,25,1,4);_write_radix255(body,29,4,123456)
    _write_radix255(body,105,2,201)  # Synthetic nickname A.
    for i,value in enumerate((6,1,4,0,1,0,0,0,0)):_write_radix255(body,66+i,1,value)
    for index,slot in enumerate(('top','bottom','shoes','head')):
        identity=outfit['pieces'][slot];aux=catalog.items[identity]['aux'] if identity else 0
        _write_radix255(body,4+index*3,2,identity);_write_radix255(body,6+index*3,1,aux)
    return pack_body(body)

def _ui_check(root,result,check):
    import numpy as np
    import zxingcpp
    from PIL import Image
    from PySide6.QtWidgets import QApplication,QDialog,QPushButton
    import app_dual,camera_broker,camera_output,direct_reader,game_scene,remote_bridge,portrait_service
    from friend_dialog import FriendGenerationDialog
    from friend_preview import FriendPreview
    from friend_service import FriendService
    from ticket_codec import decode_ticket
    blocked=[]
    def deny(*args,**kwargs):
        blocked.append('forbidden entry point');raise AssertionError('Forbidden offline operation')
    class FakeBroker:
        def __init__(self,folder):self.root=folder;self.qr=[];self.remote=[];self.closed=False;self.direct_modes=[]
        def status(self):return {}
        def request_qr(self,codes,revision,enabled,**options):
            self.qr.append(dict(codes=[bytes(x) for x in codes],revision=revision,enabled=enabled,**options));return revision
        def request_remote(self,value):self.remote.append(dict(value));return len(self.remote)
        def request(self,*args,**kwargs):return deny()
        def set_direct_mode(self,value):self.direct_modes.append(value)
        def close(self):self.closed=True
    with ExitStack() as guards:
        for target,attribute in [(camera_broker.Broker,'__init__'),(camera_output.Camera,'__init__'),
                (app_dual.Stream,'__init__'),(game_scene.SceneProbe,'__init__'),
                (direct_reader.Windows,'__init__'),(direct_reader.Endpoint,'__init__'),
                (direct_reader.Bridge,'__init__'),(remote_bridge.NativeEnvironment,'find'),
                (remote_bridge.NativeMemory,'__init__'),(portrait_service.PortraitService,'__init__')]:
            guards.enter_context(patch.object(target,attribute,deny))
        application=QApplication.instance() or QApplication(['PriParaOfflineCheck'])
        check('offscreen Qt platform',application.platformName()=='offscreen')
        broker=FakeBroker(root);window=None;dialogs=[]
        try:
            window=app_dual.SuiteWindow(root,broker,background=False)
            window.timer.stop();window.remote.timer.stop()
            for browser in window.galleries.values():browser.timer.stop()
            check('real six-tab suite starts without optional images',list(window.channels)==['lower'] and [window.tabs.tabText(i) for i in range(window.tabs.count())]==['QR 리더','코디 리모컨','마이티켓','우정티켓','캐릭터 생성','우정티켓 변환기'] and window.catalog.available and not window.catalog.images_available and window.ticket_worker is None and window.index_worker is None and window.portraits is None and not window.isVisible() and window.channels['lower'].camera is None)
            check('helper title and version',window.windowTitle()=='프리파라HK 도우미 v2.3.1')
            check('KR converter starts without private key material',not window.converter.service.key_status().get('available',False))
            check('database loads validated auxiliary metadata',bool(window.catalog.sets) and all(item['aux_known'] for item in window.catalog.items.values()))
            check('startup does not transmit',not broker.remote and not any(x.get('send_requested') for x in broker.qr))
            catalog=window.catalog;first,second=catalog.list_sets()[:2]
            folder=root/'synthetic-tickets';folder.mkdir()
            synthetic_profiles=[]
            for name,kind,uid,outfit in [('alpha_my.png',2,1001,first),('alpha_tomo.png',3,1001,first),('beta_tomo.png',3,1002,second)]:
                raw=_body(kind,uid,outfit,catalog)
                synthetic_profiles.append(decode_ticket(raw))
                image=Image.fromarray(np.asarray(zxingcpp.create_barcode(raw,zxingcpp.BarcodeFormat.QRCode).to_image(scale=5)));image.save(folder/name)
            first_uid=synthetic_profiles[0]['uid']
            store=window.store;indexed=store.scan(folder)
            check('real PNG index recognizes both HK ticket types',indexed['indexed']==3 and len(store.tickets('my'))==1 and len(store.tickets('tomo'))==2)
            users=store.users('tomo')
            check('same nickname stays separated by synthetic UID',len(users)==2 and len({x['uid'] for x in users})==2 and {x['nickname'] for x in users}=={'A'})
            check('store name UID and outfit searches',len(store.tickets('tomo',search='A'))==2 and not store.tickets('tomo',search='Z') and len(store.tickets('tomo',uid=first_uid))==1 and bool(store.tickets('tomo',search=first['name'],search_by='coord')))
            check('ticket code is canonical UID without rewriting internal profile',first_uid==synthetic_profiles[0]['native_serial']==synthetic_profiles[0]['ticket_code'] and synthetic_profiles[0]['profile_id']==1001)
            store.set_user_note(first_uid,'테스트 메모\n두 번째 줄');window.index_changed({})
            check('notes persist by canonical UID across both ticket kinds',store.user_note(first_uid)=='테스트 메모\n두 번째 줄' and all(any(u['uid']==first_uid and u['note']=='테스트 메모\n두 번째 줄' for u in store.users(kind)) for kind in ('my','tomo')))
            for kind,browser in window.galleries.items():
                browser.reload();browser.sort.setCurrentIndex(browser.sort.findData('uid'))
                check(kind+' browser indexed rows',browser.files.count()==(1 if kind=='my' else 2))
                browser.search.setText('A');check(kind+' browser name search',browser.files.count()==(1 if kind=='my' else 2))
                browser.search_by.setCurrentIndex(browser.search_by.findData('coord'));browser.search.setText(first['name'])
                check(kind+' browser Korean outfit search',browser.files.count()>=1)
                browser.search.clear();browser.sort.setCurrentIndex(browser.sort.findData('top_id'))
            check('browsing and sorting never send',not broker.remote and not any(x.get('send_requested') for x in broker.qr))
            profile=store.profile(first_uid)
            ticket_first=FriendGenerationDialog(store,catalog,window,profile=profile,outfit=second);dialogs.append(ticket_first);ticket_first.accept()
            check('ticket-first generation selection',ticket_first.result()==QDialog.DialogCode.Accepted and ticket_first.selection['profile']['raw']==profile['raw'] and ticket_first.selection['outfit']['id']==second['id'])
            set_first=FriendGenerationDialog(store,catalog,window,outfit=first);dialogs.append(set_first);set_first.users.setCurrentIndex(1);set_first.accept()
            check('set-first known-user selection',set_first.result()==QDialog.DialogCode.Accepted and set_first.selection['profile']['raw'] and set_first.selection['outfit']['id']==first['id'] and set_first.users.count()==2)
            created=FriendService(folder,store,catalog).create(ticket_first.selection['profile'],ticket_first.selection['outfit'])
            decoded=decode_ticket(created['raw']);original=decode_ticket(profile['raw'])
            with Image.open(created['path']) as image:image.load();png_size=image.size;found=direct_reader.decode_image(image)
            check('FriendService PNG decodes at native ticket size',png_size==(756,508) and created['raw'] in found and decoded['kind']=='tomo')
            check('generated identity appearance and pieces match',decoded['uid']==original['uid'] and decoded['nickname']==original['nickname'] and decoded['appearance']==original['appearance'] and all(decoded['items'][slot]==second['pieces'][slot] for slot in ('top','bottom','shoes','head')))
            check('generated PNG is registered',created['record']['generated'] and len(store.tickets('tomo'))==3)
            original_png=Path(created['path']).read_bytes()
            removed=store.trash(created['record']['id'])
            check('trash removes active ticket and retains saved pixels',len(store.tickets('tomo'))==2 and not Path(created['path']).exists() and len(store.trashed())==1)
            store.scan(folder)
            check('index scan does not resurrect trash',len(store.tickets('tomo'))==2 and len(store.trashed())==1)
            restored=store.restore(removed['id'])
            check('trash restore preserves exact image',Path(created['path']).read_bytes()==original_png and len(store.tickets('tomo'))==3 and not store.trashed())
            removed_again=store.trash(created['record']['id']);store.empty_trash()
            check('explicit empty trash removes only deleted image',not store.trashed() and len(store.tickets('tomo'))==2 and (folder/'alpha_tomo.png').is_file())
            created=FriendService(folder,store,catalog).create(ticket_first.selection['profile'],ticket_first.selection['outfit'])
            check('immediate outfit setting defaults on and persists without send',window.remote.auto_send.isChecked() and not broker.remote)
            window.remote.auto_send.setChecked(False)
            restored_remote=app_dual.RemoteWidget(catalog,broker,window);dialogs.append(restored_remote)
            check('explicit immediate outfit setting survives reload',not restored_remote.auto_send.isChecked() and not broker.remote)
            preview=FriendPreview(created['path'],window);dialogs.append(preview);selected=[];preview.load_qr.connect(selected.append)
            check('preview never loads automatically',not selected and not preview.isVisible())
            button=next(b for b in preview.findChildren(QPushButton) if b.text()=='QR 리더에 불러오기');button.click()
            check('preview explicit load emits saved path',selected==[created['path']])
            preview.load_qr.connect(window.show_ticket);button.click()
            check('manual preview load reaches stopped reader without sending',window.active_channel().image is not None and window.active_channel().camera is None and not any(x.get('send_requested') for x in broker.qr) and not broker.remote)
            channel=window.active_channel();channel.update_direct_status(dict(direct_mode=False,traffic_light='orange',traffic_label='요청 대기'),__import__('time').time())
            check('reopened UI restores explicit direct-off without replay',not channel.direct_enabled.isChecked() and not broker.direct_modes and not any(x.get('send_requested') for x in broker.qr))
            channel.direct_enabled.setChecked(True)
            check('only explicit checkbox change updates service mode',broker.direct_modes==[True] and not any(x.get('send_requested') for x in broker.qr))
            emitted=[];window.remote.create_friend.disconnect();window.remote.create_friend.connect(emitted.append)
            window.remote.friend_from_entry(dict(kind='set',record=first))
            unique=next((item for item in catalog.items.values() if len(catalog.sets_for_item(item['id']))==1),None)
            if unique:window.remote.friend_from_entry(dict(kind='item',record=unique))
            check('favorites generation supports set and single item without applying outfit',len(emitted)==(2 if unique else 1) and emitted[0]['id']==first['id'] and not broker.remote)
            character=window.character
            check('character creator has seven categories and editable default likes',character.creator is not None and character.tabs.count()==7 and character.likes_input.text()=='20000000')
            character.name_input.setText('TEST☆');character.likes_input.setText('345678');character.generate_button.click()
            output=character.creator.output
            check('character generation creates and indexes real MyTicket PNG',bool(output) and Path(output['path']).is_file() and any(row['uid']==output['profile']['uid'] for row in store.tickets('my')) and output['profile']['nickname']=='TEST☆' and output['profile']['likes']==345678)
            check('character creation and preview never send automatically',not any(x.get('send_requested') for x in broker.qr) and not broker.remote)
            character.portrait_button.click();check('optional portrait renderer absence remains usable',not blocked and bool(character.portrait_image.text()))
            check('no UI window is shown',not any(widget.isVisible() for widget in QApplication.topLevelWidgets()))
            check('no forbidden operation was called',not blocked)
            result.update(QApplication_created=True,offscreen=True,real_suite_window_initialized=True,optional_images_absent=True,catalog_items=len(catalog.items),catalog_sets=len(catalog.sets),synthetic_ticket_types=[2,3],synthetic_user_groups=len(users),actual_png_decode=True,friend_service_render_verified=True,generation_dialog_directions_tested=2,preview_manual_load_tested=True,automatic_ticket_or_remote_send=False,forbidden_entry_calls=len(blocked))
        finally:
            for dialog in dialogs:dialog.close();dialog.deleteLater()
            if window is not None:window.close();window.deleteLater()
            application.processEvents()
            if blocked:raise AssertionError('Forbidden offline operation was attempted')

def run(report_path):
    result=dict(passed=False,version='2.3.1',frozen=bool(getattr(sys,'frozen',False)),checks=[],protocol_keys_embedded=False,camera_opened=False,driver_registration=False,IPC_or_mutex_created=False,producer_started=False,game_files_read_or_written=False,game_process_access=False,personal_files_read=False,network_used=False,UI_shown=False,scope='Synthetic files, actual offscreen UI, read-only DLL version; no live game or camera')
    def check(name,value):
        result['checks'].append(dict(name=name,passed=bool(value)))
        if not value:raise AssertionError(name)
    report_path=Path(report_path)
    try:
        import numpy as np
        from PIL import Image
        import PySide6
        import camera_output,zxingcpp
        from direct_reader import decode_image,supported_body
        from protocol_delivery import build_coordinate_item,decode_app_my
        from game_scene import SceneProbe
        from ticket_item import captured_item
        from ticket_capture import split_ticket
        native=camera_output._Native();sender_version=native.dll.PPF_Version();check('frame sender ABI version only',sender_version==0x10000)
        sample=np.zeros((756,1548,3),np.uint8);sample[0,0]=(12,34,56);full,tomo,my=split_ticket(Image.fromarray(sample))
        check('ticket split dimensions',full.size==(756,1548) and tomo.size==(756,508) and my.size==(756,1040))
        synthetic=b'PRIPARA_DIRECT_SYNTHETIC';qr=zxingcpp.create_barcode(synthetic,zxingcpp.BarcodeFormat.QRCode).to_image(scale=4)
        check('synthetic QR pixel roundtrip',decode_image(Image.fromarray(np.asarray(qr)))==[synthetic])
        check('binary high-byte support',supported_body(bytes(1+(i*71)%253 for i in range(121))))
        check('43-byte coordinate delivery',len(build_coordinate_item(1001))==43)
        internal=Path(getattr(sys,'_MEIPASS',Path(__file__).parent));source=Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[1]
        original=(source/'data.db').read_bytes();original_hash=hashlib.sha256(original).hexdigest()
        with tempfile.TemporaryDirectory(prefix='pripara-offline-') as temporary:
            root=Path(temporary);(root/'data.db').write_bytes(original)
            (root/'ticket_settings.json').write_text(json.dumps(dict(input_dir=str(root/'synthetic-input'),output_dir=str(root/'synthetic-tickets'))),encoding='utf-8')
            with _profile(root):_ui_check(root,result,check)
        check('distribution database unchanged',hashlib.sha256((source/'data.db').read_bytes()).hexdigest()==original_hash)
        result.update(passed=True,check_count=len(result['checks']),direct_decoder_synthetic_roundtrip=True,binary_high_bytes_supported=True,coordinate_delivery_module_loaded=True,scene_probe_module_loaded=True,capture_recovery_module_loaded=True,numpy=np.__version__,PySide6=PySide6.__version__,sender_version=hex(sender_version),sender_sha256=hashlib.sha256(camera_output._dll_path().read_bytes()).hexdigest(),internal_folder_present=internal.is_dir(),ticket_split_sizes=[list(x.size) for x in (full,tomo,my)])
    except Exception as error:
        result.update(error_type=type(error).__name__,check_count=len(result['checks']));report_path.parent.mkdir(parents=True,exist_ok=True);report_path.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8');raise
    report_path.parent.mkdir(parents=True,exist_ok=True);report_path.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8');return result


def run_portrait(report_path):
    """Render a synthetic appearance with shipped assets in a fresh profile."""
    import shutil,time
    from PIL import Image
    from PySide6.QtWidgets import QApplication
    from portrait_service import PortraitService
    import camera_broker,direct_reader,remote_bridge
    report_path=Path(report_path);report_path.parent.mkdir(parents=True,exist_ok=True)
    source=Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[1]
    result=dict(passed=False,version='2.3.1',frozen=bool(getattr(sys,'frozen',False)),synthetic_appearance=True,
        personal_browser_profile=False,game_process_access=False,camera_opened=False,QR_sent=False,UI_shown=False)
    blocked=[]
    def deny(*args,**kwargs):blocked.append(True);raise AssertionError('Forbidden portrait-check operation')
    try:
        with tempfile.TemporaryDirectory(prefix='portrait-offline-') as temporary,ExitStack() as guards:
            root=Path(temporary);shutil.copyfile(source/'images.bin',root/'images.bin')
            for target,attribute in [(camera_broker.Broker,'__init__'),(direct_reader.Windows,'__init__'),
                    (direct_reader.Endpoint,'__init__'),(remote_bridge.NativeEnvironment,'find')]:
                guards.enter_context(patch.object(target,attribute,deny))
            with _profile(root):
                app=QApplication.instance() or QApplication(['PriParaPortraitCheck']);assert app.platformName()=='offscreen'
                service=PortraitService(root);assert service.available,service.reason
                profile=dict(appearance=[5,0,1,0,0,0,0,0,0]);done=[];errors=[]
                service.ready.connect(lambda key,path:done.append(path));service.failed.connect(lambda key,message:errors.append(message))
                start=time.monotonic();assert service.request(profile) is None
                while not done and not errors and time.monotonic()-start<45:app.processEvents();time.sleep(.02)
                assert done and not errors and not blocked
                picture=Path(done[0]);assert service.request(profile)==str(picture)
                with Image.open(picture) as image:
                    assert image.size==(256,256) and len(image.convert('RGB').getcolors(65537) or [])>100
                    result['size']=list(image.size)
                shutil.copyfile(picture,report_path.with_suffix('.png'));service.close()
                if hasattr(service,'wait_closed'):assert service.wait_closed(timeout=20)
                result.update(passed=True,elapsed_seconds=round(time.monotonic()-start,3),cache_reused=True,
                    png_sha256=hashlib.sha256(picture.read_bytes()).hexdigest(),images_sha256=hashlib.sha256((root/'images.bin').read_bytes()).hexdigest())
    except Exception as error:
        result['error_type']=type(error).__name__;report_path.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8');raise
    report_path.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8');return result


def run_portrait_suite(report_path):
    """Exercise automatic portrait generation through the actual suite window."""
    import shutil,time
    import numpy as np
    import zxingcpp
    from PIL import Image
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt
    import app_dual,camera_broker,direct_reader,remote_bridge
    from portrait_service import PortraitService
    from remote_catalog import Catalog
    from ticket_store import TicketStore
    report_path=Path(report_path);report_path.parent.mkdir(parents=True,exist_ok=True)
    source=Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[1]
    result=dict(passed=False,version='2.3.1',frozen=bool(getattr(sys,'frozen',False)),
        actual_suite=True,actual_portrait_service=True,manual_preview_clicked=False,synthetic_tickets_only=True,
        private_environment_used=False,UI_shown=False,game_process_access=False,camera_opened=False,QR_sent=False)
    blocked=[]
    def deny(*args,**kwargs):blocked.append(True);raise AssertionError('Forbidden suite-check operation')
    class FakeBroker:
        def __init__(self,root):self.root=root;self.sent=False
        def status(self):return {}
        def request_qr(self,codes,revision,enabled,**options):self.sent|=bool(options.get('send_requested'));return revision
        def request_remote(self,*args,**kwargs):return deny()
        def request(self,*args,**kwargs):return deny()
        def close(self):pass
    window=None
    try:
        with tempfile.TemporaryDirectory(prefix='portrait-suite-') as temporary,ExitStack() as guards:
            root=Path(temporary)
            for name in ('data.db','images.bin'):shutil.copyfile(source/name,root/name)
            folder=root/'synthetic-tickets';folder.mkdir()
            (root/'ticket_settings.json').write_text(json.dumps(dict(input_dir=str(root/'synthetic-input'),output_dir=str(folder))),encoding='utf-8')
            for target,attribute in [(camera_broker.Broker,'__init__'),(direct_reader.Windows,'__init__'),
                    (direct_reader.Endpoint,'__init__'),(remote_bridge.NativeEnvironment,'find'),
                    (remote_bridge.NativeMemory,'__init__'),(app_dual.Stream,'__init__')]:
                guards.enter_context(patch.object(target,attribute,deny))
            with _profile(root):
                app=QApplication.instance() or QApplication(['PriParaPortraitSuiteCheck']);app_dual.configure(app)
                catalog=Catalog(root);outfit=catalog.list_sets()[0]
                for kind in (2,3):
                    raw=_body(kind,1001,outfit,catalog)
                    image=Image.fromarray(np.asarray(zxingcpp.create_barcode(raw,zxingcpp.BarcodeFormat.QRCode).to_image(scale=5)))
                    image.save(folder/('synthetic_my.png' if kind==2 else 'synthetic_tomo.png'))
                store=TicketStore(root/'data/tickets.sqlite3',catalog=catalog);assert store.scan(folder)['indexed']==2
                expected=store.users()[0];store.close();catalog.close()
                service=PortraitService(root);assert service.available,service.reason
                def settle_service():
                    service.close()
                    if hasattr(service,'wait_closed'):service.wait_closed(timeout=20)
                    else:service._executor.shutdown(wait=True,cancel_futures=False)
                guards.callback(settle_service)
                def close_suite():
                    nonlocal window
                    if window is not None:window.close();window=None
                guards.callback(close_suite)
                broker=FakeBroker(root);start=time.monotonic()
                window=app_dual.SuiteWindow(root,broker,background=False,portrait_service=service)
                window.remote.timer.stop();key=service.key(expected);path=service.cache/(key+'.png')
                while not path.is_file() and time.monotonic()-start<50:
                    app.processEvents();state=service.status(expected)
                    if state.get('state') in ('failed','unavailable'):
                        result['portrait_failure_state']=state['state'];result['portrait_failure_reason']=state.get('reason','')
                        raise AssertionError('Automatic portrait failed: '+state.get('reason',''))
                    time.sleep(.025)
                assert path.is_file(),'Automatic suite portrait was not created'
                for _ in range(3):app.processEvents();time.sleep(.025)
                for gallery in window.galleries.values():
                    gallery.reload();user=next(gallery.users.item(i) for i in range(gallery.users.count()) if gallery.users.item(i).data(Qt.ItemDataRole.UserRole+1))
                    assert not user.icon().isNull() and user.icon().cacheKey()!=gallery._placeholder_icon.cacheKey()
                with Image.open(path) as image:assert image.size==(256,256)
                assert not broker.sent and not blocked and not window.isVisible()
                shutil.copyfile(path,report_path.with_suffix('.png'))
                result.update(passed=True,auto_created=True,both_libraries_have_portraits=True,elapsed_seconds=round(time.monotonic()-start,3),
                    png_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),size=[256,256])
                window.close();window=None;app.processEvents()
    except Exception as error:
        if window is not None:window.close()
        result['error_type']=type(error).__name__;report_path.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8');raise
    report_path.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8');return result
