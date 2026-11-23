"""Cached local portraits using the existing atelier model renderer.

Only appearance numbers reach an isolated headless renderer. No ticket IDs,
names, notes, game process or personal browser profile are used.
"""
from concurrent.futures import ThreadPoolExecutor,TimeoutError as FutureTimeoutError
from pathlib import Path,PurePosixPath
import copy,hashlib,json,os,subprocess,sys,tempfile,threading,zipfile,struct,math,time,shutil,re,uuid
from PySide6.QtCore import QObject,Signal
from ipc_files import atomic_bytes

SCHEMA='portrait-v2-headchest-256'
PREFIX='_portrait/'
MAX_AUTO_ATTEMPTS=3
RETRY_DELAYS=(1.0,3.0)
JOB_FORMAT='pripara-owned-portrait-job-v1'
RUNTIME_FORMAT='pripara-owned-portrait-runtime-v1'

class _BrowserHandle:
    """Own the launcher handle while preserving Chromium's native sandbox.

    Never discover or kill an existing browser or unknown descendant. Detached
    children exit naturally; locked temporary folders are retried later.
    """
    def __init__(self,command,**options):
        self.process=subprocess.Popen(command,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),**options)
    def poll(self):return self.process.poll()
    def close(self):
        if self.process.poll() is not None:return
        try:self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=2)

def _process_exists(pid):
    if pid<=0:return True
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes as w
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];kernel.OpenProcess.restype=w.HANDLE
        kernel.GetExitCodeProcess.argtypes=[w.HANDLE,ctypes.POINTER(w.DWORD)];kernel.GetExitCodeProcess.restype=w.BOOL
        kernel.CloseHandle.argtypes=[w.HANDLE];kernel.CloseHandle.restype=w.BOOL
        handle=kernel.OpenProcess(0x1000,False,pid)  # QUERY_LIMITED_INFORMATION only.
        if not handle:return ctypes.get_last_error()!=87  # Access denied is conservatively alive.
        try:
            code=w.DWORD();return not kernel.GetExitCodeProcess(handle,ctypes.byref(code)) or code.value==259
        finally:kernel.CloseHandle(handle)
    try:os.kill(pid,0)
    except ProcessLookupError:return False
    except PermissionError:return True
    return True

def cleanup_render_jobs(cache,*,now=None):
    """Reclaim only this renderer's known, inactive job folders.

    Unknown folders and young legacy jobs are left alone; a lock is deferred,
    never worked around by killing an unrelated process. Cached PNG portraits
    and the source images archive are outside this cleanup scope.
    """
    cache=Path(cache);jobs=cache/'jobs';result={'removed':0,'deferred':0};clock=time.time() if now is None else now
    candidates=[]
    try:
        if jobs.is_dir() and not jobs.is_symlink() and jobs.resolve().parent==cache.resolve():candidates.extend((folder,jobs,JOB_FORMAT,True) for folder in jobs.iterdir())
    except OSError:result['deferred']+=1
    workspaces=cache/'workspaces'
    try:
        if workspaces.is_dir() and not workspaces.is_symlink() and workspaces.resolve().parent==cache.resolve():candidates.extend((folder,workspaces,RUNTIME_FORMAT,False) for folder in workspaces.iterdir())
    except OSError:result['deferred']+=1
    for folder,parent,expected,legacy in candidates:
        pattern=r'portrait-[A-Za-z0-9_-]+' if legacy else r'runtime-[A-Za-z0-9_-]+'
        if not re.fullmatch(pattern,folder.name) or not folder.is_dir() or folder.is_symlink() or folder.resolve().parent!=parent.resolve():continue
        try:
            marker=folder/'portrait-owner.json';owned=False
            if marker.is_file():
                if marker.stat().st_size>4096:continue
                info=json.loads(marker.read_text(encoding='utf-8'))
                if info.get('format')!=expected or type(info.get('pid')) is not int:continue
                if _process_exists(info['pid']):continue
                owned=True
            elif legacy and clock-folder.stat().st_mtime>86400:
                page=folder/'portrait.html'
                text=page.read_text(encoding='utf-8') if page.is_file() and page.stat().st_size<4*1024*1024 else ''
                owned='createFrontRenderer' in text and 'portraitFrame' in text and "document.querySelector('#ready')" in text
            if not owned:result['deferred']+=1;continue
            shutil.rmtree(folder);result['removed']+=1
        except (OSError,ValueError,TypeError):result['deferred']+=1
    return result

