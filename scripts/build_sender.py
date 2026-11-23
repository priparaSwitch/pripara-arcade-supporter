"""Rebuild the application's CPU-frame sender; never install camera drivers."""
from pathlib import Path
import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
PRODUCT='PriParaFrameSender64.dll'
_HOME_PATH=re.compile(rb'(?:/(?:home|Users)/[^/\s"<>\x00]+/|[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s"<>\x00]+[\\/])')

def sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()

def _prefix_maps(toolchain):
    flags=[]
    for path,replacement in ((ROOT,'.'),(toolchain,'toolchain')):
        for prefix in sorted({str(path),path.as_posix()}):
            flags.extend(('-ffile-prefix-map='+prefix+'='+replacement,'-fdebug-prefix-map='+prefix+'='+replacement))
    return flags

def _normalize_runtime_paths(path):
    """Remove upstream static-runtime build paths without changing PE layout."""
    original=Path(path).read_bytes();data=bytearray(original)
    if len(data)<256 or data[:2]!=b'MZ':raise RuntimeError('Cannot normalize a non-PE output.')
    pe=struct.unpack_from('<I',data,60)[0]
    if pe+24>len(data) or data[pe:pe+4]!=b'PE\0\0':raise RuntimeError('Cannot normalize an invalid PE header.')
    count=struct.unpack_from('<H',data,pe+6)[0];optional=struct.unpack_from('<H',data,pe+20)[0]
    sections=[]
    for index in range(count):
        offset=pe+24+optional+index*40
        if offset+40>len(data):raise RuntimeError('PE section header is truncated.')
        name=bytes(data[offset:offset+8]).rstrip(b'\0').decode('ascii')
        size,start=struct.unpack_from('<II',data,offset+16);flags=struct.unpack_from('<I',data,offset+36)[0]
        if start+size>len(data):raise RuntimeError('PE section range is invalid.')
        sections.append((name,start,start+size,flags))
    ranges=[]
    for match in _HOME_PATH.finditer(original):
        start=match.start();end=original.find(b'\0',start,min(start+4096,len(original)))
        section=next((row for row in sections if row[1]<=start<row[2]),None)
        if end<0 or not section or section[0]!='.rdata' or not section[3]&0x40000000 or section[3]&0xA0000000 or end>=section[2]:
            raise RuntimeError('A runtime source path is outside a read-only standalone PE string.')
        value=original[start:end]
        if not value or any(byte<32 or byte>126 for byte in value) or not re.search(rb'\.(?:c|cc|cpp|cxx|h|hpp|hxx)$',value,re.I):
            raise RuntimeError('A runtime home path is not a supported source filename string.')
        name=re.split(rb'[\\/]',value)[-1]
        replacement=b'native-runtime/'+name
        if len(replacement)>len(value):replacement=name
        if len(replacement)>len(value):raise RuntimeError('Normalized source filename is too long.')
        data[start:end]=replacement+b'\0'*(len(value)-len(replacement));ranges.append((start,end))
    if _HOME_PATH.search(data):raise RuntimeError('Unnormalized runtime home paths remain.')
    changed=[index for index,(before,after) in enumerate(zip(original,data)) if before!=after]
    if len(data)!=len(original) or any(not any(start<=index<end for start,end in ranges) for index in changed):
        raise RuntimeError('Source-path normalization changed bytes outside its validated strings.')
    code=[];reloc=[]
    for name,start,end,flags in sections:
        if flags&0x20000000 or name=='.reloc':
            if original[start:end]!=data[start:end]:raise RuntimeError('Source-path normalization changed code or relocations.')
            entry={'section':name,'sha256':hashlib.sha256(data[start:end]).hexdigest()}
            (code if flags&0x20000000 else reloc).append(entry)
    Path(path).write_bytes(data)
    return {'normalized_string_count':len(ranges),'changed_byte_count':len(changed),'same_image_size':True,
            'only_readonly_source_strings_changed':True,'code_sections_unchanged':code,'relocation_sections_unchanged':reloc,
            'before_sha256':hashlib.sha256(original).hexdigest(),'after_sha256':hashlib.sha256(data).hexdigest(),
            'source_paths_recorded':False,'remaining_home_path_count':0}

def _validate_dll(path):
    data=Path(path).read_bytes()
    if len(data)<256 or data[:2]!=b'MZ':raise RuntimeError('Sender output is not a PE DLL.')
    offset=struct.unpack_from('<I',data,60)[0]
    if offset+24>len(data) or data[offset:offset+4]!=b'PE\0\0':raise RuntimeError('Sender PE header is invalid.')
    machine=struct.unpack_from('<H',data,offset+4)[0]
    characteristics=struct.unpack_from('<H',data,offset+22)[0]
    if machine!=0x8664 or not characteristics&0x2000:raise RuntimeError('Sender output must be a Windows x64 DLL.')
    # Loading this independent DLL/version function does not create mappings,
    # open camera channels, register filters or attach to the game.
    native=ctypes.CDLL(str(path),winmode=0x100|0x800)
    try:
        native.PPF_Version.argtypes=[];native.PPF_Version.restype=ctypes.c_uint32
        if native.PPF_Version()!=0x00010000:raise RuntimeError('Sender C ABI version is unsupported.')
        for name in ('PPF_Create','PPF_SendRgb','PPF_Close'):getattr(native,name)
    finally:
        # Explicitly unload before replacing output on Windows.
        import _ctypes
        _ctypes.FreeLibrary(native._handle)
        native._handle=0
    return {'machine':'x64','version':'00010000','required_exports':True,'camera_channels_opened':False}

