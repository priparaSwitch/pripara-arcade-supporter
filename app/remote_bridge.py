"""Port of the existing outfit remote's guarded 17-byte MyChara request.

Discovery follows the running Prineo.exe process name and image base. There is
no fixed installation path or executable hash. No injection, remote thread,
game code patch, debug-flag write, network request or background resend exists.
"""
from dataclasses import dataclass
import ctypes as C
import ctypes.wintypes as W
import struct
import time

SLOTS=('top','bottom','shoes','head')
GAME_SIGNATURE=bytes.fromhex('83 ec 08 d9 44 24')

class MemoryFailure(RuntimeError):
    pass

def _u32(memory,address):return struct.unpack('<I',memory.read(address,4))[0]
def _byte(memory,address):return memory.read(address,1)[0]

def _kind(memory,address,base,size):
    if address<0x10000:return ''
    try:
        vtable=_u32(memory,address)
        if not base+4<=vtable<base+size:return ''
        locator=_u32(memory,vtable-4)
        if not base<=locator<base+size-16:return ''
        descriptor=_u32(memory,locator+12)
        if not base<=descriptor<base+size-168:return ''
        return memory.read(descriptor+8,160).split(b'\0',1)[0].decode('ascii')
    except (MemoryFailure,ValueError,struct.error,UnicodeError):return ''

@dataclass(frozen=True)
class GameContext:
    base:int;image_size:int;manager:int;scene:int;resource:int;debug:int;control:int
    my:int;array:int;database:int;table:int;count:int;debug_enabled:bool
    def same(self,other):
        return isinstance(other,GameContext) and all(getattr(self,key)==getattr(other,key) for key in (
            'base','image_size','manager','scene','resource','debug','control','my','array','database','table','count'))

def resolve(memory,base,size):
    if not 0x10000<=base<=0xffffffff or not 0<size<=0x10000000:
        raise MemoryFailure('게임의 메모리 구성을 확인할 수 없어요.')
    manager=_u32(memory,base+0x1600fa8-0x400000);scene=_u32(memory,manager+0x40)
    if _kind(memory,scene,base,size)!='.?AVSceneCoordinate@@':
        raise MemoryFailure('게임에서 코디 화면을 열어 주세요.')
    resource=_u32(memory,scene+0x48)
    if _kind(memory,resource,base,size)!='.?AVCoordinateResource@@':
        raise MemoryFailure('코디 화면을 준비하고 있어요. 잠시 뒤 다시 선택해 주세요.')
    debug=_u32(memory,resource+0x418);control=_u32(memory,debug+4)
    debug_flag=_byte(memory,control+0x28)
    if debug_flag not in (0,1):raise MemoryFailure('코디 화면 상태를 확인할 수 없어요. 잠시 뒤 다시 선택해 주세요.')
    if _u32(memory,control)!=0:
        raise MemoryFailure('코디 TYPE을 MyChara로 맞춰 주세요. Ctrl로 DEBUG를 켠 뒤 F11로 변경하고 다시 꺼도 됩니다.')
    first=_u32(memory,control+0x1c);last=_u32(memory,control+0x20)
    if last<first or last-first!=12:raise MemoryFailure('지원하는 코디 디버그 상태가 아니에요.')
    my=_u32(memory,first)
    if _kind(memory,my,base,size)!='.?AVCoordinateDebugMyChara@@' or _u32(memory,my+0x2c)!=4:
        raise MemoryFailure('MyChara 코디 선택 화면을 확인해 주세요.')
    array=_u32(memory,my+0x24);database=_u32(memory,my+4)
    if _kind(memory,database,base,size)!='.?AVItemResource@@':
        raise MemoryFailure('게임 의상 목록을 읽을 수 없어요.')
    header=_u32(memory,database+0xc);table=_u32(memory,database+0x10);count=_u32(memory,header+4)
    if not 0<count<=50000 or array<0x10000 or table<0x10000:
        raise MemoryFailure('게임 의상 목록을 확인할 수 없어요.')
    return GameContext(base,size,manager,scene,resource,debug,control,my,array,database,table,count,debug_flag==1)

def open_context(memory,info):
    base=info['base']
    if memory.read(base+0x1c50a0,6)!=GAME_SIGNATURE:
        raise MemoryFailure('게임의 코디 처리 버전이 달라 연결을 멈췄어요.')
    if memory.read(base,2)!=b'MZ':raise MemoryFailure('게임 실행 이미지가 올바르지 않아요.')
    pe=_u32(memory,base+0x3c)
    if pe>0x10000 or memory.read(base+pe,4)!=b'PE\0\0':raise MemoryFailure('게임 실행 이미지가 올바르지 않아요.')
    size=_u32(memory,base+pe+24+56)
    return resolve(memory,base,size)

