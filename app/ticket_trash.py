"""Verified, non-overwriting file moves for the private ticket recycle folder."""
from __future__ import annotations
import hashlib,os,shutil,stat,uuid
from pathlib import Path

class TicketTrashError(OSError):
    pass

class TicketTrash:
    def __init__(self,root):self.root=Path(root).resolve()
    @staticmethod
    def _regular(path):
        path=Path(path)
        for node in (path,*path.parents):
            junction=getattr(node,'is_junction',lambda:False)
            if node.is_symlink() or junction():raise TicketTrashError('연결된 경로의 파일은 휴지통에서 처리할 수 없습니다.')
        information=path.stat()
        if not stat.S_ISREG(information.st_mode):raise TicketTrashError('일반 티켓 파일만 처리할 수 있습니다.')
        return information
    def check_root(self):
        if self.root.is_symlink() or self.root.resolve()!=self.root:raise TicketTrashError('휴지통 경로가 변경되었습니다.')
        for node in self.root.parents:
            if node.is_symlink() or getattr(node,'is_junction',lambda:False)():raise TicketTrashError('연결된 휴지통 경로는 사용할 수 없습니다.')
    def contains(self,path):
        path=Path(path)
        try:return path.resolve().is_relative_to(self.root)
        except (OSError,ValueError):return False
    def destination(self,source):
        self.check_root();self.root.mkdir(parents=True,exist_ok=True)
        return self.root/(uuid.uuid4().hex+Path(source).suffix.lower())
    def validate_entry(self,path,*,exists=True):
        self.check_root();path=Path(path)
        if path.parent!=self.root or path.resolve().parent!=self.root or path.is_symlink():
            raise TicketTrashError('휴지통 밖의 파일은 처리할 수 없습니다.')
        if exists:self._regular(path)
        return path
    @staticmethod
    def free_destination(path):
        path=Path(path)
        if not path.exists() and not path.is_symlink():return path
        for number in range(1,100_000):
            candidate=path.with_name(path.stem+' (복원 '+str(number)+')'+path.suffix)
            if not candidate.exists() and not candidate.is_symlink():return candidate
        raise TicketTrashError('복원할 파일 이름을 만들지 못했습니다.')
    def move(self,source,destination):
        """Cross-volume-safe copy/verify/unlink. Never overwrite either file."""
        source=Path(source);destination=Path(destination);before=self._regular(source)
        if source.resolve()==destination.resolve():raise TicketTrashError('같은 파일로 이동할 수 없습니다.')
        for node in (destination.parent,*destination.parent.parents):
            if node.is_symlink() or getattr(node,'is_junction',lambda:False)():raise TicketTrashError('연결된 경로에는 복원할 수 없습니다.')
        destination.parent.mkdir(parents=True,exist_ok=True)
        created=False
        try:
            original_hash=hashlib.sha256()
            with source.open('rb') as reader,destination.open('xb') as writer:
                created=True
                for block in iter(lambda:reader.read(1024*1024),b''):writer.write(block);original_hash.update(block)
                writer.flush();os.fsync(writer.fileno())
            copied_hash=hashlib.sha256()
            with destination.open('rb') as reader:
                for block in iter(lambda:reader.read(1024*1024),b''):copied_hash.update(block)
            after=self._regular(source)
            if (before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns) or original_hash.digest()!=copied_hash.digest() or destination.stat().st_size!=before.st_size:
                raise TicketTrashError('이동 중 파일이 변경되어 원본을 보존했습니다.')
            shutil.copystat(source,destination,follow_symlinks=False)
            source.unlink()
        except Exception:
            if created:
                try:destination.unlink()
                except OSError:pass
            raise
    def erase(self,path):
        path=self.validate_entry(path,exists=False)
        if path.exists():self._regular(path);path.unlink()
