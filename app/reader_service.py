"""Private local service leases; persisted reader transport outlives its UI.

Only runtime capabilities are stored in the ignored IPC directory. Process
creation identity, canonical path, session and elevation are checked afresh;
an unverified PID or stale descriptor never authorizes helper reuse.
"""
import ctypes as C
import ctypes.wintypes as W
from pathlib import Path
import os,time,uuid
from ipc_files import atomic_json,read_json

PROTOCOL=2
SERVICE_NAME='reader_service_v2'

def canonical(path):return os.path.normcase(str(Path(path).resolve()))
def same_process(left,right):
    return isinstance(left,dict) and isinstance(right,dict) and all(left.get(key)==right.get(key) and left.get(key) is not None for key in ('pid','created','path','session_id','user_sid'))

class ProcessInspector:
    """Query-only Windows identity. No VM, input or message-sending API."""
    def __init__(self):
        self.k=C.WinDLL('kernel32',use_last_error=True);self.a=C.WinDLL('advapi32',use_last_error=True);self.u=C.WinDLL('user32',use_last_error=True)
        k=self.k;k.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD];k.OpenProcess.restype=W.HANDLE;k.CloseHandle.argtypes=[W.HANDLE]
        k.QueryFullProcessImageNameW.argtypes=[W.HANDLE,W.DWORD,W.LPWSTR,C.POINTER(W.DWORD)]
        k.GetProcessTimes.argtypes=[W.HANDLE,C.POINTER(W.FILETIME),C.POINTER(W.FILETIME),C.POINTER(W.FILETIME),C.POINTER(W.FILETIME)]
        k.GetExitCodeProcess.argtypes=[W.HANDLE,C.POINTER(W.DWORD)];k.ProcessIdToSessionId.argtypes=[W.DWORD,C.POINTER(W.DWORD)]
        self.a.OpenProcessToken.argtypes=[W.HANDLE,W.DWORD,C.POINTER(W.HANDLE)]
        self.a.GetTokenInformation.argtypes=[W.HANDLE,C.c_int,C.c_void_p,W.DWORD,C.POINTER(W.DWORD)]
        self.a.ConvertSidToStringSidW.argtypes=[C.c_void_p,C.POINTER(W.LPWSTR)];self.a.ConvertSidToStringSidW.restype=W.BOOL
        k.LocalFree.argtypes=[C.c_void_p];k.LocalFree.restype=C.c_void_p
        self.a.ConvertSidToStringSidW.argtypes=[C.c_void_p,C.POINTER(W.LPWSTR)];k.LocalFree.argtypes=[C.c_void_p]
        self.a.ConvertSidToStringSidW.argtypes=[C.c_void_p,C.POINTER(W.LPWSTR)];k.LocalFree.argtypes=[C.c_void_p]
        self.u.GetWindowThreadProcessId.argtypes=[W.HWND,C.POINTER(W.DWORD)]
        self.u.IsWindow.argtypes=[W.HWND];self.u.GetClassNameW.argtypes=[W.HWND,W.LPWSTR,C.c_int]
        k.OpenFileMappingW.argtypes=[W.DWORD,W.BOOL,W.LPCWSTR];k.OpenFileMappingW.restype=W.HANDLE
        k.MapViewOfFile.argtypes=[W.HANDLE,W.DWORD,W.DWORD,W.DWORD,C.c_size_t];k.MapViewOfFile.restype=C.c_void_p;k.UnmapViewOfFile.argtypes=[C.c_void_p]
    def inspect(self,pid):
        if type(pid) is not int or pid<=0:return None
        h=self.k.OpenProcess(0x1000,False,pid)
        if not h:return None
        token=W.HANDLE()
        try:
            exitcode=W.DWORD()
            if not self.k.GetExitCodeProcess(h,C.byref(exitcode)) or exitcode.value!=259:return None
            path=C.create_unicode_buffer(32768);length=W.DWORD(len(path))
            times=[W.FILETIME() for unused in range(4)];session=W.DWORD()
            if not self.k.QueryFullProcessImageNameW(h,0,path,C.byref(length)) or not self.k.GetProcessTimes(h,*[C.byref(value) for value in times]) or not self.k.ProcessIdToSessionId(pid,C.byref(session)):return None
            if not self.a.OpenProcessToken(h,8,C.byref(token)):return None
            elevated=W.DWORD();needed=W.DWORD()
            if not self.a.GetTokenInformation(token,20,C.byref(elevated),4,C.byref(needed)):return None
            needed=W.DWORD();self.a.GetTokenInformation(token,1,None,0,C.byref(needed))
            if not 0<needed.value<65536:return None
            user=C.create_string_buffer(needed.value)
            if not self.a.GetTokenInformation(token,1,user,len(user),C.byref(needed)):return None
            sid=W.LPWSTR()
            if not self.a.ConvertSidToStringSidW(C.c_void_p.from_buffer(user),C.byref(sid)):return None
            try:user_sid=sid.value
            finally:self.k.LocalFree(C.cast(sid,C.c_void_p))
            created=(times[0].dwHighDateTime<<32)|times[0].dwLowDateTime
            return dict(pid=pid,created=str(created),path=canonical(path.value),session_id=session.value,user_sid=user_sid,elevated=bool(elevated.value))
        finally:
            if token:self.k.CloseHandle(token)
            self.k.CloseHandle(h)
    def endpoint(self):
        mapping=self.k.OpenFileMappingW(4,False,'_Psytec_QR_Code_Reader_Cam1_File_Mapping_')
        if not mapping:return dict(hwnd=0,pid=0,class_name='')
        view=None
        try:
            view=self.k.MapViewOfFile(mapping,4,0,0,4)
            hwnd=C.c_uint32.from_address(view).value if view else 0;pid=W.DWORD();name=C.create_unicode_buffer(256)
            if hwnd and self.u.IsWindow(hwnd):self.u.GetWindowThreadProcessId(hwnd,C.byref(pid));self.u.GetClassNameW(hwnd,name,len(name))
            return dict(hwnd=hwnd,pid=pid.value,class_name=name.value)
        finally:
            if view:self.k.UnmapViewOfFile(view)
            self.k.CloseHandle(mapping)