def map_rows(memory,context,outfit,items):
    table=memory.read(context.table,context.count*144);rows=[]
    for index,slot in enumerate(SLOTS):
        identity=outfit[slot]
        if identity==0:rows.append(0xffffffff);continue
        item=items[identity];source=item['sourceRow'];found=None
        def matches(row):
            return struct.unpack_from('<I',table,row*144+0x68)[0]==identity and table[row*144+0x78]==index+1
        if 0<=source<context.count and matches(source):found=source
        if found is None:found=next((row for row in range(context.count) if matches(row)),None)
        if found is None:raise MemoryFailure('게임에 없는 의상이 포함되어 있어요: '+item['name'])
        rows.append(found)
    return rows

def _pack(memory,address):
    first=_u32(memory,address+0xc);last=_u32(memory,address+0x10)
    if first==0 or last<first or last-first>64 or (last-first)%8:return []
    data=memory.read(first,last-first)
    return [struct.unpack_from('<I',data,index)[0] for index in range(0,len(data),8)]

def deliver(memory,context,outfit,items,*,clock=time.monotonic,sleeper=time.sleep):
    if not context.same(resolve(memory,context.base,context.image_size)):
        raise MemoryFailure('게임 화면이 바뀌었어요. 다시 선택해 주세요.')
    if _byte(memory,context.my+0x30):raise MemoryFailure('이전 코디를 반영 중이에요. 잠시 기다려 주세요.')
    rows=map_rows(memory,context,outfit,items);payload=struct.pack('<4I',*rows)
    before=memory.read(context.array,16)
    if not context.same(resolve(memory,context.base,context.image_size)):
        raise MemoryFailure('게임 화면이 바뀌었어요. 다시 선택해 주세요.')
    memory.write(context.array,payload)
    if memory.read(context.array,16)!=payload:
        raise MemoryFailure('선택값을 확인할 수 없어 적용을 멈췄어요.')
    try:
        if not context.same(resolve(memory,context.base,context.image_size)):
            raise MemoryFailure('게임 화면이 바뀌어 적용을 멈췄어요.')
        memory.write(context.my+0x30,b'\x01')
    except Exception:
        try:
            if context.same(resolve(memory,context.base,context.image_size)) and not _byte(memory,context.my+0x30):
                memory.write(context.array,before)
        except Exception:pass
        raise
    expected=[outfit[slot] for slot in SLOTS if outfit[slot]];started=clock();accepted=False
    while clock()-started<2.5:
        if _u32(memory,context.manager+0x40)!=context.scene:break
        if not _byte(memory,context.my+0x30) and expected==_pack(memory,context.scene+0xc8):
            accepted=True;break
        sleeper(.015)
    return dict(accepted=accepted,message='게임이 코디를 받았어요.' if accepted else '선택값을 전달했어요. 게임 화면의 반영 상태를 확인해 주세요.',
                ids=[outfit[slot] for slot in SLOTS],rows=rows,write_bytes=17,debug_enabled=context.debug_enabled)

class NativeMemory:
    def __init__(self,pid,writable=False):
        if not hasattr(C,'WinDLL'):raise MemoryFailure('Windows에서 사용할 수 있는 기능이에요.')
        self.kernel=C.WinDLL('kernel32',use_last_error=True);k=self.kernel
        k.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD];k.OpenProcess.restype=W.HANDLE
        k.CloseHandle.argtypes=[W.HANDLE]
        k.ReadProcessMemory.argtypes=[W.HANDLE,C.c_void_p,C.c_void_p,C.c_size_t,C.POINTER(C.c_size_t)]
        k.WriteProcessMemory.argtypes=[W.HANDLE,C.c_void_p,C.c_void_p,C.c_size_t,C.POINTER(C.c_size_t)]
        self.handle=k.OpenProcess(0x438 if writable else 0x410,False,pid);self.writable=writable
        if not self.handle:raise MemoryFailure('게임에 연결할 수 없어요. 게임과 송출 도우미의 실행 권한을 확인해 주세요.')
    def read(self,address,length):
        if not 0x10000<=address<=0xffffffff or not 0<=length<=8000000 or address+length>0x100000000:
            raise MemoryFailure('게임의 데이터 위치를 확인할 수 없어요.')
        if not length:return b''
        data=C.create_string_buffer(length);got=C.c_size_t()
        if not self.kernel.ReadProcessMemory(self.handle,address,data,length,C.byref(got)) or got.value!=length:
            raise MemoryFailure('게임 상태가 바뀌었어요. 코디 화면에서 다시 선택해 주세요.')
        return data.raw
    def write(self,address,data):
        if not self.writable or not 0x10000<=address<=0xffffffff or address+len(data)>0x100000000:
            raise MemoryFailure('코디를 전달할 수 없는 연결이에요.')
        raw=C.create_string_buffer(bytes(data),len(data));written=C.c_size_t()
        if not self.kernel.WriteProcessMemory(self.handle,address,raw,len(data),C.byref(written)) or written.value!=len(data):
            raise MemoryFailure('코디를 전달하지 못했어요. 게임 화면을 확인해 주세요.')
    def close(self):
        if self.handle:self.kernel.CloseHandle(self.handle);self.handle=None
    def __enter__(self):return self
    def __exit__(self,*unused):self.close()

