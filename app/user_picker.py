"""Responsive, local-only profile picker with asynchronous face thumbnails."""
from pathlib import Path
import html

from PySide6.QtCore import Qt, QSize, Signal, QTimer, QModelIndex
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QAbstractItemView


def _key(record):
    return record.get('identity_key') or record.get('key') or record.get('uid')


class UserPicker(QListWidget):
    """Each card stores its identity record; duplicate nicknames stay separate."""
    currentIndexChanged=Signal(int)

    def __init__(self,parent=None,*,portrait_service=None,portrait_provider=None):
        super().__init__(parent)
        self.setObjectName('friendUserPicker')
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setFlow(QListWidget.Flow.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setIconSize(QSize(88,88))
        self.setGridSize(QSize(136,132))
        self.setSpacing(4)
        self.setWordWrap(True)
        self.setUniformItemSizes(True)
        self.setMinimumHeight(155)
        self.setMaximumHeight(290)
        self._provider=portrait_provider or (getattr(portrait_service,'request',None) if portrait_service else None)
        self._path_cache={}
        self._placeholder=self._placeholder_icon()
        self._refresh_timer=QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self.refresh_portraits)
        self.currentRowChanged.connect(self.currentIndexChanged.emit)
        if portrait_service is not None:
            signal=getattr(portrait_service,'status_changed',None) or getattr(portrait_service,'ready',None)
            if signal is not None:signal.connect(self.queue_refresh)

    @staticmethod
    def _placeholder_icon():
        pixmap=QPixmap(88,88);pixmap.fill(QColor('#f2edf4'))
        painter=QPainter(pixmap);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen);painter.setBrush(QColor('#c8bacd'))
        painter.drawEllipse(28,13,32,35);painter.drawRoundedRect(16,52,56,36,18,18)
        painter.end()
        return QIcon(pixmap)

    def set_records(self,records):
        selected=_key(self.currentData() or {})
        self.blockSignals(True)
        self.clear()
        target=None
        for record in records:
            record=dict(record)
            item=QListWidgetItem(self._placeholder,str(record.get('nickname') or '이름 없음'))
            item.setData(Qt.ItemDataRole.UserRole,record)
            uid=str(record.get('uid') or 'ID 미확인')
            # Plain text is escaped so a nickname is never treated as HTML.
            item.setToolTip('<qt>'+html.escape(str(record.get('nickname') or '이름 없음'))+'<br/>유저 ID '+html.escape(uid)+'</qt>')
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            self.addItem(item)
            if _key(record)==selected:target=item
        if self.count():self.setCurrentItem(target or self.item(0))
        self.blockSignals(False)
        self.refresh_portraits()
        self.currentIndexChanged.emit(self.currentRow())

    def currentData(self,role=Qt.ItemDataRole.UserRole):
        item=self.currentItem()
        return item.data(role) if item else None

    def itemData(self,index,role=Qt.ItemDataRole.UserRole):
        item=self.item(index)
        return item.data(role) if item else None

    def itemText(self,index):
        item=self.item(index)
        return item.text() if item else ''

    def setCurrentIndex(self,index):
        if isinstance(index,QModelIndex):return super().setCurrentIndex(index)
        self.setCurrentRow(index)

    def queue_refresh(self,*_):
        if not self._refresh_timer.isActive():self._refresh_timer.start(0)

    def refresh_portraits(self):
        if not callable(self._provider):return
        for index in range(self.count()):
            item=self.item(index);record=item.data(Qt.ItemDataRole.UserRole)
            try:
                value=self._provider(dict(record))
                if isinstance(value,QIcon):icon=value
                elif isinstance(value,QPixmap):icon=QIcon(value)
                elif isinstance(value,(str,Path)):
                    path=Path(value);stat=path.stat();signature=(str(path),stat.st_mtime_ns,stat.st_size)
                    cached=self._path_cache.get(_key(record))
                    icon=cached[1] if cached and cached[0]==signature else QIcon(str(path))
                    self._path_cache[_key(record)]=(signature,icon)
                else:continue
                if not icon.isNull():item.setIcon(icon)
            except (OSError,ValueError,TypeError,RuntimeError):
                # The service exposes retry/status elsewhere; selection stays usable.
                pass

    def resizeEvent(self,event):
        super().resizeEvent(event)
        width=max(1,self.viewport().width()-8)
        columns=max(1,width//140)
        self.setGridSize(QSize(max(126,width//columns-8),132))
