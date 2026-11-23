"""Original QRReader-compatible local IPC. No game-memory writes or QR keys.

The UI decodes pixels; the elevated producer owns the Windows message endpoint.
Delivery requires both a user image/resend action and an actual StartDecode
request. My-ticket item delivery is adapted only inside SceneCoordinate.
"""
from __future__ import annotations
import ctypes as C
import ctypes.wintypes as W
import hashlib
import json
import os
from pathlib import Path
import struct
import time
from ipc_files import read_bytes, read_json, atomic_json

MAPPING_BYTES=0x2E4
MAPPING_FORMAT='_Psytec_QR_Code_Reader_Cam{}_File_Mapping_'
MAX_BODY=127
NATIVE_TTL_MS=5000
_ASYNC_CALLBACKS={}  # Keep ctypes trampolines alive until Windows completes them.

def decode_image(image):
    """Return QR binary segments exactly, including control bytes/inverted QR."""
    if image is None:return []
    import zxingcpp
    results=zxingcpp.read_barcodes(image.convert('RGB'),formats=zxingcpp.BarcodeFormat.QRCode,try_invert=True,try_rotate=True)
    output=[]
    for result in results:
        if not result.valid:continue
        raw=bytes(result.bytes)
        if raw and raw not in output:output.append(raw)
    return output

def supported_body(raw):
    """Current HK app tickets are already the Reader's native mode-0 data.

    Older encrypted printed formats need a separate native decoder and continue
    to work through the existing virtual-camera option. No protocol key is copied.
    """
    raw=bytes(raw)
    return len(raw)==121 and b'\0' not in raw

def connection_signal(*,game_present,owner_verified,linked,scan_requested,connection_kind,error=None):
    """A registered link alone is never an active scan request."""
    if error:return dict(traffic_light='red',traffic_label='연결 실패 · '+str(error))
    if not game_present:return dict(traffic_light='orange',traffic_label='게임 실행 대기')
    if connection_kind=='legacy' and owner_verified:
        return dict(traffic_light='orange',traffic_label='기존 QRReader 연결 · 요청 상태 확인 불가')
    if owner_verified and linked and scan_requested:
        return dict(traffic_light='green',traffic_label='게임이 QR 읽기를 요청 중')
    if owner_verified:return dict(traffic_light='orange',traffic_label='연결됨 · 게임의 QR 읽기 요청 대기')
    return dict(traffic_light='orange',traffic_label='QR 연결 준비 중')

