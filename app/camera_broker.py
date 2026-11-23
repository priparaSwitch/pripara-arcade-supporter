"""Elevated video producer; the image UI remains at normal integrity for drop/paste."""
import ctypes,json,time,os,sys,subprocess,uuid,hashlib
from pathlib import Path
import numpy as np
import camera_output as pyvirtualcam
from single_instance import InstanceLock,event
from ipc_files import atomic_json,atomic_bytes,read_json,read_bytes

def atomic(path,data):
    atomic_json(path,data)

def publish_status(path,data):
    # Telemetry contention must never terminate the actual video producer.
    try:atomic_json(path,data,timeout=.15);return True
    except OSError:return False

class Broker:
    def __init__(self,root,*,inspector=None,launcher=None):
        from reader_service import ProcessInspector,SERVICE_NAME
        self.root=Path(root).resolve();self.session=self.root/'ipc'/str(uuid.uuid4());self.session.mkdir(parents=True)
        self.inspector=inspector or ProcessInspector();self.identity=self.inspector.inspect(os.getpid())
        if not self.identity:raise RuntimeError('프로그램 실행 정보를 확인하지 못했습니다.')
        self.service_directory=self.root/'ipc'/SERVICE_NAME;self.service_directory.mkdir(parents=True,exist_ok=True)
        self._closed=False;self._token=None;self._attach_sent=None
        atomic(self.session/'owner.json',{'identity':self.identity})
        if self._service():self._attach();return
        if launcher is not None:launcher(self.session);return
        if getattr(sys,'frozen',False):exe=sys.executable;args=['--stream-helper',str(self.session)]
        else:exe=sys.executable;args=[str(Path(__file__).with_name('app_dual.py')),'--stream-helper',str(self.session)]
        result=ctypes.windll.shell32.ShellExecuteW(None,'runas',exe,subprocess.list2cmdline(args),str(self.root),0)
        if result<=32:raise RuntimeError('송출 도우미의 관리자 권한 실행이 취소되거나 실패했습니다.')
    def request(self,role,device,frame,running=True):
        if self._closed:raise ValueError('QR reader window is detached')
        if role!='lower' or device!='PriPara QR Camera':raise ValueError('Unsupported QR camera')
        if running:
            raw=self.session/(role+'.rgb');atomic_bytes(raw,frame.tobytes())
            data={'running':True,'device':device,'width':frame.shape[1],'height':frame.shape[0],'revision':time.time_ns()}
        else:data={'running':False}
        atomic(self.session/(role+'.json'),data)
        return data.get('revision')
    def request_qr(self,raw_codes,revision,enabled,*,send_requested=False,item_id=None):
        if self._closed:raise ValueError('QR reader window is detached')
        if not isinstance(revision,int) or isinstance(revision,bool) or revision<0:raise ValueError('Invalid QR revision')
        if len(raw_codes)>16:raise ValueError('Too many QR codes')
        if item_id is not None and (not isinstance(item_id,int) or isinstance(item_id,bool) or not 0<item_id<255**2):raise ValueError('Invalid printed item')
        codes=[]
        for code in raw_codes:
            if not isinstance(code,(bytes,bytearray)) or not 0<len(code)<=4096:raise ValueError('Invalid QR data')
            codes.append(bytes(code))
        raw=b''.join(codes)
        # Payload first, metadata last. The digest lets the reader reject a
        # transient same-length payload/previous-metadata pair during refresh.
        atomic_bytes(self.session/'direct_qr.bin',raw)
        atomic(self.session/'direct_qr.json',{'enabled':bool(enabled),'revision':revision,'count':len(codes),'lengths':[len(c) for c in codes],'sha256':hashlib.sha256(raw).hexdigest(),'send_requested':bool(send_requested),'item_id':item_id})
        return revision
    def status(self):
        record=self._service()
        if not record:return {}
        if not self._closed:self._attach(record)
        try:
            state=read_json(self.service_directory/'status.json')
            if state.get('session')!=self.session.name or state.get('helper_created')!=record['identity']['created'] or state.get('pid')!=record['identity']['pid'] or state.get('elevated') is not True or not -1<=time.time()-float(state.get('at',0))<4:return {}
            return state
        except (OSError,ValueError,TypeError,AttributeError):return {}
    def _service(self):
        from reader_service import verified_service
        try:
            record=read_json(self.service_directory/'service.json')
            if verified_service(record,self.inspector,self.identity):self._token=record['token'];return record
        except (OSError,ValueError,TypeError):pass
        return None
    def _attach(self,record=None):
        record=record or self._service()
        if not record or self._closed:return
        if record.get('session')==self.session.name:return
        if self._attach_sent==record['token']:return
        atomic(self.service_directory/'attach.json',dict(token=record['token'],owner=self.identity,session=self.session.name,revision=uuid.uuid4().hex,at=time.time()))
        self._attach_sent=record['token']
    def request_remote(self,selection):
        if self._closed:raise ValueError('QR reader window is detached')
        slots=('top','bottom','shoes','head')
        if not isinstance(selection,dict) or set(selection)!=set(slots):raise ValueError('Invalid outfit selection')
        if any(not isinstance(selection[k],int) or isinstance(selection[k],bool) or not 0<=selection[k]<255**2 for k in slots):raise ValueError('Invalid outfit item')
        revision=time.time_ns()
        atomic(self.session/'remote.json',{'revision':revision,'selection':{k:selection[k] for k in slots}})
        return revision
    def set_direct_mode(self,enabled):
        if self._closed or type(enabled) is not bool:raise ValueError('Invalid direct reader mode')
        atomic(self.session/'direct_mode.json',dict(enabled=enabled,revision=time.time_ns()))
    def close(self):
        if self._closed:return
        self._closed=True
        try:
            if self._token:atomic(self.service_directory/'detach.json',dict(token=self._token,owner=self.identity,session=self.session.name,revision=uuid.uuid4().hex,at=time.time()))
            # Startup may not have published its token yet; a session-local
            # detach is accepted only for the already verified attached owner.
            atomic(self.session/'detach.json',dict(owner=self.identity,session=self.session.name))
        except OSError:pass