class _RenderDirectory:
    def __init__(self,jobs,defer=None):self.jobs=Path(jobs);self.path=None;self.nonce=uuid.uuid4().hex;self.defer=defer
    def __enter__(self):
        self.path=Path(tempfile.mkdtemp(prefix='portrait-',dir=self.jobs))
        atomic_bytes(self.path/'portrait-owner.json',json.dumps({'format':JOB_FORMAT,'pid':os.getpid(),'nonce':self.nonce}).encode('utf-8'))
        return self.path
    def __exit__(self,*error):
        for attempt in range(6):
            if _remove_owned_directory(self.path,self.jobs,JOB_FORMAT,self.nonce):return
            if attempt<5:time.sleep(.05)
        if self.defer:self.defer(self.path,self.jobs,JOB_FORMAT,self.nonce)

def _remove_owned_directory(directory,parent,expected,nonce):
    directory=Path(directory);parent=Path(parent)
    try:
        if not directory.exists():return True
        if directory.is_symlink() or directory.resolve().parent!=parent.resolve():return False
        marker=directory/'portrait-owner.json'
        if marker.stat().st_size>4096:return False
        info=json.loads(marker.read_text(encoding='utf-8'))
        if info.get('format')!=expected or info.get('pid')!=os.getpid() or info.get('nonce')!=nonce:return False
        shutil.rmtree(directory);return True
    except (OSError,ValueError,TypeError):return False

class PortraitRenderError(RuntimeError):
    def __init__(self,kind,reason,report=None):
        super().__init__(reason);self.kind=kind;self.reason=reason;self.report=report or {}

def browser_environment(job):
    """Child-only Windows environment; never inherit private app/tool settings."""
    allowed={'systemroot','windir','systemdrive','programfiles','programfiles(x86)','commonprogramfiles','commonprogramfiles(x86)','processor_architecture','os'}
    environment={key:value for key,value in os.environ.items() if key.casefold() in allowed}
    windows=Path(environment.get('SystemRoot',environment.get('SYSTEMROOT',os.environ.get('WINDIR','C:/Windows'))))
    environment.setdefault('SystemRoot',str(windows));environment.setdefault('WINDIR',str(windows))
    environment['PATH']=str(windows/'System32')+os.pathsep+str(windows)
    for variable,folder in (('HOME','home'),('USERPROFILE','home'),('APPDATA','roaming'),('LOCALAPPDATA','local'),('TEMP','temp'),('TMP','temp')):
        path=Path(job)/'environment'/folder;path.mkdir(parents=True,exist_ok=True);environment[variable]=str(path)
    return environment

