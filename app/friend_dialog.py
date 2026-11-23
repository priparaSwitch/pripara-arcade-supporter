"""Explicit profile/outfit selection for local HK friendship-ticket creation."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QDialogButtonBox,
)
from ticket_browser import identity_key, display_uid, can_generate
from user_picker import UserPicker


class FriendGenerationDialog(QDialog):
    def __init__(self, store, catalog, parent=None, *, profile=None, outfit=None,
                 portrait_service=None,portrait_provider=None):
        super().__init__(parent)
        self.store = store
        self.catalog = catalog
        self.fixed_profile = dict(profile) if profile is not None else None
        self.fixed_outfit = dict(outfit) if outfit is not None else None
        self.selection = None
        self.setWindowTitle('우정티켓 생성하기')
        self.resize(720, 750)
        layout = QVBoxLayout(self)
        description = QLabel('유저 ID·닉네임·생김새를 유지하고 선택한 코디로 우정티켓을 만듭니다.')
        description.setWordWrap(True)
        layout.addWidget(description)
        layout.addWidget(QLabel('사용자'))
        self.users=UserPicker(self,portrait_service=portrait_service,portrait_provider=portrait_provider)
        layout.addWidget(self.users)
        self.user_info = QLabel('')
        self.user_info.setWordWrap(True)
        self.user_info.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.user_info)
        set_row = QHBoxLayout()
        set_row.addWidget(QLabel('의상 세트'))
        self.search = QLineEdit()
        self.search.setPlaceholderText('코디 세트 이름 또는 ID 검색')
        self.search.setClearButtonEnabled(True)
        set_row.addWidget(self.search, 1)
        layout.addLayout(set_row)
        self.sets = QListWidget()
        self.sets.setMinimumHeight(240)
        layout.addWidget(self.sets, 1)
        self.set_info = QLabel('')
        self.set_info.setWordWrap(True)
        self.set_info.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.set_info)
        self.error = QLabel('')
        self.error.setWordWrap(True)
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setStyleSheet('color:#a63245')
        layout.addWidget(self.error)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.create_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.create_button.setText('우정티켓 만들기')
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('취소')
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.users.currentIndexChanged.connect(self.update_selection)
        self.sets.itemSelectionChanged.connect(self.update_selection)
        self.search.textChanged.connect(self.reload_sets)
        self.load_users()
        self.reload_sets()
        if self.fixed_outfit is not None:
            self.search.setEnabled(False)
            self.sets.setEnabled(False)
        self.update_selection()

    def load_users(self):
        try:
            records = [self.fixed_profile] if self.fixed_profile is not None else list(self.store.users())
            if self.fixed_profile is None:
                records = [record for record in records if record.get('can_generate', False)]
                records.sort(key=lambda p: (str(p.get('nickname', '')).casefold(), str(p.get('uid') or identity_key(p) or '')))
            self.users.set_records(records)
        except Exception:
            self.users.set_records([])
            self.error.setText('알고 있는 사용자 목록을 불러오지 못했습니다.')

    def selected_profile(self):
        record = self.users.currentData()
        if not isinstance(record, dict):
            return None
        if self.fixed_profile is not None:
            return dict(self.fixed_profile)
        identifier = identity_key(record)
        if not identifier:
            return None
        try:
            profile = self.store.profile(identifier)
            return dict(profile) if isinstance(profile, dict) else None
        except Exception:
            return None

    def reload_sets(self, *_):
        current = self.sets.currentItem()
        old = current.data(Qt.ItemDataRole.UserRole).get('id') if current else None
        self.sets.blockSignals(True)
        self.sets.clear()
        try:
            records = [self.fixed_outfit] if self.fixed_outfit is not None else list(self.catalog.list_sets(search=self.search.text().strip())) if self.catalog else []
            selected = None
            for record in records:
                label = str(record.get('name') or '이름 없는 코디 세트')
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, dict(record))
                self.sets.addItem(item)
                if record.get('id') == old:
                    selected = item
            if self.sets.count():
                self.sets.setCurrentItem(selected or self.sets.item(0))
        except Exception:
            self.error.setText('코디 세트 목록을 불러오지 못했습니다. data.db를 확인해 주세요.')
        finally:
            self.sets.blockSignals(False)
        self.update_selection()

    def selected_outfit(self):
        item = self.sets.currentItem()
        return dict(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def update_selection(self, *_):
        profile = self.selected_profile()
        outfit = self.selected_outfit()
        if profile:
            appearance = profile.get('appearance')
            parts = profile.get('parts')
            detail = f"{profile.get('nickname') or '이름 없음'} · 유저 ID {display_uid(profile)}"
            if isinstance(parts, dict) and parts:
                labels = [value for value in parts.values() if isinstance(value, str) and value]
                detail += '\n생김새 · ' + ', '.join(labels) if labels else '\n원본 티켓의 생김새를 그대로 사용합니다.'
            elif isinstance(appearance, (list, tuple)):
                detail += '\n생김새 정보 ' + str(len(appearance)) + '개 유지'
            self.user_info.setText(detail)
        else:
            self.user_info.setText('먼저 유저 ID와 프로필이 확인된 티켓을 불러와 주세요.')
        if outfit:
            self.set_info.setText(str(outfit.get('name') or '선택한 코디 세트'))
        else:
            self.set_info.setText('선택할 코디 세트가 없습니다. data.db를 확인해 주세요.')
        permitted = bool(can_generate(profile) and outfit)
        self.create_button.setEnabled(permitted)
        if permitted:
            self.error.clear()
        elif profile and not can_generate(profile):
            self.error.setText('유저 ID 또는 원본 프로필을 확인할 수 없어 우정티켓을 생성하지 않습니다.')

    def accept(self):
        profile = self.selected_profile()
        outfit = self.selected_outfit()
        if not (can_generate(profile) and outfit):
            self.update_selection()
            return
        self.selection = {'profile': profile, 'outfit': outfit}
        super().accept()


# Name retained as a convenient public entry point for callers.
GenerationDialog = FriendGenerationDialog
