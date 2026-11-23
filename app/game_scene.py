"""Read only the linked game's scene RTTI. Never write or inject into a game."""
import ctypes as C
import ctypes.wintypes as W
from pathlib import Path
import struct

class SceneProbe:
    def __init__(self,windows):
        self.win=windows;self.k=windows.k
        self.k.ReadProcessMemory.argtypes=[W.HANDLE,C.c_void_p,C.c_void_p,C.c_size_t,C.POINTER(C.c_size_t)]
        self.k.CreateToolhelp32Snapshot.argtypes=[W.DWORD,W.DWORD];self.k.CreateToolhelp32Snapshot.restype=W.HANDLE
        class Module(C.Structure):
            _fields_=[('size',W.DWORD),('module',W.DWORD),('pid',W.DWORD),('global_usage',W.DWORD),('usage',W.DWORD),('base',C.c_void_p),('image_size',W.DWORD),('handle',W.HMODULE),('name',W.WCHAR*256),('path',W.WCHAR*260)]
        self.Module=Module
        self.k.Module32FirstW.argtypes=[W.HANDLE,C.POINTER(Module)]
        self.k.Module32NextW.argtypes=[W.HANDLE,C.POINTER(Module)]
    def scene(self,hwnd):
        try:return self._scene(hwnd)
        except (OSError,ValueError,TypeError,OverflowError):return None
    def _scene(self,hwnd):
        path=self.win.path_for_window(hwnd)
        if not path or path.name.lower()!='prineo.exe':return None
        pid=self.win.window_pid(hwnd);snapshot=self.k.CreateToolhelp32Snapshot(0x18,pid)
        if snapshot in (None,C.c_void_p(-1).value):return None
        base=size=0
        try:
            entry=self.Module();entry.size=C.sizeof(entry);ok=self.k.Module32FirstW(snapshot,C.byref(entry))
            while ok:
                if entry.name.lower()=='prineo.exe' and Path(entry.path).resolve()==path.resolve():
                    base=int(entry.base);size=entry.image_size;break
                ok=self.k.Module32NextW(snapshot,C.byref(entry))
        finally:self.k.CloseHandle(snapshot)
        if not base or not 0x100000<size<0x10000000:return None
        handle=self.k.OpenProcess(0x1010,False,pid)  # QUERY_LIMITED_INFORMATION | VM_READ only.
        if not handle:return None
        try:
            def read(address,length):
                if not 0x10000<=address<=0xFFFFFFFF-length:raise ValueError('Invalid game pointer')
                data=C.create_string_buffer(length);got=C.c_size_t()
                if not self.k.ReadProcessMemory(handle,address,data,length,C.byref(got)) or got.value!=length:raise ValueError('Game state unavailable')
                return data.raw
            def u32(address):return struct.unpack('<I',read(address,4))[0]
            header=read(base,64)
            if header[:2]!=b'MZ':return None
            pe=u32(base+0x3C)
            if pe>0x1000 or read(base+pe,4)!=b'PE\0\0' or read(base+pe+24,2)!=b'\x0b\x01':return None
            preferred=u32(base+pe+24+28)
            root_rva=0x1600FA8-0x400000
            # The loader may update the mapped PE header's ImageBase to the
            # actual ASLR base. Both forms describe the same verified module.
            if preferred not in (0x400000,base) or not 0<root_rva<size-4:return None
            manager=u32(base+root_rva);scene=u32(manager+0x40);vtable=u32(scene)
            if not base+4<=vtable<base+size-4:return None
            locator=u32(vtable-4)
            if not base<=locator<base+size-16:return None
            descriptor=u32(locator+12)
            if not base<=descriptor<base+size-136:return None
            name=read(descriptor+8,128).split(b'\0',1)[0]
            if not name.startswith(b'.?AVScene') or not name.endswith(b'@@'):return None
            return name[4:-2].decode('ascii')
        except (OSError,ValueError,struct.error,UnicodeError):return None
        finally:self.k.CloseHandle(handle)

