"""Validate a read-only native-intent proof before reconstructing a lost link.

This is distinct from observing a game WindowMessage. Scene names or an
initialization-once bit alone are never accepted as a request. The provider
owns all version, control-flow and stable-process checks; the coordinator
also matches the helper's independently queried game creation identity.
"""
import re
import time
from concurrent.futures import ThreadPoolExecutor

CHECKS=('code_verified','scene_owner_matches','controller_owner_matches',
        'active_friend_input','no_completion','sdk_link_result',
        'pause_paths_verified','game_identity_stable')

def verified_intent(evidence,game,hwnd):
    if not isinstance(evidence,dict) or not isinstance(game,dict):return False
    if evidence.get('verified') is not True or evidence.get('source')!='native_coordinate_scan_intent':return False
    if evidence.get('game_hwnd')!=hwnd or evidence.get('game_pid')!=game.get('pid') or evidence.get('game_created')!=game.get('created'):return False
    if type(evidence.get('game_pid')) is not int or not isinstance(evidence.get('game_created'),str) or not evidence['game_created']:return False
    if not all(type(evidence.get(key)) is int and 0x10000<=evidence[key]<0x100000000 for key in ('module_base','scene','controller')):return False
    if not isinstance(evidence.get('state_signature'),str) or re.fullmatch(r'[0-9a-f]{64}',evidence['state_signature']) is None:return False
    checks=evidence.get('checks')
    return isinstance(checks,dict) and all(checks.get(key) is True for key in CHECKS)

class ReconnectProof:
    def __init__(self,provider=None):
        self.provider=provider;self.last=dict(state='waiting',reason='실제 게임의 QR 읽기 요청을 기다립니다.')
        self._executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='ReaderProof') if provider is not None else None
        self._future=None;self._future_key=None;self._active_key=None;self._last_started=float('-inf');self._closed=False
    def _probe(self,windows,hwnd):
        try:evidence=self.provider.inspect(windows,hwnd)
        except Exception:evidence=None
        return evidence,time.monotonic()
    def invalidate(self):
        if self._future is not None:self._future.cancel()
        self._future=None;self._future_key=None;self._active_key=None;self._last_started=float('-inf')
        self.last=dict(state='waiting',reason='실제 게임의 QR 읽기 요청을 기다립니다.')
    def inspect(self,windows,hwnd,game):
        if self.provider is None or self._closed or not isinstance(game,dict):return None
        key=(game.get('pid'),game.get('created'),hwnd)
        if self._active_key!=key:self.invalidate();self._active_key=key
        now=time.monotonic()
        if self._future is not None and self._future.done():
            future=self._future;future_key=self._future_key;self._future=None;self._future_key=None
            try:evidence,finished=future.result()
            except Exception:evidence=None;finished=now
            if future_key!=key or not 0<=now-finished<=.5:
                self.last=dict(state='stale',reason='읽기 상태 확인이 오래되어 새 근거를 기다립니다.');return None
            if not verified_intent(evidence,game,hwnd):
                self.last=dict(state='unverified',reason='연결 복구 근거가 충분하지 않습니다. 실제 게임의 읽기 요청을 기다립니다.');return None
            self.last=dict(state='verified',source='verified_native_request_recovery',reason='원게임의 현재 읽기 요청 조건을 확인해 연결을 복구했습니다.')
            return evidence
        if self._future is None and now-self._last_started>=1:
            self._last_started=now;self._future_key=key;self._future=self._executor.submit(self._probe,windows,hwnd)
        return None
    def close(self):
        if self._closed:return
        self.invalidate();self._closed=True
        if self._executor:self._executor.shutdown(wait=False,cancel_futures=True)