class NativeEnvironment:
    def find(self):
        if not hasattr(C,'WinDLL'):raise MemoryFailure('Windows에서 사용할 수 있는 기능이에요.')
        kernel=C.WinDLL('kernel32',use_last_error=True)
        kernel.CreateToolhelp32Snapshot.argtypes=[W.DWORD,W.DWORD];kernel.CreateToolhelp32Snapshot.restype=W.HANDLE
        kernel.CloseHandle.argtypes=[W.HANDLE]
        class Process(C.Structure):
            _fields_=[('size',W.DWORD),('usage',W.DWORD),('pid',W.DWORD),('heap',C.c_size_t),('module',W.DWORD),('threads',W.DWORD),('parent',W.DWORD),('priority',W.LONG),('flags',W.DWORD),('name',W.WCHAR*260)]
        kernel.Process32FirstW.argtypes=[W.HANDLE,C.POINTER(Process)];kernel.Process32NextW.argtypes=[W.HANDLE,C.POINTER(Process)]
        handle=kernel.CreateToolhelp32Snapshot(2,0)
        if handle==C.c_void_p(-1).value:raise MemoryFailure('게임 실행 목록을 읽을 수 없어요.')
        pids=[]
        try:
            entry=Process();entry.size=C.sizeof(entry);more=kernel.Process32FirstW(handle,C.byref(entry))
            while more:
                if entry.name.lower()=='prineo.exe':pids.append(entry.pid)
                more=kernel.Process32NextW(handle,C.byref(entry))
        finally:kernel.CloseHandle(handle)
        if not pids:raise MemoryFailure('게임 실행을 기다리고 있어요.')
        if len(pids)!=1:raise MemoryFailure('Prineo가 여러 개 켜져 있어요. 하나만 남겨 주세요.')
        pid=pids[0]
        class Module(C.Structure):
            _fields_=[('size',W.DWORD),('module',W.DWORD),('pid',W.DWORD),('global_usage',W.DWORD),('usage',W.DWORD),('base',C.c_void_p),('image_size',W.DWORD),('handle',W.HMODULE),('name',W.WCHAR*256),('path',W.WCHAR*260)]
        kernel.Module32FirstW.argtypes=[W.HANDLE,C.POINTER(Module)];kernel.Module32NextW.argtypes=[W.HANDLE,C.POINTER(Module)]
        handle=kernel.CreateToolhelp32Snapshot(0x18,pid)
        if handle==C.c_void_p(-1).value:raise MemoryFailure('게임에 연결할 수 없어요. 송출 도우미의 실행 권한을 확인해 주세요.')
        try:
            entry=Module();entry.size=C.sizeof(entry);more=kernel.Module32FirstW(handle,C.byref(entry))
            while more:
                if entry.name.lower()=='prineo.exe':return dict(pid=pid,base=int(entry.base),image_size=entry.image_size,path=entry.path)
                more=kernel.Module32NextW(handle,C.byref(entry))
        finally:kernel.CloseHandle(handle)
        raise MemoryFailure('게임 실행 이미지의 위치를 확인할 수 없어요.')
    def open_memory(self,pid,writable=False):return NativeMemory(pid,writable)

def friendly(error):
    if isinstance(error,(MemoryFailure,ValueError)):return str(error)
    return '게임에 연결하지 못했어요. 게임이 켜져 있는지 확인해 주세요.'

class RemoteBridge:
    def __init__(self,catalog,environment=None):
        self.catalog=catalog;self.environment=environment or NativeEnvironment()
    def inspect(self):
        try:
            if not self.catalog.available:raise MemoryFailure(self.catalog.error or 'data.db 파일을 확인해 주세요.')
            info=self.environment.find()
            with self.environment.open_memory(info['pid'],False) as memory:
                context=open_context(memory,info);raw=memory.read(context.array,16);current=[]
                for row in struct.unpack('<4I',raw):
                    current.append(_u32(memory,context.table+row*144+0x68) if row<context.count else 0)
            return dict(ready=True,message='게임 연결됨 · MyChara · DEBUG '+('ON' if context.debug_enabled else 'OFF'),pid=info['pid'],current_ids=current,debug_enabled=context.debug_enabled)
        except Exception as error:return dict(ready=False,message=friendly(error),pid=0,current_ids=[],debug_enabled=False)
    def send(self,selection):
        outfit=self.catalog.validate_outfit(selection);info=self.environment.find()
        with self.environment.open_memory(info['pid'],True) as memory:
            context=open_context(memory,info);result=deliver(memory,context,outfit,self.catalog.items)
        result['pid']=info['pid'];return result