class Windows:
    def __init__(self):
        self.k=C.WinDLL('kernel32',use_last_error=True)
        self.u=C.WinDLL('user32',use_last_error=True)
        k,u=self.k,self.u
        k.CreateFileMappingW.argtypes=[W.HANDLE,C.c_void_p,W.DWORD,W.DWORD,W.DWORD,W.LPCWSTR];k.CreateFileMappingW.restype=W.HANDLE
        k.OpenFileMappingW.argtypes=[W.DWORD,W.BOOL,W.LPCWSTR];k.OpenFileMappingW.restype=W.HANDLE
        k.MapViewOfFile.argtypes=[W.HANDLE,W.DWORD,W.DWORD,W.DWORD,C.c_size_t];k.MapViewOfFile.restype=C.c_void_p
        k.UnmapViewOfFile.argtypes=[C.c_void_p]
        k.CloseHandle.argtypes=[W.HANDLE]
        k.GetModuleHandleW.argtypes=[W.LPCWSTR];k.GetModuleHandleW.restype=W.HMODULE
        k.GetTickCount.restype=W.DWORD
        k.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD];k.OpenProcess.restype=W.HANDLE
        k.QueryFullProcessImageNameW.argtypes=[W.HANDLE,W.DWORD,W.LPWSTR,C.POINTER(W.DWORD)]
        u.GetWindowThreadProcessId.argtypes=[W.HWND,C.POINTER(W.DWORD)]
        self.ENUMPROC=C.WINFUNCTYPE(W.BOOL,W.HWND,W.LPARAM)
        u.EnumWindows.argtypes=[self.ENUMPROC,W.LPARAM]
        u.RegisterWindowMessageW.argtypes=[W.LPCWSTR];u.RegisterWindowMessageW.restype=W.UINT
        u.FindWindowW.argtypes=[W.LPCWSTR,W.LPCWSTR];u.FindWindowW.restype=W.HWND
        u.GetWindowTextW.argtypes=[W.HWND,W.LPWSTR,C.c_int]
        u.IsWindow.argtypes=[W.HWND];u.IsWindow.restype=W.BOOL
        u.SendMessageTimeoutW.argtypes=[W.HWND,W.UINT,W.WPARAM,W.LPARAM,W.UINT,W.UINT,C.POINTER(C.c_size_t)];u.SendMessageTimeoutW.restype=C.c_ssize_t
        self.SENDASYNCPROC=C.WINFUNCTYPE(None,W.HWND,W.UINT,C.c_size_t,C.c_ssize_t)
        u.SendMessageCallbackW.argtypes=[W.HWND,W.UINT,W.WPARAM,W.LPARAM,self.SENDASYNCPROC,C.c_size_t];u.SendMessageCallbackW.restype=W.BOOL
        u.PostMessageW.argtypes=[W.HWND,W.UINT,W.WPARAM,W.LPARAM];u.PostMessageW.restype=W.BOOL
        self.WNDPROC=C.WINFUNCTYPE(C.c_ssize_t,W.HWND,W.UINT,W.WPARAM,W.LPARAM)
        class WNDCLASS(C.Structure):
            _fields_=[('style',W.UINT),('proc',self.WNDPROC),('clsExtra',C.c_int),('wndExtra',C.c_int),('instance',W.HINSTANCE),('icon',W.HICON),('cursor',W.HANDLE),('background',W.HBRUSH),('menu',W.LPCWSTR),('name',W.LPCWSTR)]
        self.WNDCLASS=WNDCLASS
        u.RegisterClassW.argtypes=[C.POINTER(WNDCLASS)];u.RegisterClassW.restype=W.ATOM
        u.UnregisterClassW.argtypes=[W.LPCWSTR,W.HINSTANCE]
        u.CreateWindowExW.argtypes=[W.DWORD,W.LPCWSTR,W.LPCWSTR,W.DWORD,C.c_int,C.c_int,C.c_int,C.c_int,W.HWND,W.HMENU,W.HINSTANCE,C.c_void_p];u.CreateWindowExW.restype=W.HWND
        u.DefWindowProcW.argtypes=[W.HWND,W.UINT,W.WPARAM,W.LPARAM];u.DefWindowProcW.restype=C.c_ssize_t
        u.DestroyWindow.argtypes=[W.HWND]
        u.PeekMessageW.argtypes=[C.POINTER(W.MSG),W.HWND,W.UINT,W.UINT,W.UINT]
        u.TranslateMessage.argtypes=[C.POINTER(W.MSG)];u.DispatchMessageW.argtypes=[C.POINTER(W.MSG)];u.DispatchMessageW.restype=C.c_ssize_t

    def path_for_window(self,hwnd):
        pid=W.DWORD();self.u.GetWindowThreadProcessId(hwnd,C.byref(pid))
        h=self.k.OpenProcess(0x1000,False,pid.value)
        if not h:return None
        try:
            buf=C.create_unicode_buffer(32768);n=W.DWORD(len(buf))
            return Path(buf.value) if self.k.QueryFullProcessImageNameW(h,0,buf,C.byref(n)) else None
        finally:self.k.CloseHandle(h)

    def game_window(self):
        hwnd=self.u.FindWindowW('DirectX Graphics Window','Pripara')
        path=self.path_for_window(hwnd) if hwnd else None
        return int(hwnd) if path and path.name.lower()=='prineo.exe' else 0

    def window_pid(self,hwnd):
        pid=W.DWORD();self.u.GetWindowThreadProcessId(hwnd,C.byref(pid));return pid.value

    def mapping_owner(self,channel):
        h=self.k.OpenFileMappingW(4,False,MAPPING_FORMAT.format(channel))
        if not h:return 0
        try:
            p=self.k.MapViewOfFile(h,4,0,0,4)
            if not p:return 0
            try:return C.c_uint32.from_address(p).value
            finally:self.k.UnmapViewOfFile(p)
        finally:self.k.CloseHandle(h)

    def send(self,hwnd,message,wparam=0,lparam=0,timeout=250):
        result=C.c_size_t()
        ok=self.u.SendMessageTimeoutW(hwnd,message,wparam,lparam,0x2,timeout,C.byref(result))
        return bool(ok),result.value

    def close_reader_windows(self,owner):
        """Use the original Exit command, not monitor-only WM_CLOSE/hide.

        Original Reader command E141 routes APP_TERMINATE to its data window.
        This runs its capture cleanup and normal window destruction. A tray
        window previously closed by the legacy launcher may already be absent.
        """
        pid=W.DWORD();self.u.GetWindowThreadProcessId(owner,C.byref(pid))
        if not pid.value:return False
        tray=[]
        @self.ENUMPROC
        def each(hwnd,unused):
            candidate=W.DWORD();self.u.GetWindowThreadProcessId(hwnd,C.byref(candidate))
            if candidate.value==pid.value:
                title=C.create_unicode_buffer(256);self.u.GetWindowTextW(hwnd,title,256)
                if 'TrayIconHandler' in title.value:tray.append(hwnd)
            return True
        self.u.EnumWindows(each,0)
        if tray:return any(self.u.PostMessageW(hwnd,0x111,0xE141,0) for hwnd in tray)
        return bool(self.u.PostMessageW(owner,self.u.RegisterWindowMessageW('APP_TERMINATE'),0,0))

