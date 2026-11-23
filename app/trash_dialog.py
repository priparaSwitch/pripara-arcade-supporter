"""Local, restorable ticket trash; permanent deletion is an explicit action."""
from pathlib import Path
from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidget,
    QListWidgetItem, QSplitter, QWidget, QMessageBox, QSizePolicy,
)


class TrashDialog(QDialog):
    changed = Signal(object)

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.selected_record = None
        self.setWindowTitle('티켓 휴지통')
        self.resize(850, 640)
        layout = QVBoxLayout(self)
        hint = QLabel('삭제한 마이티켓과 우정티켓을 모았습니다. 원하는 티켓을 선택해 복원할 수 있습니다.\n같은 이미지에 포함된 마이티켓·우정티켓은 함께 처리됩니다.')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(self.splitter, 1)
        self.files = QListWidget()
        self.files.setObjectName('trashTickets')
        self.files.setIconSize(QSize(70, 90))
        self.files.setMinimumWidth(260)
        self.splitter.addWidget(self.files)
        right = QWidget()
        preview_layout = QVBoxLayout(right)
        self.preview = QLabel('휴지통이 비어 있습니다')
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(300, 300)
        self.preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        preview_layout.addWidget(self.preview, 1)
        self.caption = QLabel('')
        self.caption.setTextFormat(Qt.TextFormat.PlainText)
        self.caption.setWordWrap(True)
        preview_layout.addWidget(self.caption)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 2)
        self.error = QLabel('')
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setWordWrap(True)
        self.error.setStyleSheet('color:#a63245')
        layout.addWidget(self.error)
        actions = QHBoxLayout()
        self.count = QLabel('')
        actions.addWidget(self.count, 1)
        self.restore_button = QPushButton('복원')
        self.restore_button.setObjectName('trashRestore')
        self.restore_button.clicked.connect(self.restore_selected)
        actions.addWidget(self.restore_button)
        self.empty_button = QPushButton('휴지통 비우기')
        self.empty_button.setObjectName('trashEmpty')
        self.empty_button.setToolTip('휴지통에 있는 티켓 이미지가 영구 삭제됩니다.')
        self.empty_button.clicked.connect(self.empty_trash)
        actions.addWidget(self.empty_button)
        close = QPushButton('닫기')
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        layout.addLayout(actions)
        self.files.currentItemChanged.connect(self.show_selected)
        self.reload()

    @staticmethod
    def image_path(record):
        return str(record.get('trash_path') or record.get('path') or '')

    @staticmethod
    def kind_label(record):
        kinds = record.get('kinds') or [record.get('kind')]
        names = [name for kind, name in (('my', '마이티켓'), ('tomo', '우정티켓')) if kind in kinds]
        return ' · '.join(names) or '티켓'

    def reload(self):
        old = self.selected_record.get('id') if self.selected_record else None
        self.files.blockSignals(True)
        self.files.clear()
        try:
            rows = list(self.store.trash_tickets())
            selected = None
            for row in rows:
                kind = self.kind_label(row)
                uid = row.get('uid') or 'ID 미확인'
                label = f"{row.get('nickname') or '이름 미확인'} · {uid}\n{kind} · {row.get('coord_name') or '코디 미확인'}"
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, dict(row))
                path = self.image_path(row)
                if path:
                    item.setIcon(QIcon(path))
                self.files.addItem(item)
                if row.get('id') == old:
                    selected = item
            if self.files.count():
                self.files.setCurrentItem(selected or self.files.item(0))
            self.count.setText(str(len(rows)) + '장')
            self.empty_button.setEnabled(bool(rows))
            self.error.clear()
        except Exception:
            self.count.setText('')
            self.empty_button.setEnabled(False)
            self.error.setText('휴지통 목록을 불러오지 못했습니다.')
        finally:
            self.files.blockSignals(False)
        self.show_selected()

    def show_selected(self, *_):
        item = self.files.currentItem()
        row = item.data(Qt.ItemDataRole.UserRole) if item else None
        self.selected_record = row
        path = self.image_path(row) if row else ''
        self.restore_button.setEnabled(bool(path and Path(path).is_file()))
        self.preview.clear()
        if not row:
            self.preview.setText('휴지통이 비어 있습니다')
            self.caption.clear()
            return
        image = QPixmap(path)
        if image.isNull():
            self.preview.setText('휴지통의 이미지 파일을 찾을 수 없습니다')
        else:
            self.preview.setPixmap(image.scaled(self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                                Qt.TransformationMode.SmoothTransformation))
        self.caption.setText(f"{row.get('nickname') or '이름 미확인'} · 유저 ID {row.get('uid') or 'ID 미확인'}\n"
                             + (row.get('coord_name') or '코디 미확인'))

    def restore_selected(self, *_):
        if not self.selected_record:
            return
        row = dict(self.selected_record)
        try:
            result = self.store.restore(row)
        except Exception:
            self.error.setText('티켓을 복원하지 못했습니다. 저장 위치와 남은 공간을 확인해 주세요.')
            return
        self.reload()
        self.changed.emit(dict(operation='restore', kind=row.get('kind'), result=result))

    def empty_trash(self, *_):
        if not self.files.count():
            return
        confirm = QMessageBox(self)
        confirm.setWindowTitle('휴지통 비우기')
        confirm.setIcon(QMessageBox.Icon.Warning)
        confirm.setText('휴지통의 모든 티켓 이미지를 영구 삭제합니다.\n같은 이미지에 포함된 마이티켓·우정티켓은 함께 처리됩니다.\n이 작업은 되돌릴 수 없습니다.')
        remove = confirm.addButton('영구 삭제', QMessageBox.ButtonRole.DestructiveRole)
        cancel = confirm.addButton('취소', QMessageBox.ButtonRole.RejectRole)
        confirm.setDefaultButton(cancel)
        confirm.setEscapeButton(cancel)
        confirm.exec()
        if confirm.clickedButton() is not remove:
            return
        try:
            result = self.store.empty_trash()
        except Exception:
            self.error.setText('휴지통을 비우지 못했습니다. 이미지 파일이 사용 중인지 확인해 주세요.')
            return
        self.reload()
        self.changed.emit(dict(operation='empty', kind=None, result=result))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.selected_record:
            self.show_selected()