def _protocol_test(compiler,env,stage,log):
    """Compare the real sender with the original receiver on channels 50/51."""
    test_dll=stage/'sender-test.dll';receiver_dll=stage/'receiver-test.dll'
    for source,destination,extra in [
        ('native/sender/PriParaFrameSender.cpp',test_dll,['-DPRIPARA_TEST_CHANNELS']),
        ('native/sender/selftest_receiver.cpp',receiver_dll,[]),
    ]:
        args=[str(compiler),'-std=c++17','-shared','-static','-O2','-DNDEBUG','-Wl,--no-insert-timestamp','-s']+_prefix_maps(compiler.parent.parent)+extra+[source,'-o',str(destination),'-ladvapi32']
        subprocess.run(args,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,check=True)
        _normalize_runtime_paths(destination)
    sender=ctypes.CDLL(str(test_dll),winmode=0x100|0x800)
    receiver=ctypes.CDLL(str(receiver_dll),winmode=0x100|0x800)
    sender.PPF_Create.argtypes=[ctypes.c_int32]*4+[ctypes.POINTER(ctypes.c_uint64)];sender.PPF_Create.restype=ctypes.c_int32
    sender.PPF_SendRgb.argtypes=[ctypes.c_uint64,ctypes.c_void_p,ctypes.c_uint64,ctypes.c_int32];sender.PPF_SendRgb.restype=ctypes.c_int32
    sender.PPF_Close.argtypes=[ctypes.c_uint64];sender.PPF_Close.restype=ctypes.c_int32
    receiver.PPFT_Start.argtypes=[ctypes.c_int];receiver.PPFT_Start.restype=ctypes.c_void_p
    receiver.PPFT_Frame.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint64,ctypes.POINTER(ctypes.c_int32),ctypes.POINTER(ctypes.c_uint64)];receiver.PPFT_Frame.restype=ctypes.c_int
    for name in ('PPFT_Stop','PPFT_Pause','PPFT_Resume'):
        getattr(receiver,name).argtypes=[ctypes.c_void_p];getattr(receiver,name).restype=None
    handles=[];receivers=[];cases=[]
    def require(condition,label):
        if not condition:raise RuntimeError('Isolated sender self-test failed: '+label)
    def create(channel,width,height):
        handle=ctypes.c_uint64();require(sender.PPF_Create(channel,width,height,1000,ctypes.byref(handle))==0,'create isolated sender')
        handles.append(handle.value);return handle.value
    def exchange(target,handle,width,height,rgb):
        frame=(ctypes.c_uint8*len(rgb)).from_buffer_copy(rgb)
        pixels=(ctypes.c_uint8*(width*height*4))();header=(ctypes.c_int32*8)();sequence=ctypes.c_uint64()
        expected=bytearray(width*height*4)
        for y in range(height):
            for x in range(width):
                offset=((height-y-1)*width+x)*3;index=(y*width+x)*4
                expected[index:index+3]=rgb[offset:offset+3];expected[index+3]=255
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            require(sender.PPF_SendRgb(handle,frame,len(rgb),width*3) in (0,1,2),'send status')
            count=receiver.PPFT_Frame(target,pixels,len(pixels),header,ctypes.byref(sequence))
            if count==len(expected) and bytes(pixels)==expected:
                require(list(header)[1:]==[width,height,width,0,0,0,1000],'receiver header')
                return
            time.sleep(.015)
        raise RuntimeError('Isolated sender self-test did not receive the expected RGB frame.')
    try:
        for channel in (0,1,2,49,52,-1):
            invalid=ctypes.c_uint64(123)
            require(sender.PPF_Create(channel,4,2,1000,ctypes.byref(invalid))==-1 and invalid.value==0,'production channels blocked')
            require(not receiver.PPFT_Start(channel),'receiver production channels blocked')
        cases.append('isolated binaries reject installed camera channels')
        for channel in (50,51):
            handle=create(channel,4,2)
            data=(ctypes.c_uint8*24)(*range(24))
            require(sender.PPF_SendRgb(handle,data,24,12)==2,'inactive receiver is normal')
            target=receiver.PPFT_Start(channel);require(bool(target),'test channel exclusively claimed');receivers.append(target)
            duplicate=ctypes.c_uint64();require(sender.PPF_Create(channel,4,2,1000,ctypes.byref(duplicate))==-2,'duplicate sender rejected')
            require(sender.PPF_SendRgb(handle,data,23,12)==-1,'truncated RGB rejected')
            require(sender.PPF_SendRgb(handle,None,24,12)==-1,'null RGB rejected')
        cases.append('inactive receiver and invalid/duplicate native calls')
        for index in range(2):exchange(receivers[index],handles[index],4,2,bytes((value+index*100)%256 for value in range(24)))
        cases.append('two independent channels match original receiver RGBA/header/orientation')
        for target in receivers:receiver.PPFT_Pause(target)
        for target in receivers:receiver.PPFT_Resume(target)
        for index in range(2):exchange(receivers[index],handles[index],4,2,bytes(reversed(range(24))))
        cases.append('original receiver pause and resume')
        old=handles.pop(0);require(sender.PPF_Close(old)==0,'close old sender')
        large=create(50,640,480)
        exchange(receivers[0],large,640,480,bytes(value%251 for value in range(640*480*3)))
        cases.append('full 640x480 frame after sender recreation')
    finally:
        for handle in handles:sender.PPF_Close(handle)
        for target in receivers:receiver.PPFT_Stop(target)
        import _ctypes
        for library in (sender,receiver):
            _ctypes.FreeLibrary(library._handle);library._handle=0
    return {'passed':True,'case_count':len(cases),'cases':cases,'channels':[50,51],
            'installed_channels_opened':False,'original_receiver_used':True}