class Endpoint:
    """Single channel's message window and original 740-byte data mapping."""
    def __init__(self,channel=1,mapping_name=None,bootstrap_game=True,windows=None,image_provider=None,image_mapping_name=None):
        self.channel=channel;self.win=windows or Windows();self.k=self.win.k;self.u=self.win.u
        self.mapping_name=mapping_name or MAPPING_FORMAT.format(channel)
        self.bootstrap_game=bootstrap_game
        self.image_provider=image_provider;self.flip_monitor=False
        self.image_mapping_name=image_mapping_name or f'_Psytec_QR_Code_Reader_Cam{channel}_Image_Buffer_'
        self.client=0;self.callback=self.u.RegisterWindowMessageW(f'NOTIFY_DECODE{channel}')
        self.paused=True;self.requests=0;self.published=0;self.consumed=0;self.last_mode=None
        self.restored_requests=0;self.request_source='none'
        self.last_error=None;self.pending=False;self.hwnd=0;self.mapping=None;self.view=None
        self.notification_inflight=False;self.notification_started=0.;self.notification_completions=0
        self.expired=0;self.replaced=0;self.unconsumed_callbacks=0;self.notify_error_code=None
        self.pending_revision=None;self.replacement_waiting=False;self.last_consumed_at=0.
        self.notify_proc=self.win.SENDASYNCPROC(self._notification_complete)
        self.instance=self.k.GetModuleHandleW(None)
        self.class_name=f'PriParaDirectQr_{os.getpid()}_{channel}_{id(self)}'
        self.messages={name:self.u.RegisterWindowMessageW(name) for name in ['EXT_REQUIRE_LINK','EXT_PAUSE_CAPTURE','EXT_RESTART_CAPTURE','EXT_SET_DECODE_RECT','EXT_GET_DEVICE_ID','EXT_GET_PICTURE_IMG','EXT_SET_FLIP_MONITOR']}
        self.proc=self.win.WNDPROC(self._window_proc)
        wc=self.win.WNDCLASS();wc.proc=self.proc;wc.instance=self.instance;wc.name=self.class_name
        if not self.u.RegisterClassW(C.byref(wc)):raise C.WinError(C.get_last_error())
        try:
            self.hwnd=self.u.CreateWindowExW(0,self.class_name,f'PriPara direct QR Cam{channel}',0,0,0,0,0,None,None,self.instance,None)
            if not self.hwnd:raise C.WinError(C.get_last_error())
            C.set_last_error(0)
            self.mapping=self.k.CreateFileMappingW(W.HANDLE(-1),None,4,0,MAPPING_BYTES,self.mapping_name)
            error=C.get_last_error()
            if not self.mapping:raise C.WinError(error)
            if error==183:raise RuntimeError('기존 QR 리더가 아직 연결되어 있습니다.')
            self.view=self.k.MapViewOfFile(self.mapping,0xF001F,0,0,MAPPING_BYTES)
            if not self.view:raise C.WinError(C.get_last_error())
            C.memset(self.view,0,MAPPING_BYTES)
            self.write_u32(0,int(self.hwnd)&0xFFFFFFFF)
        except Exception:
            self.close();raise

    def read_u32(self,offset):return C.c_uint32.from_address(self.view+offset).value
    def write_u32(self,offset,value):C.c_uint32.from_address(self.view+offset).value=value&0xFFFFFFFF

    def _window_proc(self,hwnd,message,wparam,lparam):
        try:
            if message==self.messages['EXT_REQUIRE_LINK']:
                self.client=int(wparam);self.callback=int(lparam)
                if not self.client:
                    self.cancel_pending('pause');self.paused=True;self.recovery_blocked=True
                return 1
            if message==self.messages['EXT_RESTART_CAPTURE']:
                if self.paused:self.requests+=1
                self.paused=False
                self.request_source='game';self.recovery_blocked=False
                if not self.client and self.bootstrap_game:
                    self.client=self.win.game_window()
                    self.callback=self.u.RegisterWindowMessageW(f'NOTIFY_DECODE{self.channel}')
                return 1
            if message==self.messages['EXT_PAUSE_CAPTURE']:
                self.paused=True;self.recovery_blocked=True;self.request_source='game_pause'
                self.cancel_pending('pause')
                return 1
            if message==self.messages['EXT_SET_FLIP_MONITOR']:
                self.flip_monitor=bool(wparam);return 1
            if message==self.messages['EXT_SET_DECODE_RECT']:return 1
            if message==self.messages['EXT_GET_DEVICE_ID']:
                return 0  # Native handler returns a capture-device index, not VID/PID.
            if message==self.messages['EXT_GET_PICTURE_IMG']:return self._picture()
        except Exception as e:self.last_error=type(e).__name__;return 0
        return self.u.DefWindowProcW(hwnd,message,wparam,lparam)

    def _picture(self):
        if not self.image_provider:return 0
        # The original DLL creates this map before sending the request.
        # Supplying a partial frame must never report success to its caller.
        data=self.image_provider(self.flip_monitor)
        if data is None or len(data)!=640*480*3:return 0
        h=self.k.OpenFileMappingW(2,False,self.image_mapping_name)
        if not h:return 0
        try:
            p=self.k.MapViewOfFile(h,2,0,0,len(data))
            if not p:return 0
            try:C.memmove(p,data,len(data));return 1
            finally:self.k.UnmapViewOfFile(p)
        finally:self.k.CloseHandle(h)

    def pump(self):
        msg=W.MSG();count=0
        while count<50 and self.u.PeekMessageW(C.byref(msg),None,0,0,1):
            if msg.message==0x12:continue
            self.u.TranslateMessage(C.byref(msg));self.u.DispatchMessageW(C.byref(msg));count+=1
        self._observe_consumed()
        if getattr(self,'clear_when_idle',False) and not self.notification_inflight:self.discard_delivery()
        # A completed/failed notification cannot leave an expired ready=1
        # mailbox blocking every later image. Never overwrite an in-flight body.
        if self.view and self.pending and not self.notification_inflight and self.pending_age_ms()>=NATIVE_TTL_MS:
            self.cancel_pending('expired')

    def pending_age_ms(self):
        if not self.view or not self.pending:return 0
        return (int(self.k.GetTickCount())-self.read_u32(8))&0xFFFFFFFF

    def _observe_consumed(self):
        if self.view and self.pending and not self.read_u32(4):
            self.pending=False;self.consumed+=1;self.last_consumed_at=time.monotonic()
            self.last_error=None;self.notify_error_code=None;self.unconsumed_callbacks=0

    def cancel_pending(self,reason='replaced'):
        self._observe_consumed()
        if self.view and self.pending:
            self.write_u32(4,0);self.pending=False
            if reason=='expired':self.expired+=1
            elif reason=='replaced':self.replaced+=1

    def new_revision(self,revision):
        self.pending_revision=revision
        if self.notification_inflight:
            # Keep bytes stable until the receiver has finished its getter.
            self.replacement_waiting=True
        else:self.cancel_pending('replaced')

    def discard_delivery(self):
        """Clear our own mailbox, without changing observed Link/Start/Pause."""
        self.cancel_pending('ui_detached');self.pending_revision=None;self.replacement_waiting=False
        self.clear_when_idle=bool(self.notification_inflight)
        if self.view and not self.notification_inflight:
            self.write_u32(4,0);self.write_u32(0xc,0);self.write_u32(0x10,0)
            C.memset(self.view+0x14,0,0xc0)

    def _notification_complete(self,hwnd,message,token,result):
        try:
            self.notification_inflight=False;self.notification_completions+=1
            if not self.view:return
            self._observe_consumed()
            if self.pending:
                age=self.pending_age_ms()
                if age>=NATIVE_TTL_MS:self.cancel_pending('expired')
                elif self.replacement_waiting:self.cancel_pending('replaced')
                else:
                    # The game completed its callback without consuming data.
                    # Do not queue a second empty callback for the same packet.
                    self.unconsumed_callbacks+=1;self.cancel_pending('unconsumed')
            self.replacement_waiting=False
            if self.notify_error_code is not None:
                self.last_error=None;self.notify_error_code=None
        finally:_ASYNC_CALLBACKS.pop(int(token),None)

    def publish(self,body,mode=0):
        body=bytes(body)
        if not self.view or self.paused or self.notification_inflight:return False
        if not 0<len(body)<=MAX_BODY or b'\0' in body:raise ValueError('Invalid original Reader byte length')
        if self.read_u32(4):
            if self.pending_age_ms()>=NATIVE_TTL_MS:self.cancel_pending('expired')
            else:return False
        if not self.client and self.bootstrap_game:self.client=self.win.game_window()
        if not self.client or not self.callback or not self.u.IsWindow(self.client):return False
        # Publish readiness last so a consumer never sees a partially copied QR.
        self.write_u32(4,0)
        C.memset(self.view+0x14,0,0xC0)
        C.memmove(self.view+0x14,body+b'\0',len(body)+1)
        self.write_u32(0xC,len(body));self.write_u32(0x10,mode)
        self.write_u32(8,self.k.GetTickCount());self.write_u32(4,1)
        self.pending=True;self.published+=1;self.last_mode=mode
        # Exactly one notification can be in flight. Its completion callback is
        # delivered by the existing PeekMessage loop, without blocking video.
        token=id(self);_ASYNC_CALLBACKS[token]=(self,self.notify_proc)
        self.notification_inflight=True;self.notification_started=time.monotonic()
        C.set_last_error(0)
        ok=self.u.SendMessageCallbackW(self.client,self.callback,1,1,self.notify_proc,token)
        if not ok:
            self.notify_error_code=C.get_last_error();self.notification_inflight=False
            _ASYNC_CALLBACKS.pop(token,None);self.cancel_pending('notify_failed')
            if self.notify_error_code==5:self.last_error='게임과 송출 도우미의 실행 권한이 다릅니다. (오류 5)'
            elif self.notify_error_code==1400:self.last_error='게임 연결 창이 종료되었습니다. (오류 1400)'
            else:self.last_error='게임 QR 통지에 실패했습니다. (오류 '+str(self.notify_error_code)+')'
            return False
        self.last_error=None;self.notify_error_code=None
        return True

    def status(self):
        inflight_age=time.monotonic()-self.notification_started if self.notification_inflight else 0.
        return dict(connected=bool(self.view),paused=self.paused,linked=bool(self.client and self.u.IsWindow(self.client)),requests=self.requests,restored_requests=self.restored_requests,request_source=self.request_source,published=self.published,consumed=self.consumed,pending=self.pending,mode=self.last_mode,error=self.last_error,notification_inflight=self.notification_inflight,notification_age_seconds=round(inflight_age,3),notification_completions=self.notification_completions,notify_error_code=self.notify_error_code,pending_age_ms=self.pending_age_ms(),expired=self.expired,replaced=self.replaced,unconsumed_callbacks=self.unconsumed_callbacks)

    def close(self):
        self.cancel_pending('close')
        if self.view:
            self.write_u32(0,0);self.write_u32(4,0)
            self.k.UnmapViewOfFile(self.view);self.view=None
        if self.mapping:self.k.CloseHandle(self.mapping);self.mapping=None
        if self.hwnd:self.u.DestroyWindow(self.hwnd);self.hwnd=0
        self.u.UnregisterClassW(self.class_name,self.instance)