def run_browser_capture(command,capture,job,timeout=35,*,cancelled=None):
    """Accept a validated capture, including a launcher's detached handoff.

    The temporary HTML/profile survives until capture success or the deadline;
    a successful launcher exit alone is not evidence of a completed render.
    """
    from PIL import Image
    started=time.monotonic();deadline=started+timeout;process=None
    stdout=Path(job)/'browser.stdout.log';stderr=Path(job)/'browser.stderr.log'
    report={};problem=None
    try:
        with stdout.open('wb') as out,stderr.open('wb') as err:
            process=_BrowserHandle(command,cwd=job,env=browser_environment(job),stdin=subprocess.DEVNULL,stdout=out,stderr=err)
            while time.monotonic()<deadline:
                if cancelled is not None and cancelled():problem=('cancelled','얼굴 미리보기 생성을 취소했습니다.');break
                code=process.poll();report['returncode']=code
                try:
                    with Image.open(capture) as image:
                        image.load()
                        marker=image.convert('RGB').getpixel((2,258)) if image.width>=256 and image.height>=264 else None
                    if marker==(0,180,72):return report
                    if marker==(222,20,20):
                        problem=('renderer','얼굴 렌더러를 준비하지 못했습니다.');break
                    if code is not None:
                        problem=('capture_not_ready','얼굴 이미지 생성이 완료되지 않았습니다.');break
                except (OSError,ValueError,SyntaxError):pass
                if code not in (None,0):
                    problem=('browser_exit','얼굴 생성용 브라우저가 종료되었습니다.');break
                time.sleep(.05)
            if problem is None:problem=('capture_timeout','얼굴 이미지 응답을 기다리다 시간이 초과되었습니다.')
    except OSError:
        problem=('browser_start','얼굴 생성용 브라우저를 시작하지 못했습니다.')
    finally:
        if process is not None:process.close()
        report['elapsed_ms']=round((time.monotonic()-started)*1000,1)
    if problem is not None:
        # Keep diagnostic categories/codes, never raw URLs, paths or stderr.
        try:
            raw=stderr.read_bytes()[-65536:];report.update(stderr_bytes=stderr.stat().st_size,
                gpu_error=b'GPU process' in raw,access_denied=b'0XC0000022' in raw.upper() or b'exit_code=-1073741790' in raw)
        except OSError:pass
        raise PortraitRenderError(problem[0],problem[1],report)

def browser_executable():
    candidates=[]
    for variable,default in (('ProgramFiles(x86)','C:/Program Files (x86)'),('ProgramFiles','C:/Program Files')):
        directory=Path(os.environ.get(variable,default))
        candidates.extend((directory/'Microsoft/Edge/Application/msedge.exe',directory/'Google/Chrome/Application/chrome.exe'))
    return next((path for path in candidates if path.is_file()),None)

def appearance_values(profile):
    values=profile.get('appearance') if isinstance(profile,dict) else None
    if not isinstance(values,(list,tuple)) or len(values)!=9 or any(type(v) is not int or not 0<=v<=254 for v in values):raise ValueError('외형 정보를 확인하지 못했습니다.')
    return list(values)

def selection_values(appearance):
    face,hair,color,skin,eye,glasses,extra,accent,tail=appearance
    if extra or tail:raise ValueError('이 원포인트 형식의 미리보기 자료가 없습니다.')
    return [face+1,skin,hair+1,color,eye+1,glasses,accent,0,0,0,0]

def compose_scene(manifest,appearance):
    values=selection_values(appearance);face,skin,hair,color,eye,glasses,accent=values[:7]
    components=manifest['components'];scene=copy.deepcopy(manifest['baseScene'])
    def part(category,key,nullable=False):
        table=components.get(category,{})
        if str(key) not in table:raise ValueError('선택한 파츠의 3D 자료가 없습니다.')
        value=table[str(key)]
        if value is None and nullable:return None
        if not isinstance(value,dict) or value.get('supported') is False:raise ValueError('선택한 파츠의 3D 자료가 없습니다.')
        return copy.deepcopy(value)
    models=[copy.deepcopy(item) for item in scene.get('models',[]) if item.get('role')=='top']
    models.extend((part('face',face),part('hair',hair)))
    values[7]=int(manifest.get('baseValues',[0]*11)[7])
    if glasses:
        accessory=part('glasses',str(face)+':'+str(glasses),True)
        if accessory:models.append(accessory)
    appearance_out={key:part(category,identity) for key,category,identity in (
        ('skin','skin',skin),('hairColor','hairColor',color),('eye','eye',str(eye)+':'+str(skin)))}
    point=part('accent',accent)
    appearance_out['accent']=point.get('appearance',{})
    if point.get('model'):models.append(point['model'])
    scene.update(models=models,appearance=appearance_out,unavailable=[],warnings=[])
    return scene,values

