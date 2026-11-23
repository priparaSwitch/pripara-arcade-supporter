"""Read-only, version-pinned proof of the native Coordinate reader intent.

This reconstructs a currently active native controller's intended link. It does
not claim that a past StartDecode succeeded or fabricate an observed message.
No message, ticket, input, process-memory write or injection is performed here.
"""
from __future__ import annotations
import ctypes as C
import ctypes.wintypes as W
import hashlib,json,struct,time
from collections import OrderedDict
from pathlib import Path

# Integrity identities of the reviewed executable/SDK, not profile IDs or keys.
GAME_SHA256='bd75c18c04529a394b7c3e43a5791ecb738e802e179a41722f54513e26af0f72'
SDK_SHA256='3959ced4f2b035e4149c606da4fd39c55d383fa8c4b239bd99c5255dfaa3dfb8'
PREFERRED=0x400000
SCENE_ROOT_RVA=0x1200fa8
_CHECKS=('code_verified','scene_owner_matches','controller_owner_matches','active_friend_input','no_completion','sdk_link_result','pause_paths_verified','game_identity_stable')

def _u32(data,offset=0):return struct.unpack_from('<I',data,offset)[0]
def _layout(data):
    if len(data)<256 or data[:2]!=b'MZ':raise ValueError('Invalid PE')
    pe=_u32(data,60)
    if pe>4096 or data[pe:pe+4]!=b'PE\0\0' or struct.unpack_from('<H',data,pe+4)[0]!=0x14c:raise ValueError('Unsupported PE')
    optional=pe+24;header=struct.unpack_from('<H',data,pe+20)[0]
    if header<224 or data[optional:optional+2]!=b'\x0b\x01':raise ValueError('Not PE32')
    count=struct.unpack_from('<H',data,pe+6)[0];base=_u32(data,optional+28);size=_u32(data,optional+56)
    if not 0<count<=96 or not 0x1000<size<=0x10000000:raise ValueError('Invalid image extent')
    sections=[]
    for index in range(count):
        offset=optional+header+index*40
        if offset+40>len(data):raise ValueError('Truncated section table')
        name=data[offset:offset+8].rstrip(b'\0').decode('ascii');virtual,rva,length,raw=struct.unpack_from('<IIII',data,offset+8)
        if raw+length>len(data) or rva+max(virtual,length)>size:raise ValueError('Invalid section extent')
        sections.append(dict(name=name,rva=rva,length=length,raw=raw,flags=_u32(data,offset+36)))
    def from_rva(rva,length):
        section=next((s for s in sections if s['rva']<=rva and rva+length<=s['rva']+s['length']),None)
        if section is None:raise ValueError('Unmapped file range')
        position=section['raw']+rva-section['rva'];return data[position:position+length]
    relocation_rva,relocation_size=struct.unpack_from('<II',data,optional+96+5*8);relocations=[]
    if relocation_size:
        table=from_rva(relocation_rva,relocation_size);position=0
        while position<len(table):
            if position+8>len(table):raise ValueError('Truncated relocation block')
            page,block=struct.unpack_from('<II',table,position)
            if block<8 or block%2 or position+block>len(table):raise ValueError('Invalid relocation block')
            for entry in struct.unpack_from('<'+'H'*((block-8)//2),table,position+8):
                kind=entry>>12
                if kind==3:relocations.append(page+(entry&0xfff))
                elif kind!=0:raise ValueError('Unsupported relocation type')
            position+=block
    return dict(base=base,size=size,sections=sections,relocations=relocations,from_rva=from_rva)

def _normalized_read(memory,module,layout,rva,length):
    result=bytearray(memory.read(module+rva,length));delta=module-layout['base']
    if delta:
        for address in layout['relocations']:
            if rva<=address and address+4<=rva+length:
                offset=address-rva;struct.pack_into('<I',result,offset,(_u32(result,offset)-delta)&0xffffffff)
            elif address<rva+length and address+4>rva:raise ValueError('Partial relocation range')
    return bytes(result)

class _ReadOnlyMemory:
    def __init__(self,identity):
        self.k=C.WinDLL('kernel32',use_last_error=True);k=self.k;self.handle=None
        k.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD];k.OpenProcess.restype=W.HANDLE;k.CloseHandle.argtypes=[W.HANDLE]
        k.ReadProcessMemory.argtypes=[W.HANDLE,C.c_void_p,C.c_void_p,C.c_size_t,C.POINTER(C.c_size_t)];k.ReadProcessMemory.restype=W.BOOL
        k.CreateToolhelp32Snapshot.argtypes=[W.DWORD,W.DWORD];k.CreateToolhelp32Snapshot.restype=W.HANDLE
        class Module(C.Structure):
            _fields_=[('size',W.DWORD),('module',W.DWORD),('pid',W.DWORD),('global_usage',W.DWORD),('usage',W.DWORD),('base',C.c_void_p),('image_size',W.DWORD),('handle',W.HMODULE),('name',W.WCHAR*256),('path',W.WCHAR*260)]
        k.Module32FirstW.argtypes=[W.HANDLE,C.POINTER(Module)];k.Module32NextW.argtypes=[W.HANDLE,C.POINTER(Module)]
        self.handle=k.OpenProcess(0x1010,False,identity['pid'])
        if not self.handle:raise OSError('Game read access unavailable')
        snapshot=k.CreateToolhelp32Snapshot(0x18,identity['pid']);self.modules={}
        try:
            if snapshot in (None,C.c_void_p(-1).value):raise OSError('Module snapshot unavailable')
            entry=Module();entry.size=C.sizeof(entry);more=k.Module32FirstW(snapshot,C.byref(entry))
            while more:
                self.modules[entry.name.casefold()]=dict(base=int(entry.base),size=entry.image_size,path=Path(entry.path));more=k.Module32NextW(snapshot,C.byref(entry))
            game=self.modules.get('prineo.exe')
            if not game or str(game['path'].resolve()).casefold()!=str(Path(identity['path']).resolve()).casefold():raise ValueError('Game module identity mismatch')
            self.base=game['base'];self.size=game['size']
        except Exception:self.close();raise
        finally:
            if snapshot not in (None,C.c_void_p(-1).value):k.CloseHandle(snapshot)
    def read(self,address,length):
        if not 0x10000<=address<=0xffffffff or not 0<length<=0x1000000 or address+length>0x100000000:raise ValueError('Invalid bounded read')
        output=bytearray()
        for offset in range(0,length,65536):
            count=min(65536,length-offset);buffer=C.create_string_buffer(count);got=C.c_size_t()
            if not self.k.ReadProcessMemory(self.handle,address+offset,buffer,count,C.byref(got)) or got.value!=count:raise OSError('Game read failed')
            output.extend(buffer.raw)
        return bytes(output)
    def close(self):
        if self.handle:self.k.CloseHandle(self.handle);self.handle=None

class NativeScanIntent:
    def __init__(self,*,memory_factory=None,identity_query=None):
        self.memory_factory=memory_factory or _ReadOnlyMemory;self.identity_query=identity_query;self._inspector=None;self._verified_code=OrderedDict()
    def _identity(self,win,hwnd):
        pid=win.window_pid(hwnd)
        if self.identity_query is not None:return self.identity_query(pid)
        query=getattr(win,'query_process',None)
        if query:return query(pid)
        if self._inspector is None:
            from reader_service import ProcessInspector
            self._inspector=ProcessInspector()
        return self._inspector.inspect(pid)
    @staticmethod
    def _same(left,right):return isinstance(left,dict) and isinstance(right,dict) and all(left.get(k) is not None and left.get(k)==right.get(k) for k in ('pid','created','path'))
    def _code_verified(self,memory,identity):
        sdk=memory.modules.get('qrreader.dll')
        if not sdk:return False
        game_path=Path(identity['path']);sdk_path=Path(sdk['path']);game_stat=game_path.stat();sdk_stat=sdk_path.stat()
        key=(identity['pid'],identity['created'],memory.base,memory.size,str(game_path.resolve()),game_stat.st_size,game_stat.st_mtime_ns,
             sdk['base'],sdk['size'],str(sdk_path.resolve()),sdk_stat.st_size,sdk_stat.st_mtime_ns)
        now=time.monotonic();cached=self._verified_code.pop(key,None)
        if cached is not None and 0<=now-cached[0]<(5 if cached[1] else 1):
            self._verified_code[key]=cached;return cached[1]
        result=self._verify_code_bytes(memory,identity)
        self._verified_code[key]=(time.monotonic(),result)
        while len(self._verified_code)>8:self._verified_code.popitem(last=False)
        return result
    def _verify_code_bytes(self,memory,identity):
        raw=Path(identity['path']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=GAME_SHA256:return False
        layout=_layout(raw)
        if layout['base']!=PREFERRED or layout['size']!=memory.size:return False
        text=next(section for section in layout['sections'] if section['name']=='.text')
        original=layout['from_rva'](text['rva'],text['length'])
        # The reviewed add-on executable sections contain mutable scratch data.
        # Their original source is pinned by the whole-file integrity identity;
        # all original control code is verified in the actual relocated .text.
        if hashlib.sha256(_normalized_read(memory,memory.base,layout,text['rva'],text['length'])).digest()!=hashlib.sha256(original).digest():return False
        sdk=memory.modules.get('qrreader.dll')
        if not sdk:return False
        sdk_raw=sdk['path'].read_bytes()
        if hashlib.sha256(sdk_raw).hexdigest()!=SDK_SHA256:return False
        sdk_layout=_layout(sdk_raw)
        for rva,length in ((0x7bf0,0x260),(0x8b60,0x40)):
            if _normalized_read(memory,sdk['base'],sdk_layout,rva,length)!=sdk_layout['from_rva'](rva,length):return False
        for rva,target in ((0xd763b4,0x7db0),(0xd763b8,0x7cd0),(0xd763c4,0x7c60),(0xd763c8,0x7bf0)):
            if _u32(memory.read(memory.base+rva,4))!=sdk['base']+target:return False
        return True
    @staticmethod
    def _state(memory):
        base=memory.base;size=memory.size
        def u(address):return _u32(memory.read(address,4))
        def byte(address):return memory.read(address,1)[0]
        if memory.read(base,2)!=b'MZ':raise ValueError('Invalid mapped image')
        manager=u(base+SCENE_ROOT_RVA);scene=u(manager+0x40);vtable=u(scene)
        if not base+4<=vtable<base+size-4:raise ValueError('Invalid scene vtable')
        locator=u(vtable-4)
        if not base<=locator<base+size-16:raise ValueError('Invalid scene locator')
        descriptor=u(locator+12)
        if not base<=descriptor<base+size-168:raise ValueError('Invalid scene descriptor')
        if memory.read(descriptor+8,160).split(b'\0',1)[0]!=b'.?AVSceneCoordinate@@':raise ValueError('Different scene')
        controller=u(scene+0x54);resource=u(scene+0x48)
        return dict(manager=manager,scene=scene,controller=controller,owner=u(controller),resource=resource,
                    phase=u(scene+0x64),mode=u(controller+4),once=byte(controller+0x150),step=u(controller+0x154),
                    complete=byte(controller+0x148),done=byte(controller+0x149),modal=byte(resource+0x31c),
                    linked=byte(base+0x120648d),callback=u(base+0x120649c))
    def inspect(self,win,game_hwnd):
        failure=dict(verified=False,source='native_coordinate_scan_intent',reason='native_intent_unverified')
        memory=None
        try:
            identity=self._identity(win,game_hwnd)
            if not identity or Path(identity['path']).name.casefold()!='prineo.exe' or not isinstance(identity.get('created'),str):return failure
            if win.game_window()!=game_hwnd or win.window_pid(game_hwnd)!=identity['pid']:return failure
            memory=self.memory_factory(identity);before=self._state(memory)
            if not (before['owner']==before['scene'] and before['phase']==4 and before['mode']==1 and before['once']==1 and before['step']==0 and before['complete']==0 and before['done']==0 and before['modal']==0 and before['linked']==1 and 0xc000<=before['callback']<=0xffff):return failure
            if not self._code_verified(memory,identity):return failure
            time.sleep(.01);after=self._state(memory);current=self._identity(win,game_hwnd)
            if before!=after or not self._same(identity,current) or win.game_window()!=game_hwnd or win.window_pid(game_hwnd)!=identity['pid']:return failure
            checks={key:True for key in _CHECKS}
            signature=hashlib.sha256(json.dumps(dict(state=after,pid=identity['pid'],created=identity['created'],base=memory.base),sort_keys=True).encode()).hexdigest()
            return dict(verified=True,source='native_coordinate_scan_intent',game_hwnd=game_hwnd,game_pid=identity['pid'],game_created=identity['created'],
                        module_base=memory.base,scene=after['scene'],controller=after['controller'],state_signature=signature,checks=checks,
                        historical_start_success_claimed=False,observed_message_claimed=False,tickets_armed=False,memory_writes=False)
        except (OSError,ValueError,TypeError,KeyError,StopIteration,struct.error,OverflowError):return failure
        finally:
            if memory is not None:memory.close()