def build(toolchain):
    if sys.platform!='win32' or ctypes.sizeof(ctypes.c_void_p)!=8:raise RuntimeError('Build the sender using 64-bit Python on Windows.')
    toolchain=Path(toolchain).expanduser().resolve(strict=True)
    compiler=toolchain/'bin/x86_64-w64-mingw32-clang++.exe'
    if not compiler.is_file():raise RuntimeError('Specified toolchain is missing its Windows x64 C++ compiler.')
    source=ROOT/'native/sender/PriParaFrameSender.cpp'
    if not source.is_file():raise RuntimeError('Sender source is missing from native/sender.')
    output=ROOT/'build/native';output.mkdir(parents=True,exist_ok=True)
    app=ROOT/'app/native';app.mkdir(parents=True,exist_ok=True)
    previous=sha(app/PRODUCT) if (app/PRODUCT).is_file() else None
    env=dict(os.environ);env['PATH']=str(toolchain/'bin')+os.pathsep+env.get('PATH','')
    version=subprocess.run([str(compiler),'--version'],capture_output=True,text=True,check=True,env=env).stdout.splitlines()[0]
    with tempfile.TemporaryDirectory(prefix='.sender-',dir=output) as temporary:
        stage=Path(temporary)/PRODUCT
        args=[str(compiler),'-std=c++17','-shared','-static','-O2','-DNDEBUG',
              '-Wl,--no-insert-timestamp','-s']+_prefix_maps(toolchain)+['native/sender/PriParaFrameSender.cpp','-o',str(stage),'-ladvapi32']
        with (output/'sender.build.log').open('wb') as stream:
            subprocess.run(args,cwd=ROOT,env=env,stdout=stream,stderr=subprocess.STDOUT,check=True)
            normalization=_normalize_runtime_paths(stage)
            protocol=_protocol_test(compiler,env,Path(temporary),stream)
        abi=_validate_dll(stage)
        fingerprint=sha(stage)
        os.replace(stage,output/PRODUCT)
        # The only runtime product this command replaces is the app's sender.
        shutil.copyfile(output/PRODUCT,app/PRODUCT)
    result={'passed':True,'compiler_version':version,'compiler_sha256':sha(compiler),
            'source_sha256':sha(source),'product':{'path':'app/native/'+PRODUCT,'bytes':(app/PRODUCT).stat().st_size,'sha256':fingerprint},
            'previous_app_sha256':previous,'matches_previous_app':previous==fingerprint if previous else None,
            'abi':abi,'protocol_test':protocol,'normalization':normalization,'source_built':True,'installed_DLLs_used':False,'driver_filters_built':False,
            'registration_performed':False,'game_accessed':False,'camera_opened':False,'paths_recorded':False}
    (output/'sender-build-report.json').write_text(json.dumps(result,indent=2,sort_keys=True)+'\n',encoding='utf-8')
    print(json.dumps(result,sort_keys=True))
    return result

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--toolchain',required=True,type=Path,help='Explicit local LLVM-MinGW toolchain root (not its bin directory)')
    args=parser.parse_args(argv)
    try:build(args.toolchain)
    except (OSError,subprocess.SubprocessError,RuntimeError,ValueError) as error:
        message=str(error) if isinstance(error,RuntimeError) else 'A build operation failed; inspect build/native/sender.build.log locally.'
        parser.exit(2,'Sender build failed: '+message+'\n')
    return 0

if __name__=='__main__':sys.exit(main())