def _clear_session_payload(session):
    if session is None:return
    for name in ('direct_qr.bin','direct_qr.json','direct_mode.json','lower.rgb','lower.json','remote.json'):
        try:(session/name).unlink(missing_ok=True)
        except OSError:pass

def run_helper(session,root):
    from reader_service import ProcessInspector,ServiceLease
    session=Path(session).resolve();root=Path(root).resolve()
    if not session.is_relative_to(root/'ipc'):raise ValueError('Invalid session directory')
    if not ctypes.windll.shell32.IsUserAnAdmin():raise RuntimeError('Producer must run elevated')
    instance=InstanceLock('Producer')
    if not instance.primary:
        publish_status(session/'status.json',{'pid':os.getpid(),'at':time.time(),'stopped':True,'error':'다른 QR 리더 도우미가 이미 실행 중입니다.'})
        event(root,'duplicate_producer_blocked');return
    inspector=ProcessInspector();identity=inspector.inspect(os.getpid())
    if not identity or not identity['elevated']:instance.close();raise RuntimeError('Helper identity unavailable')
    lease=ServiceLease(root,inspector,identity)
    try:lease.startup(session)
    except (OSError,ValueError,KeyError,TypeError):lease.session=None;lease.owner=None
    cameras={};frames={};revisions={};counts={};errors={};sizes={}
    last_status=0;status_failures=0;bridge=None;bridge_error=None;remote=None;remote_state={}
    game=None;previous_game=None;last_game_check=0;idle_since=None;bound_session=None;draining_remote=[]
    try:
        from direct_reader import Bridge
        provider=None
        try:
            from native_scan_intent import NativeScanIntent
            provider=NativeScanIntent()
        except Exception:pass  # Optional proof failure must not stop real SDK messages.
        bridge=Bridge(root,None,persistent=True,reconnect_provider=provider)
    except Exception:bridge_error='게임 직접 연결을 준비하지 못했습니다. 연결 상태를 확인해 주세요.'
    def clear_video():
        for camera in cameras.values():
            try:camera.send(np.full((480,640,3),255,np.uint8))
            except Exception:pass
            try:camera.close()
            except Exception:pass
        cameras.clear();frames.clear();revisions.clear();sizes.clear();counts.clear();errors.clear()
    try:
        while True:
            started=time.monotonic();old_session=bound_session
            lease.poll()
            if lease.session!=bound_session:
                if remote:remote.close();draining_remote.append(remote);remote=None
                remote_state=dict(state=dict(ready=False,message='프로그램 창 연결 대기'),pending=False,busy=False)
                clear_video();_clear_session_payload(bound_session)
                if bridge:bridge.detach_ui()
                bound_session=lease.session
                if bound_session is not None and bridge:bridge.attach_session(bound_session)
            draining_remote=[job for job in draining_remote if getattr(job,'_future',None) is not None and not job._future.done()]
            if lease.session is not None and remote is None and not draining_remote:
                try:
                    from remote_runtime import RemoteRuntime
                    remote=RemoteRuntime(root,lease.session)
                except Exception:remote_state={'state':{'ready':False,'message':'리모컨을 준비하지 못했습니다.'}}
            elif draining_remote:remote_state={'state':{'ready':False,'message':'이전 코디 전송을 마무리하고 있어요.'},'busy':True}
            elif lease.session is None:remote_state={'state':{'ready':False,'message':'프로그램 창 연결 대기'},'busy':False,'pending':False}
            if started-last_game_check>=.25:
                last_game_check=started;game=None
                if bridge:
                    try:
                        hwnd=bridge.win.game_window()
                        candidate=inspector.inspect(bridge.win.window_pid(hwnd)) if hwnd else None
                        if candidate and Path(candidate['path']).name.lower()=='prineo.exe':game=candidate
                    except Exception:pass
                if previous_game and (not game or game['pid']!=previous_game['pid'] or game['created']!=previous_game['created']):
                    if bridge:bridge.reset_game_peer()
                previous_game=game
                if bridge:bridge.game_identity=game
            if game is None and lease.session is None and not draining_remote:
                if idle_since is None:idle_since=started
                elif started-idle_since>=2:break
            else:idle_since=None
            if bridge:
                try:bridge.run_tick();bridge_error=None
                except Exception:bridge_error='게임 직접 연결이 일시 중단되었습니다. 실제 게임 요청을 기다립니다.'
            if remote and lease.session is not None:
                try:remote_state=remote.tick()
                except Exception:remote_state={'error':'리모컨 연결이 일시 중단되었습니다. QR 리더는 계속 사용할 수 있습니다.'}
            if lease.session is not None:
                try:
                    request=read_json(lease.session/'lower.json')
                    if request.get('running') and (request.get('device')!='PriPara QR Camera' or request.get('width')!=640 or request.get('height')!=480):raise ValueError('Unsupported QR camera request')
                    if not request.get('running'):
                        clear_video()
                    else:
                        if 'lower' not in cameras:cameras['lower']=pyvirtualcam.Camera(width=640,height=480,fps=30,backend='unitycapture',device='PriPara QR Camera');counts['lower']=0
                        if revisions.get('lower')!=request['revision']:
                            raw=read_bytes(lease.session/'lower.rgb')
                            if len(raw)==640*480*3:
                                frames['lower']=np.frombuffer(raw,np.uint8).reshape(480,640,3);revisions['lower']=request['revision']
                        if 'lower' in frames:cameras['lower'].send(frames['lower']);counts['lower']=counts.get('lower',0)+1;errors.pop('lower',None)
                except FileNotFoundError:pass
                except Exception:errors['lower']='QR 카메라 영상 연결을 확인해 주세요.'
            if started-last_status>.2:
                try:direct_state={'error':True,'message':bridge_error} if bridge_error else (bridge.status() if bridge else {})
                except Exception:direct_state={'error':True,'message':'게임 연결 상태를 확인하지 못했습니다.'}
                state=dict(pid=os.getpid(),helper_created=identity['created'],elevated=True,at=time.time(),
                           persistent=True,ui_attached=lease.session is not None,session=lease.session.name if lease.session else None,
                           game_active=bool(game),status_publish_failures=status_failures,direct_qr=direct_state,remote=remote_state,
                           channels={'lower':dict(running='lower' in cameras,frames=counts.get('lower',0),revision=revisions.get('lower'),error=errors.get('lower'))})
                if not publish_status(lease.directory/'status.json',state):status_failures+=1
                if lease.session:publish_status(lease.session/'status.json',state)
                try:lease.publish(bridge.endpoint.hwnd if bridge and bridge.endpoint else 0,game)
                except (OSError,ValueError):status_failures+=1
                last_status=started
            time.sleep(max(0,1/30-(time.monotonic()-started)))
    finally:
        if remote:remote.close()
        if bridge:
            try:bridge.close()
            except Exception:pass
        clear_video();_clear_session_payload(bound_session)
        publish_status(lease.directory/'status.json',{'stopped':True,'at':time.time(),'channels':{}})
        instance.close()