def portrait_frame(scene,raw_root):
    """Camera-only crop: complete upper hair silhouette through upper chest.

    Source vertices are read unchanged. Clothing/arm extents never determine
    head size, and long hair below the chest does not force a full-body view.
    """
    raw_root=Path(raw_root);points=[];face=[]
    def resource(address,extension):
        import re
        match=re.fullmatch(r'/front-cache/([0-9a-f]{64})/asset\.'+extension,address)
        if not match:raise ValueError('Invalid portrait geometry URL')
        return raw_root/(match[1]+'.'+extension)
    for entry in scene.get('models',[]):
        if entry.get('role') not in ('face','hair','glasses','accent'):continue
        model=json.loads(resource(entry['url'],'json').read_bytes())
        raw=resource(model['geometryUrl'],'bin').read_bytes();matrix=entry.get('transform')
        if matrix is not None and (len(matrix)!=16 or any(not isinstance(v,(int,float)) or not math.isfinite(v) for v in matrix)):raise ValueError('Invalid portrait transform')
        for mesh in model.get('meshes',[]):
            if mesh.get('active') is False or mesh.get('auxiliary') or mesh.get('optionalAccent'):continue
            info=mesh['positions'];offset=info['byteOffset'];count=info['count']
            if info.get('type')!='float32' or type(offset) is not int or type(count) is not int or offset<0 or count<0 or count%3 or offset+count*4>len(raw):raise ValueError('Invalid portrait vertex range')
            for point in struct.iter_unpack('<fff',raw[offset:offset+count*4]):
                if matrix is not None:
                    x,y,z=point;point=tuple(matrix[i]*x+matrix[i+4]*y+matrix[i+8]*z+matrix[i+12] for i in range(3))
                if not all(math.isfinite(v) for v in point):raise ValueError('Invalid portrait vertex')
                points.append(point)
                if entry['role']=='face':face.append(point)
    if not face or not points:raise ValueError('Portrait head geometry is unavailable')
    face_low=min(p[1] for p in face);face_high=max(p[1] for p in face);face_height=face_high-face_low
    if face_height<=0:raise ValueError('Portrait face bounds are invalid')
    bottom=face_low-face_height*.24
    visible=[point for point in points if point[1]>=bottom]
    low_x=min(p[0] for p in visible);high_x=max(p[0] for p in visible)
    top=max(p[1] for p in visible)+face_height*.055
    height=max(top-bottom,(high_x-low_x)*1.10)
    return dict(centerX=(low_x+high_x)/2,centerY=bottom+height/2,height=height,
                headTop=max(p[1] for p in visible),headLeft=low_x,headRight=high_x,chestBottom=bottom)