def verified_service(record,inspector,client,*,now=None):
    if not isinstance(record,dict) or record.get('protocol')!=PROTOCOL:return False
    token=record.get('token')
    if not isinstance(token,str) or len(token)!=64 or any(c not in '0123456789abcdef' for c in token):return False
    try:
        age=(time.time() if now is None else now)-float(record['at'])
        stated=record['identity'];actual=inspector.inspect(stated['pid'])
        if not -1<=age<4 or not same_process(stated,actual) or actual.get('elevated') is not True:return False
        if actual['path']!=client['path'] or actual['session_id']!=client['session_id'] or actual['user_sid']!=client['user_sid']:return False
        window=record.get('endpoint_hwnd',0)
        if type(window) is not int or window<0:return False
        if window:
            endpoint=inspector.endpoint()
            if endpoint['hwnd']!=window or endpoint['pid']!=actual['pid'] or not endpoint['class_name'].startswith('PriParaDirectQr_'):return False
        return True
    except (KeyError,TypeError,ValueError,OSError):return False

class ServiceLease:
    """Filesystem control plane. All identity checks are injectable for tests."""
    def __init__(self,root,inspector,identity):
        self.root=Path(root).resolve();self.inspector=inspector;self.identity=identity
        self.directory=self.root/'ipc'/SERVICE_NAME;self.directory.mkdir(parents=True,exist_ok=True)
        self.token=uuid.uuid4().hex+uuid.uuid4().hex;self.session=None;self.owner=None;self.attach_revision=None
        self.last_attach=None;self.last_detach=None
    def session_path(self,name):
        if not isinstance(name,str) or str(uuid.UUID(name))!=name:raise ValueError('Invalid UI session')
        path=(self.root/'ipc'/name).resolve()
        if path.parent!=(self.root/'ipc').resolve() or not path.is_dir():raise ValueError('Invalid UI session')
        return path
    def _owner(self,folder):
        data=read_json(folder/'owner.json');owner=data['identity'];actual=self.inspector.inspect(owner['pid'])
        if not same_process(owner,actual) or actual['path']!=self.identity['path'] or actual['session_id']!=self.identity['session_id'] or actual['user_sid']!=self.identity['user_sid']:raise ValueError('UI identity changed')
        try:
            detached=read_json(folder/'detach.json')
            if isinstance(detached,dict) and detached.get('session')==folder.name and same_process(detached.get('owner'),owner):raise ValueError('UI already detached')
        except FileNotFoundError:pass
        return actual
    def startup(self,session):
        folder=self.session_path(Path(session).name);owner=self._owner(folder)
        self.session=folder;self.owner=owner;self.attach_revision=None
        return folder
    def _owned_request(self,data):
        return isinstance(data,dict) and data.get('token')==self.token and self.session is not None and data.get('session')==self.session.name and same_process(data.get('owner'),self.owner)
    def poll(self):
        before=self.session
        if self.owner and not same_process(self.owner,self.inspector.inspect(self.owner['pid'])):self.session=None;self.owner=None
        if self.session:
            try:
                data=read_json(self.session/'detach.json')
                if data.get('session')==self.session.name and same_process(data.get('owner'),self.owner):self.session=None;self.owner=None
            except (OSError,ValueError,TypeError,AttributeError):pass
        try:
            data=read_json(self.directory/'detach.json')
            if data.get('revision')!=self.last_detach and self._owned_request(data):
                self.last_detach=data['revision'];self.session=None;self.owner=None
        except (OSError,ValueError,TypeError,KeyError,AttributeError):pass
        try:
            data=read_json(self.directory/'attach.json')
            if data.get('revision')!=self.last_attach and data.get('token')==self.token and -1<=time.time()-float(data.get('at',0))<5:
                self.last_attach=data['revision'];folder=self.session_path(data['session']);owner=self._owner(folder)
                if not same_process(data.get('owner'),owner):raise ValueError('Attach identity mismatch')
                # One still-running UI cannot be silently displaced.
                if self.owner is None or same_process(self.owner,owner):
                    self.session=folder;self.owner=owner;self.attach_revision=data['revision']
        except (OSError,ValueError,TypeError,KeyError,AttributeError):pass
        return before!=self.session
    def describe(self,endpoint_hwnd=0,game=None):
        return dict(protocol=PROTOCOL,at=time.time(),identity=self.identity,token=self.token,
                    endpoint_hwnd=int(endpoint_hwnd or 0),session=self.session.name if self.session else None,
                    attach_revision=self.attach_revision,game=game,ui_attached=self.session is not None)
    def publish(self,endpoint_hwnd=0,game=None):
        data=self.describe(endpoint_hwnd,game);atomic_json(self.directory/'service.json',data,timeout=.15);return data
