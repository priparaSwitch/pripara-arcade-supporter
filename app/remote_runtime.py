"""Serialized outfit requests without blocking the QR/video producer loop.

Only this adapter's worker calls the game bridge. The producer calls tick()
and publishes its returned snapshot; changing a request revision is the sole
way to enqueue a send. A completed older send never becomes a newer result.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time

from ipc_files import read_json
from remote_bridge import RemoteBridge, MemoryFailure
from remote_catalog import Catalog

SLOTS=('top','bottom','shoes','head')
MAX_REQUEST_BYTES=16384

def _revision(value):
    if type(value) is int and 0<=value<=0xffffffffffffffff:return ('int',value)
    if isinstance(value,str) and value and len(value)<=128:return ('str',value)
    raise ValueError('리모컨 요청 번호가 올바르지 않아요.')

def _selection(value):
    if not isinstance(value,dict) or set(value)!=set(SLOTS):
        raise ValueError('리모컨 코디 요청의 부위 정보가 올바르지 않아요.')
    if any(type(value[slot]) is not int or not 0<=value[slot]<=0x7fffffff for slot in SLOTS):
        raise ValueError('리모컨 코디 요청의 의상 번호가 올바르지 않아요.')
    return {slot:value[slot] for slot in SLOTS}

def _error(error):
    # Never publish unexpected exception text: it may include a local path.
    if isinstance(error,(MemoryFailure,ValueError)):
        return str(error)[:500]
    return '게임에 코디를 전달하지 못했어요. 게임 화면과 연결을 확인해 주세요.'

class RemoteRuntime:
    def __init__(self,root,session,catalog=None,bridge=None):
        self.root=Path(root);self.session=Path(session)
        self._owns_catalog=catalog is None and bridge is None
        self.catalog=Catalog(self.root) if self._owns_catalog else catalog
        self.bridge=bridge if bridge is not None else RemoteBridge(self.catalog)
        self._executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='OutfitRemote')
        self._future=None;self._job=None;self._queued=None;self._seen=set()
        self._revision=None;self._key=None;self._result=None;self._result_revision=None
        self._error=None;self._state=dict(ready=False,message='게임 연결 확인 중…')
        self._last_inspect=float('-inf');self._stamp=None;self._closed=False

    def _read_request(self):
        path=self.session/'remote.json';request_key=None
        try:
            info=path.stat();stamp=(info.st_mtime_ns,info.st_ctime_ns,info.st_ino,info.st_size)
            if stamp==self._stamp:return
            self._stamp=stamp
            if not 0<info.st_size<=MAX_REQUEST_BYTES:
                raise ValueError('리모컨 요청 파일 크기가 올바르지 않아요.')
            data=read_json(path)
            if not isinstance(data,dict):raise ValueError('리모컨 요청 형식이 올바르지 않아요.')
            request_key=_revision(data.get('revision'))
            if request_key in self._seen:return
            self._seen.add(request_key);self._revision=data['revision'];self._key=request_key
            self._result=None;self._result_revision=None;self._error=None
            # Even a malformed newer request supersedes a queued old request.
            self._queued=None
            selection=_selection(data.get('selection'))
            self._queued=(request_key,data['revision'],selection)
        except FileNotFoundError:pass
        except (ValueError,UnicodeError):
            if request_key is None:self._key=None;self._revision=None
            self._queued=None;self._result=None;self._result_revision=None
            self._error='리모컨 요청을 읽지 못했어요. 코디를 다시 선택해 전송해 주세요.'
        except OSError:
            self._error='리모컨 요청 파일에 접근하지 못했어요. 잠시 뒤 다시 전송해 주세요.'

    def _collect(self):
        if self._future is None or not self._future.done():return
        future=self._future;job=self._job;self._future=None;self._job=None
        try:
            value=future.result()
            if not isinstance(value,dict):raise RuntimeError('Invalid bridge result')
            if job[0]=='inspect':
                self._state=value
            elif job[1]==self._key:
                self._result=value;self._result_revision=job[2];self._error=None
        except Exception as error:
            if job[0]=='inspect':self._state=dict(ready=False,message=_error(error))
            elif job[1]==self._key:
                self._result=None;self._result_revision=job[2];self._error=_error(error)

    def _schedule(self):
        if self._future is not None or self._closed:return
        if self._queued is not None:
            key,revision,selection=self._queued;self._queued=None
            self._job=('send',key,revision)
            self._future=self._executor.submit(self.bridge.send,selection)
        elif time.monotonic()-self._last_inspect>=1:
            self._last_inspect=time.monotonic();self._job=('inspect',)
            self._future=self._executor.submit(self.bridge.inspect)

    def _snapshot(self):
        pending=not self._closed and bool(self._queued is not None or (self._job and self._job[0]=='send'))
        return dict(revision=self._revision,state=dict(self._state),
                    result=dict(self._result) if self._result is not None else None,
                    result_revision=self._result_revision,error=self._error,
                    pending=pending,busy=pending,closed=self._closed)

    def tick(self):
        if self._closed:return self._snapshot()
        try:
            self._read_request();self._collect();self._schedule()
        except Exception:
            # A broken catalog, IPC file, or worker must not stop QR/video.
            self._error='리모컨 연결이 일시 중단됐어요. 코디를 다시 전송해 주세요.'
        return self._snapshot()

    def close(self):
        if self._closed:return
        self._closed=True;self._queued=None
        self._executor.shutdown(wait=False,cancel_futures=True)
        # An already running send may finish; it is never repeated or started
        # after close. Catalog.close only closes optional art, not item data.
        if self._owns_catalog and self.catalog is not None:self.catalog.close()

    def __enter__(self):return self
    def __exit__(self,*unused):self.close()