class PortraitService(QObject):
    ready=Signal(str,str)
    failed=Signal(str,str)
    status_changed=Signal(str)
    def __init__(self,root,parent=None):
        super().__init__(parent);self.root=Path(root);self.archive=self.root/'images.bin'
        self.manifest=None;self.reason='얼굴 미리보기 자료가 없습니다.';self.available=False
        self._lock=threading.Lock();self._pending=set();self._running=set();self._failed=set();self._closed=False;self._cache_checked={}
        self._attempts={};self._failures={};self._retry_timers={}
        self._executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='PortraitRenderer')
        self._browser=browser_executable();self._runtime=None;self._runtime_directory=None;self._runtime_nonce=None;self._runtime_cleanup_timer=None;self._cleanup_timers={};self._cleanup_active=set();self._cleanup_stopped=False;self._close_future=None
        try:
            with zipfile.ZipFile(self.archive) as archive:
                info=archive.getinfo(PREFIX+'manifest.json')
                if not 0<info.file_size<=16*1024*1024:raise ValueError('Invalid portrait manifest')
                raw=archive.read(info);manifest=json.loads(raw)
                required={PREFIX+'web/'+name for name in ('front_renderer.mjs','front_motion.mjs','front_hair_physics.mjs','vendor/three.module.js','vendor/three.core.js')}
                required.update(PREFIX+'assets/'+row['id'] for row in manifest.get('assets',[]))
                if not required.issubset(set(archive.namelist())):raise ValueError('Incomplete portrait resource closure')
            if manifest.get('format')!='pripara-portrait-components' or manifest.get('version')!=1:raise ValueError('Unsupported portrait assets')
            if not isinstance(manifest.get('components'),dict) or not isinstance(manifest.get('baseScene'),dict):raise ValueError('Incomplete portrait manifest')
            self.manifest=manifest;self.revision=hashlib.sha256(raw).hexdigest()[:20]
            self.cache=self.root/'data/portraits'/self.revision
            self.available=self._browser is not None
            self.reason='' if self.available else '새 얼굴 미리보기 생성에는 Edge 또는 Chrome이 필요합니다.'
        except (OSError,KeyError,ValueError,TypeError,zipfile.BadZipFile):self.revision='unavailable';self.cache=self.root/'data/portraits/unavailable'
        self.cleanup_report=cleanup_render_jobs(self.cache)
    def key(self,profile):
        values=appearance_values(profile)
        return hashlib.sha256((SCHEMA+'|'+self.revision+'|'+json.dumps(values,separators=(',',':'))).encode()).hexdigest()
    def request(self,profile):
        if self.manifest is None or self._closed:return None
        try:values=appearance_values(profile);key=self.key(profile)
        except ValueError:return None
        path=self.cache/(key+'.png')
        if self._cached_portrait(path):return str(path)
        if not self.available:return None
        with self._lock:
            if key in self._pending:return None
            failure=self._failures.get(key)
            if key in self._failed and (not failure or not failure['retryable'] or self._attempts.get(key,0)>=MAX_AUTO_ATTEMPTS or time.monotonic()<failure['retry_at']):return None
            self._failed.discard(key)
            self._pending.add(key)
        self.status_changed.emit(key)
        try:self._executor.submit(self._work,key,values,path)
        except RuntimeError:
            with self._lock:self._pending.discard(key)
        return None
    def status(self,profile):
        try:key=self.key(profile)
        except ValueError:return dict(state='unavailable',reason='외형 정보를 확인하지 못했습니다.',retryable=False)
        if self.manifest is None:return dict(state='unavailable',reason=self.reason,retryable=False)
        path=self.cache/(key+'.png')
        if self._cached_portrait(path):return dict(state='ready',reason='',retryable=False,path=str(path))
        if not self.available:return dict(state='unavailable',reason=self.reason,retryable=False)
        with self._lock:
            if key in self._pending:return dict(state='pending' if key in self._running else 'queued',reason='얼굴 미리보기 생성 중…' if key in self._running else '얼굴 미리보기 생성 대기 중…',retryable=False)
            failure=self._failures.get(key)
            if failure:
                wait=max(0.,failure['retry_at']-time.monotonic())
                automatic=failure['retryable'] and self._attempts.get(key,0)<MAX_AUTO_ATTEMPTS
                return dict(state='retrying' if automatic else 'failed',reason=failure['reason'],retryable=failure['retryable'],retry_after_seconds=round(wait,1))
        return dict(state='idle',reason='얼굴 미리보기 생성 대기 중…',retryable=True)
    def retry(self,profile):
        try:key=self.key(profile)
        except ValueError:return None
        with self._lock:
            if key in self._pending:return None
            timer=self._retry_timers.pop(key,None)
            if timer:timer.cancel()
            self._failed.discard(key);self._failures.pop(key,None);self._attempts.pop(key,None)
        self.status_changed.emit(key)
        return self.request(profile)
    def _cached_portrait(self,path):
        try:
            information=path.stat();signature=(information.st_size,information.st_mtime_ns)
            if not 0<information.st_size<=4*1024*1024:return False
            with self._lock:
                if self._cache_checked.get(str(path))==signature:return True
            from PIL import Image
            with Image.open(path) as image:
                if image.format!='PNG' or image.size!=(256,256):return False
                image.load()
            with self._lock:self._cache_checked[str(path)]=signature
            return True
        except (OSError,ValueError,SyntaxError):return False
    def _work(self,key,values,path):
        retry_delay=None
        try:
            if self._closed:return
            with self._lock:self._running.add(key);self._attempts[key]=self._attempts.get(key,0)+1
            self.status_changed.emit(key)
            scene,selection=compose_scene(self.manifest,values)
            runtime=self._prepare_runtime();self.cache.mkdir(parents=True,exist_ok=True)
            self._render(runtime,scene,selection,path)
            with self._lock:self._failed.discard(key);self._failures.pop(key,None)
            if not self._closed:self.ready.emit(key,str(path))
        except Exception as error:
            retryable=not isinstance(error,(ValueError,KeyError,TypeError)) or isinstance(error,PortraitRenderError)
            reason=error.reason if isinstance(error,PortraitRenderError) else ('선택한 파츠의 얼굴 자료가 없습니다.' if not retryable else '얼굴 미리보기를 만들지 못했습니다.')
            with self._lock:
                attempts=self._attempts.get(key,1)
                retry_delay=RETRY_DELAYS[min(attempts-1,len(RETRY_DELAYS)-1)] if retryable and attempts<MAX_AUTO_ATTEMPTS else None
                self._failed.add(key);self._failures[key]=dict(reason=reason,retryable=retryable,retry_at=time.monotonic()+(retry_delay or 0))
            if retryable:self._runtime=None
            if isinstance(error,PortraitRenderError):
                try:atomic_bytes(self.cache/'render-failure.json',json.dumps(dict(kind=error.kind,**error.report)).encode('utf-8'))
                except OSError:pass
            if not self._closed:self.failed.emit(key,reason)
        finally:
            with self._lock:self._pending.discard(key);self._running.discard(key)
            if self._closed:self._dispose_runtime()
            if not self._closed:
                self.status_changed.emit(key)
                if retry_delay is not None:
                    def retry_request():
                        with self._lock:self._retry_timers.pop(key,None)
                        if not self._closed:self.request(dict(appearance=list(values)))
                    timer=threading.Timer(retry_delay,retry_request);timer.daemon=True
                    with self._lock:
                        previous=self._retry_timers.pop(key,None)
                        if previous:previous.cancel()
                        self._retry_timers[key]=timer
                    if not self._closed:timer.start()
                self._schedule_runtime_cleanup()
    def _schedule_runtime_cleanup(self):
        with self._lock:
            if self._closed:return
            previous=self._runtime_cleanup_timer
            if previous:previous.cancel()
            def schedule():
                try:self._executor.submit(self._cleanup_idle_runtime)
                except RuntimeError:pass
            timer=threading.Timer(5.0,schedule);timer.daemon=True;self._runtime_cleanup_timer=timer
        timer.start()
    def _cleanup_idle_runtime(self):
        with self._lock:
            if self._pending or self._running:return
            self._runtime_cleanup_timer=None
        self._dispose_runtime()
    def _dispose_runtime(self):
        # All calls run on the single renderer worker, after capture is done.
        directory=self._runtime_directory;nonce=self._runtime_nonce;self._runtime=None;self._runtime_directory=None;self._runtime_nonce=None
        if directory is None:return
        parent=self.cache/'workspaces'
        if directory.is_symlink() or directory.resolve().parent!=parent.resolve() or not re.fullmatch(r'runtime-[A-Za-z0-9_-]+',directory.name):return
        if not _remove_owned_directory(directory,parent,RUNTIME_FORMAT,nonce):self._defer_directory(directory,parent,RUNTIME_FORMAT,nonce)
    def _defer_directory(self,directory,parent,expected,nonce,attempt=0):
        # Non-daemon render work owns file use; daemon cleanup retries only the
        # exact now-unused, nonce-marked directory. It never keeps the app alive.
        if attempt>=3:return
        key=str(directory);delays=(2.,3.,10.)  # Retry at approximately 2/5/15 seconds.
        def cleanup():
            current=threading.current_thread()
            with self._lock:
                self._cleanup_timers.pop(key,None)
                if self._cleanup_stopped:return
                self._cleanup_active.add(current)
            try:
                if not _remove_owned_directory(directory,parent,expected,nonce):self._defer_directory(directory,parent,expected,nonce,attempt+1)
            finally:
                with self._lock:self._cleanup_active.discard(current)
        timer=threading.Timer(delays[attempt],cleanup);timer.daemon=True
        with self._lock:
            if self._cleanup_stopped:return
            previous=self._cleanup_timers.pop(key,None)
            if previous:previous.cancel()
            self._cleanup_timers[key]=timer
        timer.start()
    def _prepare_runtime(self):
        if self._runtime:return self._runtime
        self._dispose_runtime();workspaces=self.cache/'workspaces';workspaces.mkdir(parents=True,exist_ok=True)
        runtime=Path(tempfile.mkdtemp(prefix='runtime-',dir=workspaces));self._runtime_directory=runtime;self._runtime_nonce=uuid.uuid4().hex
        atomic_bytes(runtime/'portrait-owner.json',json.dumps({'format':RUNTIME_FORMAT,'pid':os.getpid(),'nonce':self._runtime_nonce}).encode('utf-8'))
        raw_root=runtime/'raw';view_root=runtime/'view';web_root=runtime/'web'
        for folder in (raw_root,view_root,web_root):folder.mkdir(parents=True,exist_ok=True)
        with zipfile.ZipFile(self.archive) as archive:
            for info in archive.infolist():
                if not info.filename.startswith((PREFIX+'assets/',PREFIX+'web/')):continue
                relative=PurePosixPath(info.filename[len(PREFIX):])
                if relative.is_absolute() or '..' in relative.parts or '\\' in info.filename or ':' in info.filename:raise ValueError('Invalid portrait archive path')
                if not 0<info.file_size<=32*1024*1024:raise ValueError('Invalid portrait asset size')
                destination=(raw_root if relative.parts[0]=='assets' else web_root).joinpath(*relative.parts[1:])
                if not destination.resolve().is_relative_to(runtime.resolve()):raise ValueError('Invalid portrait destination')
                data=archive.read(info)
                if relative.parts[0]=='assets' and hashlib.sha256(data).hexdigest()!=relative.name.split('.')[0]:raise ValueError('Portrait resource checksum mismatch')
                if destination.exists() and destination.read_bytes()==data:continue
                destination.parent.mkdir(parents=True,exist_ok=True);atomic_bytes(destination,data)
        # Browser file APIs can reject long paths even when Python's verified
        # extraction succeeds. Keep full hashes in raw resources/integrity
        # checks, but expose short, collision-free per-session numeric aliases.
        sources=sorted((source for source in raw_root.iterdir() if source.is_file()),key=lambda source:source.name)
        aliases={source.name:format(index,'x')+source.suffix for index,source in enumerate(sources)}
        def rewrite(value):
            if isinstance(value,list):return [rewrite(item) for item in value]
            if isinstance(value,dict):return {key:rewrite(item) for key,item in value.items()}
            if isinstance(value,str) and value.startswith('/front-cache/'):
                pieces=value.split('/');extension=pieces[-1].rsplit('.',1)[-1]
                identity=pieces[-2]+'.'+extension
                if len(pieces[-2])!=64 or any(c not in '0123456789abcdef' for c in pieces[-2]) or extension not in ('json','bin','png','webp'):raise ValueError('Invalid renderer URL')
                if identity not in aliases:raise ValueError('Portrait resource reference is missing')
                return (view_root/aliases[identity]).resolve().as_uri()
            return value
        for source in sources:
            destination=view_root/aliases[source.name]
            if source.suffix=='.json':atomic_bytes(destination,json.dumps(rewrite(json.loads(source.read_bytes())),ensure_ascii=False,separators=(',',':')).encode('utf-8'))
            elif not destination.exists() or destination.read_bytes()!=source.read_bytes():atomic_bytes(destination,source.read_bytes())
        self._runtime=(runtime,rewrite,web_root);return self._runtime
    def _render(self,runtime,scene,selection,destination):
        from PIL import Image
        directory,rewrite,web_root=runtime
        work=self.cache/'jobs';work.mkdir(parents=True,exist_ok=True)
        with _RenderDirectory(work,self._defer_directory) as temporary:
            job=Path(temporary)
            if job.resolve().parent!=work.resolve():raise ValueError('Invalid render job')
            script=(web_root/'front_renderer.mjs').resolve().as_uri()
            frame=portrait_frame(scene,directory/'raw')
            payload=json.dumps(dict(scene=rewrite(scene),values=selection,portraitFrame=frame),ensure_ascii=False).replace('<','\\u003c')
            html='''<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src file: 'unsafe-inline'; connect-src file:; img-src file: data: blob:; style-src 'unsafe-inline'"><style>html,body{margin:0;background:#f6f3fb}#stage{width:256px;height:256px}#ready{position:absolute;left:0;top:256px;width:8px;height:8px;background:rgb(30,20,128)}</style><div id="stage"></div><div id="ready"></div><script type="module">import {createFrontRenderer} from '''+json.dumps(script)+''';const input='''+payload+''';try{const view=await createFrontRenderer({container:document.querySelector('#stage'),pixelRatio:2,interactive:true,msaaSamples:2,preserveDrawingBuffer:true,resolveSelection:async()=>input.scene});await view.update({values:input.values});view.setCameraView({focus:'body',yaw:0,pitch:0,zoom:1,panX:0,panY:0});const fit=view.stats().framing,frame=input.portraitFrame;view.setCameraView({focus:'body',yaw:0,pitch:0,zoom:(fit.top-fit.bottom)/frame.height,panX:frame.centerX-fit.position[0],panY:frame.centerY-fit.position[1]});view.render();if(!view.stats().ready)throw Error('Portrait not ready');document.querySelector('#ready').style.background='rgb(0,180,72)';}catch(error){document.querySelector('#ready').style.background='rgb(222,20,20)';document.body.dataset.error='render-failed';}</script>'''
            page=job/'portrait.html';page.write_text(html,encoding='utf-8');capture=job/'capture.png'
            args=[str(self._browser),'--headless','--allow-file-access-from-files','--use-angle=swiftshader','--enable-unsafe-swiftshader',
                '--disable-background-networking','--disable-component-update','--disable-sync','--no-first-run','--disable-default-apps',
                '--hide-scrollbars','--force-device-scale-factor=1','--window-size=384,384','--virtual-time-budget=6000',
                '--user-data-dir='+str(job/'browser'),'--screenshot='+str(capture),page.resolve().as_uri()]
            run_browser_capture(args,capture,job,cancelled=lambda:self._closed)
            with Image.open(capture) as screenshot:
                screenshot.load()
                if screenshot.width<256 or screenshot.height<264 or screenshot.convert('RGB').getpixel((2,258))!=(0,180,72):raise RuntimeError('Portrait renderer did not finish')
                portrait=screenshot.crop((0,0,256,256)).convert('RGB')
            temporary_image=destination.with_suffix('.tmp');portrait.save(temporary_image,'PNG');os.replace(temporary_image,destination)
    def close(self):
        with self._lock:
            if self._closed:return
            self._closed=True
            for timer in self._retry_timers.values():timer.cancel()
            self._retry_timers.clear()
            if self._runtime_cleanup_timer:self._runtime_cleanup_timer.cancel();self._runtime_cleanup_timer=None
        # A running capture notices _closed, closes its own launcher handle, and
        # then cleanup runs serially. Queued renders see _closed and do no work.
        try:self._close_future=self._executor.submit(self._dispose_runtime)
        except RuntimeError:pass
        self._executor.shutdown(wait=False,cancel_futures=False)
    def wait_closed(self,timeout=20.):
        """Quiesce this service before an outer owner removes its entire root.

        Normal UI close remains asynchronous. Tests/temporary-root owners can
        await the single worker and stop deferred cleanup, avoiding concurrent
        tree deletion. No browser or unrelated process is discovered or killed.
        """
        deadline=time.monotonic()+max(0.,float(timeout));self.close()
        with self._lock:
            self._cleanup_stopped=True;timers=list(self._cleanup_timers.values());self._cleanup_timers.clear();active=list(self._cleanup_active)
        for timer in timers:timer.cancel()
        try:
            if self._close_future is not None:self._close_future.result(timeout=max(0.,deadline-time.monotonic()))
        except (TimeoutError,FutureTimeoutError):return False
        for thread in set(timers+active):
            if thread is not threading.current_thread() and thread.ident is not None:thread.join(max(0.,deadline-time.monotonic()))
        return not any(thread.is_alive() for thread in active)