class Bridge:
    """Elevated producer integration; image bytes stay in the local session."""
    def __init__(self,root,session,*,persistent=False,windows=None,reconnect_provider=None):
        self.root=Path(root);self.session=Path(session) if session is not None else None;self.win=windows or Windows()
        self.persistent=bool(persistent);self.ui_attached=session is not None
        self.direct_mode=True;self._direct_mode_revision=None
        from reader_reconnect import ReconnectProof
        self.reconnect=ReconnectProof(reconnect_provider);self.game_identity=None;self._last_reconnect=0
        self.endpoint=None;self.enabled=False;self.revision=None;self.codes=[];self.index=0
        self.last_publish=0;self.last_check=0;self.error=None;self.notice='직접 연결 대기'
        self.data_error=None
        self.send_requested=False
        self.ticket_item_id=None;self.delivery_kind='raw';self.context_scene=None;self.adapt_error=None
        self.scene_probe=None
        self.handoff=None;self.handoff_deadline=0;self.retry_at=0;self.reader_path=None

    def clear_delivery(self):
        self.send_requested=False;self.codes=[];self.index=0;self.ticket_item_id=None
        # Keep the last revision consumed: rereading an unchanged IPC file
        # after a peer reset must not rearm an earlier user action.
        self.enabled=False;self.data_error=None
        if hasattr(self,'reconnect'):self.reconnect.invalidate()
        if self.endpoint:self.endpoint.discard_delivery()

    def attach_session(self,session):
        self.clear_delivery();self.session=Path(session);self.ui_attached=True;self._direct_mode_revision=None;self.revision=None
        self.notice='게임 연결 유지 · 새 티켓 선택 또는 QR 다시 보내기를 기다립니다.'

    def reset_game_peer(self):
        self.clear_delivery()
        if self.endpoint:
            self.endpoint.client=0;self.endpoint.paused=True;self.endpoint.request_source='none';self.endpoint.recovery_blocked=False
        self.notice='게임이 바뀌었습니다. 새 게임의 실제 QR 읽기 요청을 기다립니다.'

    def detach_ui(self):
        self.clear_delivery();self.session=None;self.ui_attached=False
        self.notice='프로그램 창이 닫혀 있습니다. 게임 연결만 유지합니다.'

    def _requests(self):
        if not self.ui_attached or self.session is None:return
        try:
            mode=read_json(self.session/'direct_mode.json')
            if type(mode.get('enabled')) is bool and mode.get('revision')!=self._direct_mode_revision:
                self._direct_mode_revision=mode.get('revision');self.direct_mode=mode['enabled']
                if not self.direct_mode:self.disable(force=True)
        except (OSError,ValueError,TypeError):pass
        if not self.direct_mode:return
        try:
            request=read_json(self.session/'direct_qr.json')
            enabled=bool(request.get('enabled'))
            if not enabled:
                if self.enabled:self.disable()
                self.send_requested=False;self.codes=[];self.index=0
                self.enabled=False;return
            self.enabled=True
            revision=int(request['revision'])
            if revision==self.revision:return
            data=read_bytes(self.session/'direct_qr.bin')
            if hashlib.sha256(data).hexdigest()!=request['sha256']:return
            lengths=request['lengths']
            if request['count']!=len(lengths) or len(lengths)>32 or any(not isinstance(n,int) or not 0<n<=4096 for n in lengths) or sum(lengths)!=len(data):raise ValueError('Invalid direct QR request')
            codes=[];offset=0
            for n in lengths:
                raw=data[offset:offset+n];offset+=n
                if supported_body(raw):codes.append(raw)
            self.codes=codes;self.index=0;self.revision=revision
            item_id=request.get('item_id')
            self.ticket_item_id=item_id if isinstance(item_id,int) and not isinstance(item_id,bool) and 0<item_id<255**2 else None
            self.adapt_error=None
            # Only image selection or the explicit resend button arms a batch.
            # Restoring a preview, reconnecting, or StartDecode alone never does.
            self.send_requested=request.get('send_requested') is True and bool(codes)
            if self.endpoint:self.endpoint.new_revision(revision)
            self.data_error=None if codes or not lengths else '이 인쇄형 QR은 가상카메라 방식으로 읽어 주세요.'
            self.notice=('QR 전송 대기' if self.send_requested else '티켓 선택 또는 QR 다시 보내기를 눌러 주세요.') if codes else '읽을 HK QR 이미지 대기'
        except FileNotFoundError:pass
        except (ValueError,KeyError,TypeError,OSError) as e:
            self.data_error='QR 전송 준비를 다시 시도하고 있습니다.'

    def _claim(self):
        now=time.monotonic()
        if now<self.retry_at:return
        owner=self.win.mapping_owner(1)
        if owner:
            path=self.win.path_for_window(owner)
            if not path or path.name.lower()!='qrreader1.exe':
                self.error='다른 직접 연결 프로그램이 사용 중입니다.';self.retry_at=now+2;return
            game=self.win.game_window()
            game_path=self.win.path_for_window(game) if game else None
            if not game_path:
                self.notice='게임 실행 후 기존 QRReader 연결을 전환합니다.';self.retry_at=now+1;return
            if path.resolve()!=(game_path.parent/'camera/QRReader1.exe').resolve():
                self.error='현재 게임 폴더의 QRReader1인지 확인해 주세요.';self.retry_at=now+2;return
            if self.handoff is None:
                self.reader_path=path;self.handoff=owner;self.handoff_deadline=now+5
                self.win.close_reader_windows(owner)
                self.notice='기존 QRReader 연결 정리 중';return
            if now<self.handoff_deadline:return
            self.error='QRReader1을 정상 종료한 뒤 다시 연결해 주세요.';self.retry_at=now+2;return
        self.handoff=None
        try:
            self.endpoint=Endpoint(channel=1,windows=self.win,image_provider=self.picture)
            if not self.persistent:self._restore_scan_request()
            self.notice='게임의 QR 읽기 요청 대기';self.error=None
        except (OSError,RuntimeError):
            self.error='직접 연결 준비 중입니다.';self.retry_at=now+2

    def run_tick(self):
        now=time.monotonic()
        if now-self.last_check>=.05:self.last_check=now;self._requests()
        if not self.enabled and not (self.persistent and self.endpoint):return
        if self.endpoint is None:self._claim()
        endpoint=self.endpoint
        if endpoint is None:return
        endpoint.pump()
        game=self.win.game_window()
        self._recover_verified_request(game)
        if endpoint.client and not self.win.u.IsWindow(endpoint.client):
            self.clear_delivery();endpoint.paused=True;endpoint.client=0;endpoint.request_source='none'
        if self.send_requested and self.index<len(self.codes) and game and endpoint.client==game and not endpoint.paused:
            body,mode=self.delivery(self.codes[self.index],game)
            if body is None:
                self.index+=1;self.send_requested=self.index<len(self.codes);return
            before=endpoint.published
            accepted=endpoint.publish(body,mode)
            # A busy receiver keeps the user's pending action. Once a real
            # notification is attempted, including an OS failure, consume it.
            # Each decoded QR is sent once; no timer or scan request rearms it.
            if accepted or endpoint.published!=before:
                self.last_publish=time.monotonic();self.index+=1
                self.send_requested=self.index<len(self.codes)
                if accepted:self.notice='QR 전송 완료 · 다시 보내려면 버튼을 눌러 주세요.'

    def _recover_verified_request(self,game):
        endpoint=self.endpoint
        if not (self.persistent and self.ui_attached and self.enabled and self.direct_mode and endpoint and endpoint.view and game):return
        if endpoint.requests or endpoint.restored_requests or getattr(endpoint,'recovery_blocked',False):return
        if endpoint.client==game and not endpoint.paused:return
        if not self.game_identity or self.win.window_pid(game)!=self.game_identity.get('pid'):return
        if self.win.mapping_owner(1)!=int(endpoint.hwnd):return
        self._last_reconnect=time.monotonic()
        evidence=self.reconnect.inspect(self.win,game,self.game_identity)
        if evidence is None:return
        endpoint.client=game;endpoint.callback=self.win.u.RegisterWindowMessageW('NOTIFY_DECODE1')
        endpoint.paused=False;endpoint.restored_requests+=1;endpoint.request_source='verified_native_request_recovery'
        self.notice='원게임의 읽기 요청 조건을 확인해 연결을 복구했습니다.'

    def delivery(self,raw,game):
        from protocol_delivery import decode_app_my,build_coordinate_item,read_radix255
        self.delivery_kind='raw';self.context_scene=None
        try:profile=decode_app_my(raw)
        except ValueError:return raw,0
        if self.scene_probe is None:
            from game_scene import SceneProbe
            self.scene_probe=SceneProbe(self.win)
        self.context_scene=self.scene_probe.scene(game)
        if self.context_scene!='SceneCoordinate':return raw,0
        # Older HK-My printouts replaced the prize with four worn items. A
        # verified capture record may recover the prize, never a guessed slot.
        item=self.ticket_item_id
        if item is not None:self.delivery_kind='captured_item'
        else:
            items=[read_radix255(profile,4+i*3,2) for i in range(4)]
            if items[0] and not any(items[1:]):
                item=items[0];self.delivery_kind='myticket_item'
        if item is None:
            self.adapt_error='이 QR에는 획득 아이템이 보존되어 있지 않습니다. 원래 출력한 티켓 이미지를 선택해 주세요.'
            return None,0
        self.adapt_error=None
        # Original native item format, already-normalized mode1. No encryption
        # key or game-memory write is needed. The profile QR itself is unchanged.
        return build_coordinate_item(item),1

    def status(self):
        game=self.win.game_window();owner=self.win.mapping_owner(1)
        kind='none';verified=False;linked=False;requested=False
        connection_error=self.error
        result=dict(enabled=self.enabled,direct_mode=self.direct_mode,ui_attached=self.ui_attached,persistent_transport=self.persistent,revision=self.revision,codes=len(self.codes),notice=self.notice,error=self.error,data_error=self.data_error,send_policy='user_action_only',send_requested=self.send_requested,queued_codes=max(0,len(self.codes)-self.index) if self.send_requested else 0,delivery_kind=self.delivery_kind,context_scene=self.context_scene)
        if self.endpoint:result.update(self.endpoint.status())
        if self.endpoint:
            kind='direct';verified=bool(self.endpoint.view and owner==int(self.endpoint.hwnd) and self.win.u.IsWindow(owner))
            linked=bool(game and self.endpoint.client==game and self.win.u.IsWindow(game))
            requested=not self.endpoint.paused
            connection_error=result.get('error') or connection_error
            if not verified:connection_error='QR 통신 창의 연결 대상이 바뀌었습니다.'
            elif requested and game and not linked:connection_error='게임의 QR 수신 창을 확인하지 못했습니다.'
            elif result.get('notification_age_seconds',0)>=5:connection_error='게임의 QR 통지 응답이 지연되고 있습니다.'
        elif owner:
            path=self.win.path_for_window(owner)
            game_path=self.win.path_for_window(game) if game else None
            if path and path.name.lower()=='qrreader1.exe' and (not game_path or path.resolve()==(game_path.parent/'camera/QRReader1.exe').resolve()):
                kind='legacy';verified=True
        result.update(game_present=bool(game),owner_verified=verified,linked=linked,scan_requested=requested,connection_kind=kind,scan_state_known=kind=='direct',error=connection_error)
        result.update(connection_signal(game_present=bool(game),owner_verified=verified,linked=linked,scan_requested=requested,connection_kind=kind,error=connection_error))
        if connection_error:result['message']=connection_error
        elif self.data_error:result['message']=self.data_error
        elif self.adapt_error:result['message']=self.adapt_error
        elif self.send_requested:result['message']='QR 전송 대기 · 게임이 읽을 수 있을 때 한 번 보냅니다.'
        elif self.endpoint and self.endpoint.notification_inflight:result['message']='QR 통지 처리 대기'
        elif self.endpoint and self.endpoint.consumed:
            suffix=' · 획득 아이템 전달' if self.delivery_kind in ('captured_item','myticket_item') else ''
            result['message']='게임 수신 확인 · '+str(self.endpoint.consumed)+'회 · 자동 반복 없음'+suffix
        else:result['message']=self.notice
        result['reconnect']=self.reconnect.last
        if self.endpoint and self.endpoint.request_source=='verified_native_request_recovery' and result.get('traffic_light')=='green':
            result['traffic_label']='원게임 읽기 요청 확인 · 연결 복구됨'
        return result

    def picture(self,flip):
        if not self.ui_attached or not self.enabled or self.session is None:return bytes([255])*(640*480*3)
        try:
            import numpy as np
            request=read_json(self.session/'lower.json')
            if (request.get('width'),request.get('height'))!=(640,480):return None
            data=read_bytes(self.session/'lower.rgb')
            if len(data)!=640*480*3:return None
            frame=np.frombuffer(data,dtype=np.uint8).reshape(480,640,3)
            if flip:frame=frame[:,::-1,:]
            return frame[:,:,::-1].copy().tobytes()  # Native packed BGR24.
        except (OSError,ValueError,KeyError):return None

    def _restore_scan_request(self):
        """Continue a verified, still-running game's scan after an app update.

        This is separately counted; it is not a newly observed game message.
        No default or stale session can arm a scan on an unrelated game.
        """
        try:
            path=self.root/'ipc/direct_handoff.json';state=read_json(path)
            game=self.win.game_window();age=time.time()-float(state['at'])
            if not (state.get('scan_requested') is True and state.get('used') is not True and 0<=age<15 and game and int(state['game_hwnd'])==game and int(state['game_pid'])==self.win.window_pid(game)):return
            self.endpoint.client=game;self.endpoint.callback=self.win.u.RegisterWindowMessageW('NOTIFY_DECODE1')
            self.endpoint.paused=False;self.endpoint.restored_requests+=1;self.endpoint.request_source='continued'
            state['used']=True;state['used_by']=os.getpid();atomic_json(path,state)
        except (OSError,ValueError,TypeError,KeyError):pass

    def disable(self,force=False):
        if self.persistent and not force:self.clear_delivery();return
        self.send_requested=False
        was_reading=bool(self.endpoint and not self.endpoint.paused)
        if was_reading:
            game=self.win.game_window()
            if game and self.endpoint.client==game:
                try:atomic_json(self.root/'ipc/direct_handoff.json',dict(at=time.time(),game_hwnd=game,game_pid=self.win.window_pid(game),scan_requested=True,used=False,source='observed_direct_reader'))
                except OSError:pass
        if self.endpoint:self.endpoint.close();self.endpoint=None
        self.handoff=None;self.enabled=False
        if self.reader_path and self.reader_path.is_file() and not self.win.mapping_owner(1):
            import subprocess
            # Restore only the specific original Reader stopped for this session.
            subprocess.Popen([str(self.reader_path)],cwd=str(self.reader_path.parent),creationflags=0x08000000)
            deadline=time.monotonic()+2.5
            while time.monotonic()<deadline:
                owner=self.win.mapping_owner(1)
                path=self.win.path_for_window(owner) if owner else None
                if path and path.resolve()==self.reader_path.resolve():
                    game=self.win.game_window()
                    if game:
                        self.win.send(owner,self.win.u.RegisterWindowMessageW('EXT_REQUIRE_LINK'),game,self.win.u.RegisterWindowMessageW('NOTIFY_DECODE1'))
                        if was_reading:self.win.send(owner,self.win.u.RegisterWindowMessageW('EXT_RESTART_CAPTURE'))
                    break
                time.sleep(.05)
            self.reader_path=None

    def close(self):
        if hasattr(self,'reconnect'):self.reconnect.close()
        if self.persistent:
            self.clear_delivery()
            if self.endpoint:self.endpoint.close();self.endpoint=None
            self.reader_path=None;self.handoff=None
        else:self.disable()
