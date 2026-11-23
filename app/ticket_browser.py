"""Local ticket browser grouped by verified user identity.

Selection, sorting and indexing never transmit a QR. Only an explicit reader
button, double click or context-menu action emits ``send_image``.
"""
from pathlib import Path
import shutil
import html
from datetime import datetime

from PySide6.QtCore import Qt, Signal, QSize, QTimer, QUrl
from PySide6.QtGui import QIcon, QPixmap, QDesktopServices, QPainter, QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QLineEdit, QListWidget, QListWidgetItem, QSplitter, QMenu,
    QAbstractItemView, QSizePolicy,
    QFileDialog, QInputDialog,
)
from ticket_store import ticket_timestamp, _valid_timestamp

_ALL = '__all_users__'
_UNKNOWN = '__unidentified_tickets__'


def identity_key(record):
    return record.get('identity_key') or record.get('key') or record.get('uid')


def display_uid(record):
    value = record.get('uid')
    return str(value) if value not in (None, '', 0, '000000000000') else 'ID 미확인'


def can_generate(record):
    return bool(record and record.get('decoded', True)
                and record.get('can_generate', False)
                and (record.get('raw') or record.get('template')))


def recent_stamp(record):
    """File timestamps determine recency; internal character IDs never do."""
    printed=_valid_timestamp(record.get('printed_timestamp'))
    if printed:
        return int(printed*1_000_000_000)
    for field in ('mtime_ns', 'source_mtime_ns', 'latest_mtime_ns'):
        try:
            value = int(record.get(field) or 0)
        except (TypeError, ValueError, OverflowError):
            value = 0
        if value:
            return value
    try:
        return int(float(record.get('updated') or 0) * 1_000_000_000)
    except (TypeError, ValueError, OverflowError):
        return 0


def ticket_display_time(record):
    timestamp=_valid_timestamp(record.get('printed_timestamp'))
    if not timestamp:
        timestamp,_source=ticket_timestamp(str(record.get('path') or ''),record.get('mtime_ns',0))
    return datetime.fromtimestamp(timestamp).strftime('%Y/%m/%d %H:%M') if timestamp else '출력 시각 미확인'


def plain_tooltip(text):
    return '<qt>'+html.escape(str(text)).replace('\n','<br/>')+'</qt>'


class _TicketPreview(QLabel):
    """Retain source pixels and rescale when layout/splitter sizing settles."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._source = QPixmap()
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)

    def set_source(self, image):
        self._source = QPixmap(image)
        self._rescale()

    def clear(self):
        self._source = QPixmap()
        super().clear()

    def _rescale(self):
        if not self._source.isNull() and self.contentsRect().size().isValid():
            super().setPixmap(self._source.scaled(self.contentsRect().size(),
                                                  Qt.AspectRatioMode.KeepAspectRatio,
                                                  Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rescale()


class TicketBrowser(QWidget):
    send_image = Signal(str, str)
    generation_requested = Signal(object)
    make_my_requested = Signal(object)
    refresh_requested = Signal()
    trash_changed = Signal(object)
    note_changed = Signal(object)

    def __init__(self, root, kind, store, catalog=None, parent=None, *, portrait_provider=None):
        super().__init__(parent)
        if kind not in ('my', 'tomo'):
            raise ValueError('Invalid ticket kind')
        self.root = Path(root)
        self.kind = kind
        self.store = store
        self.catalog = catalog
        self.portrait_provider = portrait_provider
        self.portrait_status_provider = None
        self.portrait_retry_provider = None
        self._portrait_paths = {}
        self._portrait_errors = {}
        placeholder=QPixmap(56,56);placeholder.fill(Qt.GlobalColor.transparent)
        painter=QPainter(placeholder);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen);painter.setBrush(QColor('#eee7f5'));painter.drawEllipse(0,0,56,56)
        painter.setBrush(QColor('#b5a4ca'));painter.drawEllipse(19,10,18,18);painter.drawRoundedRect(12,32,32,21,13,13);painter.end()
        self._placeholder_icon = QIcon(placeholder)
        self.directory = None
        self.selected_path = None
        self.selected_record = None
        self._reload_guard = False
        self._signature = None
        layout = QVBoxLayout(self)
        heading = QHBoxLayout()
        self.title = QLabel('마이티켓' if kind == 'my' else '우정티켓')
        self.title.setObjectName('title')
        heading.addWidget(self.title)
        self.location = QLabel('유저 ID별로 보관한 티켓을 표시합니다.')
        self.location.setWordWrap(True)
        self.location.setTextFormat(Qt.TextFormat.PlainText)
        heading.addWidget(self.location, 1)
        self.refresh_button = QPushButton('새로고침')
        self.refresh_button.clicked.connect(self.refresh)
        heading.addWidget(self.refresh_button)
        self.folder_button = QPushButton('저장 폴더 열기')
        self.folder_button.clicked.connect(self.open_folder)
        self.folder_button.setEnabled(False)
        heading.addWidget(self.folder_button)
        self.trash_button = QPushButton('휴지통 보기')
        self.trash_button.setObjectName('ticketTrash')
        self.trash_button.clicked.connect(self.open_trash)
        heading.addWidget(self.trash_button)
        layout.addLayout(heading)

        tools = QHBoxLayout()
        tools.addWidget(QLabel('검색'))
        self.search_by = QComboBox()
        self.search_by.addItem('전체', 'all')
        self.search_by.addItem('이름 기준', 'name')
        self.search_by.addItem('코디 명칭 기준', 'coord')
        self.search_by.addItem('메모 기준', 'note')
        tools.addWidget(self.search_by)
        self.search = QLineEdit()
        self.search.setPlaceholderText('검색어를 입력하세요')
        self.search.setClearButtonEnabled(True)
        tools.addWidget(self.search, 1)
        tools.addWidget(QLabel('정렬'))
        self.sort = QComboBox()
        self.sort.addItem('최신 티켓순', 'recent')
        self.sort.addItem('닉네임별', 'name')
        self.sort.addItem('유저 ID별', 'uid')
        self.sort.addItem('코디 ID별 · 상의 기준', 'top_id')
        tools.addWidget(self.sort)
        layout.addLayout(tools)

        portrait_row=QHBoxLayout()
        self.portrait_status=QLabel('얼굴 미리보기 준비 중')
        self.portrait_status.setObjectName('ticketPortraitStatus')
        self.portrait_status.setWordWrap(True)
        self.portrait_status.setTextFormat(Qt.TextFormat.PlainText)
        portrait_row.addWidget(self.portrait_status,1)
        self.portrait_retry=QPushButton('얼굴 다시 만들기')
        self.portrait_retry.setToolTip('만들지 못한 얼굴 미리보기만 다시 시도합니다. QR은 전송하지 않습니다.')
        self.portrait_retry.clicked.connect(self._retry_portraits)
        self.portrait_retry.setEnabled(False)
        portrait_row.addWidget(self.portrait_retry)
        layout.addLayout(portrait_row)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.users = QListWidget()
        self.users.setObjectName('ticketUsers')
        self.users.setMinimumWidth(190)
        self.users.setIconSize(QSize(56,56))
        self.users.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.users.setWordWrap(False)
        self.users.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.users.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.users.customContextMenuRequested.connect(self.user_context_menu)
        self.users.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.splitter.addWidget(self.users)
        self.files = QListWidget()
        self.files.setObjectName('ticketFiles')
        self.files.setMinimumWidth(240)
        self.files.setIconSize(QSize(62, 84))
        self.files.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.files.setWordWrap(False)
        self.files.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.files.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.splitter.addWidget(self.files)
        right = QWidget()
        preview_layout = QVBoxLayout(right)
        self.preview = _TicketPreview('티켓을 선택하세요')
        self.preview.setObjectName('preview')
        self.preview.setMinimumSize(280, 280)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.preview.customContextMenuRequested.connect(self.preview_context_menu)
        preview_layout.addWidget(self.preview, 1)
        self.caption = QLabel('')
        self.caption.setWordWrap(True)
        self.caption.setTextFormat(Qt.TextFormat.PlainText)
        preview_layout.addWidget(self.caption)
        actions = QHBoxLayout()
        self.send_button = QPushButton('QR 리더에 표시')
        self.send_button.clicked.connect(self.send_selected)
        self.send_button.setEnabled(False)
        actions.addWidget(self.send_button)
        self.generate_button = None
        if self.kind == 'tomo':
            self.generate_button = QPushButton('우정티켓 생성하기')
            self.generate_button.clicked.connect(self.generate_selected)
            self.generate_button.setEnabled(False)
            actions.addWidget(self.generate_button)
        preview_layout.addLayout(actions)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 2)
        layout.addWidget(self.splitter, 1)
        self.count = QLabel('')
        self.count.setWordWrap(True)
        self.count.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.count)
        self.error = QLabel('')
        self.error.setWordWrap(True)
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setStyleSheet('color:#a63245')
        layout.addWidget(self.error)

        self.users.currentItemChanged.connect(self._user_changed)
        self.files.itemSelectionChanged.connect(self.show_selected)
        self.files.itemDoubleClicked.connect(lambda _item: self.send_selected())
        self.files.customContextMenuRequested.connect(self.context_menu)
        self.search.textChanged.connect(self.reload)
        self.search_by.currentIndexChanged.connect(self.reload)
        self.sort.currentIndexChanged.connect(self.reload)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.reload)
        self.timer.start(2500)
        self.reload()

    def set_directory(self, directory):
        self.directory = Path(directory) if directory is not None else None
        self.location.setText(str(self.directory) if self.directory else '유저 ID별로 보관한 티켓을 표시합니다.')
        self.folder_button.setEnabled(bool(self.directory))

    def index_directory(self, directory):
        self.set_directory(directory)
        result = self.store.scan(self.directory)
        self.reload()
        return result

    def refresh(self, *_):
        try:
            self.refresh_requested.emit()
            self.reload()
        except Exception:
            self.error.setText('티켓 목록을 갱신하지 못했습니다. 저장 폴더를 확인해 주세요.')

    def _user_changed(self, *_):
        if not self._reload_guard:
            self.reload()

    def _active_user(self):
        item = self.users.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else _ALL

    def _query(self, user):
        query = dict(sort=self.sort.currentData(), search=self.search.text().strip(),
                     search_by=self.search_by.currentData())
        if user not in (_ALL, _UNKNOWN):
            query['uid'] = user
        rows = list(self.store.tickets(self.kind, **query))
        if user == _UNKNOWN:
            rows = [row for row in rows if not row.get('decoded')
                    or str(identity_key(row) or '').startswith('unknown:')]
        return rows

    def set_portrait_provider(self, callback, *, status=None, retry=None):
        if callback is not None and not callable(callback):
            raise TypeError('portrait provider must be callable')
        if status is not None and not callable(status):raise TypeError('portrait status must be callable')
        if retry is not None and not callable(retry):raise TypeError('portrait retry must be callable')
        self.portrait_provider=callback
        self.portrait_status_provider=status
        self.portrait_retry_provider=retry
        self._portrait_paths.clear()
        self._portrait_errors.clear()
        self.reload()

    def _portrait_icon(self, user):
        if self.portrait_provider is None:
            return self._placeholder_icon
        try:
            self._portrait_errors.pop(identity_key(user),None)
            value=self.portrait_provider(dict(user))
            if isinstance(value,QIcon):
                return value if not value.isNull() else self._placeholder_icon
            if isinstance(value,QPixmap):
                return QIcon(value) if not value.isNull() else self._placeholder_icon
            if isinstance(value,(str,Path)):
                path=Path(value);stat=path.stat();signature=(str(path),stat.st_size,stat.st_mtime_ns)
                key=identity_key(user);cached=self._portrait_paths.get(key)
                if cached and cached[0]==signature:return cached[1]
                icon=QIcon(str(path))
                if not icon.isNull():
                    self._portrait_paths[key]=(signature,icon)
                    return icon
        except Exception:
            self._portrait_errors[identity_key(user)]='얼굴 미리보기를 요청하지 못했습니다.'
        return self._placeholder_icon

    def _portrait_state(self,user):
        error=self._portrait_errors.get(identity_key(user))
        if error:return dict(state='failed',reason=error,retryable=bool(self.portrait_retry_provider))
        if self.portrait_provider is None:
            return dict(state='unavailable',reason='얼굴 미리보기를 사용할 수 없습니다.',retryable=False)
        if self.portrait_status_provider:
            try:
                status=self.portrait_status_provider(dict(user))
                if isinstance(status,dict) and status.get('state') in ('ready','pending','queued','failed','unavailable','idle','retrying'):
                    return status
            except Exception:
                return dict(state='failed',reason='얼굴 미리보기 상태를 확인하지 못했습니다.',retryable=False)
        return dict(state='ready' if identity_key(user) in self._portrait_paths else 'pending',reason='',retryable=False)

    def _update_portrait_status(self,users,states):
        self.portrait_retry.setEnabled(bool(self.portrait_retry_provider) and any(state.get('retryable') for state in states))
        if not users:
            self.portrait_status.setText('티켓을 불러오면 얼굴 미리보기를 자동으로 만듭니다.');return
        counts={key:0 for key in ('ready','pending','failed','unavailable')}
        for state in states:
            key=state.get('state','unavailable')
            counts[key if key in counts else 'pending']+=1
        parts=['완료 '+str(counts['ready'])+'/'+str(len(users))]
        if counts['pending']:parts.append('자동 생성 중 '+str(counts['pending']))
        if counts['failed']:parts.append('생성 실패 '+str(counts['failed']))
        if counts['unavailable']:parts.append('자료 확인 필요 '+str(counts['unavailable']))
        text='얼굴 미리보기 · '+' · '.join(parts)
        reasons=list(dict.fromkeys(str(state.get('reason') or '') for state in states if state.get('reason')))
        if reasons:text+='\n'+reasons[0]
        self.portrait_status.setText(text)

    def _retry_portraits(self,*_):
        if not self.portrait_retry_provider:return
        try:users=list(self.store.users(self.kind))
        except Exception:
            self.portrait_status.setText('얼굴 미리보기 목록을 확인하지 못했습니다.');return
        for user in users:
            if not self._portrait_state(user).get('retryable'):continue
            try:
                self._portrait_errors.pop(identity_key(user),None)
                self.portrait_retry_provider(dict(user))
            except Exception:self._portrait_errors[identity_key(user)]='얼굴 미리보기를 다시 요청하지 못했습니다.'
        self.reload()

    def _refresh_user_icons(self, icons):
        for index in range(self.users.count()):
            item=self.users.item(index);icon=icons.get(item.data(Qt.ItemDataRole.UserRole))
            if icon is not None and item.icon().cacheKey()!=icon.cacheKey():item.setIcon(icon)

    def user_menu(self, user):
        menu=QMenu(self)
        profile=self._user_profile(user)
        if self.kind=='tomo':
            action=menu.addAction('이 우정티켓으로 마캐 만들기')
            action.setEnabled(can_generate(profile))
            action.triggered.connect(lambda:self.make_my_requested.emit(dict(profile)))
        action=menu.addAction('우정티켓 생성하기')
        action.setEnabled(can_generate(profile))
        action.triggered.connect(lambda:self.generation_requested.emit(dict(profile)))
        action=menu.addAction('메모하기')
        action.setEnabled(bool(user and user.get('uid') and display_uid(user)!='ID 미확인'))
        action.triggered.connect(lambda:self.edit_user_note(user))
        return menu

    def _user_profile(self, user):
        """Use the latest ticket of this kind, retaining its exact QR bytes."""
        if not isinstance(user,dict) or not identity_key(user):return None
        try:
            for row in self.store.tickets(self.kind,sort='recent',uid=identity_key(user)):
                if can_generate(row):return dict(row)
        except Exception:
            self.error.setText('선택한 사용자의 원본 티켓을 확인하지 못했습니다.')
        return None

    def user_context_menu(self, point):
        item=self.users.itemAt(point)
        if not item:return
        user=item.data(Qt.ItemDataRole.UserRole+1)
        if not isinstance(user,dict):return
        self.users.setCurrentItem(item)
        self.user_menu(user).exec(self.users.viewport().mapToGlobal(point))

    def edit_user_note(self, user):
        uid=user.get('uid') if isinstance(user,dict) else None
        if not uid or display_uid(user)=='ID 미확인':return
        try:
            current=self.store.user_note(uid)
            text,accepted=QInputDialog.getMultiLineText(self,'사용자 메모',
                (user.get('nickname') or '이름 없음')+'의 메모를 입력하세요.\n내용을 비우면 메모를 삭제합니다.',current)
            if not accepted:return
            self.store.set_user_note(uid,text)
        except Exception:
            self.error.setText('메모를 저장하지 못했습니다. 저장 위치를 확인해 주세요.')
            return
        self.reload()
        self.note_changed.emit(dict(uid=uid))

    def edit_ticket_note(self, row):
        if not isinstance(row,dict) or not row.get('id'):return
        try:
            current=self.store.ticket_note(row)
            text,accepted=QInputDialog.getMultiLineText(self,'티켓 메모',
                '선택한 티켓의 메모를 입력하세요.\n내용을 비우면 메모를 삭제합니다.',current)
            if not accepted:return
            self.store.set_ticket_note(row,text)
        except Exception:
            self.error.setText('티켓 메모를 저장하지 못했습니다. 저장 위치를 확인해 주세요.')
            return
        self.reload()
        self.note_changed.emit(dict(uid=row.get('uid'),ticket_id=row['id']))

    def reload(self, *_):
        if self._reload_guard:
            return
        self._reload_guard = True
        try:
            active_user = self._active_user()
            previous = self.selected_path
            all_rows = list(self.store.tickets(self.kind, sort='recent'))
            users = list(self.store.users(self.kind))
            users = [u for u in users if not str(identity_key(u) or '').startswith('unknown:')]
            key = self.sort.currentData()
            if key == 'recent':
                latest = {}
                for row in all_rows:
                    identifier = identity_key(row)
                    latest[identifier] = max(latest.get(identifier, (0, 0)),
                                             (recent_stamp(row), int(row.get('id') or 0)))
                users.sort(key=lambda u: latest.get(identity_key(u), (recent_stamp(u), 0)), reverse=True)
            elif key == 'uid':
                users.sort(key=lambda u: (u.get('uid') is None, str(u.get('uid') or ''), str(u.get('nickname', '')).casefold()))
            else:
                users.sort(key=lambda u: (str(u.get('nickname', '')).casefold(), str(u.get('uid') or identity_key(u) or '')))
            unknown_count = sum(not row.get('decoded') or str(identity_key(row) or '').startswith('unknown:') for row in all_rows)
            entries = [(_ALL, f'전체 사용자 · {len(all_rows)}장', '', None)]
            icons={};portrait_states=[]
            for index, user in enumerate(users, 1):
                nickname=user.get('nickname') or '이름 없음'
                note=str(user.get('note') or '')
                summary=' / '.join(note.splitlines()) if note else '메모'
                label=f"{nickname} * {user.get('count', 0)}장\n✎ {summary}"
                tooltip=plain_tooltip(f"번호 {index}\n유저 ID {display_uid(user)}\n{nickname}\n{user.get('count', 0)}장\n메모\n{note or '메모'}")
                identifier=identity_key(user)
                icons[identifier]=self._portrait_icon(user)
                portrait_state=self._portrait_state(user);portrait_states.append(portrait_state)
                if portrait_state.get('reason'):tooltip+='\n'+html.escape(str(portrait_state['reason']))
                entries.append((identifier,label,tooltip,user))
            self._update_portrait_status(users,portrait_states)
            if unknown_count:
                entries.append((_UNKNOWN, f'ID 미확인 티켓 · {unknown_count}장','',None))
            if active_user not in {entry[0] for entry in entries}:
                active_user = _ALL
            rows = self._query(active_user)
            signature = (active_user, self.search.text(), self.search_by.currentData(), self.sort.currentData(),
                         tuple((entry[0],entry[1],entry[2]) for entry in entries),
                         tuple((r.get('id'), r.get('path'), r.get('uid'), r.get('nickname'), r.get('top_id'),
                                r.get('coord_name'), r.get('decoded'), r.get('can_generate'), r.get('raw'),
                                r.get('ticket_note'),r.get('user_note'),recent_stamp(r)) for r in rows))
            if signature == self._signature:
                self._refresh_user_icons(icons)
                self.error.clear()
                return
            self.users.blockSignals(True)
            self.users.clear()
            selected_user = None
            for identifier, label, tooltip, user in entries:
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, identifier)
                if user is not None:
                    item.setData(Qt.ItemDataRole.UserRole+1,dict(user))
                    item.setIcon(icons[identifier])
                    item.setSizeHint(QSize(190,72))
                    item.setToolTip(tooltip)
                self.users.addItem(item)
                if identifier == active_user:
                    selected_user = item
            self.users.setCurrentItem(selected_user or self.users.item(0))
            self.users.blockSignals(False)
            active_user = self._active_user()
            self.files.blockSignals(True)
            self.files.clear()
            selected = None
            for row in rows:
                nickname = row.get('nickname') or '이름 미확인'
                top_name = row.get('top_name') or '상의 정보 없음'
                moment = ticket_display_time(row)
                note=str(row.get('ticket_note') or '')
                summary=' / '.join(note.splitlines()) if note else '메모'
                item = QListWidgetItem(f'{nickname} * {display_uid(row)}\n{top_name}\n{moment}\n✎ {summary}')
                item.setSizeHint(QSize(240,112))
                item.setData(Qt.ItemDataRole.UserRole, row)
                path = str(row.get('path') or '')
                if path:
                    item.setIcon(QIcon(path))
                item.setToolTip(plain_tooltip(f'{nickname}\n유저 ID {display_uid(row)}\n{top_name}\n{moment}\n메모\n{note or "메모"}\n'+(Path(path).name if path else '파일 없음')))
                self.files.addItem(item)
                if path == previous:
                    selected = item
            self.files.setCurrentItem(selected or self.files.item(0)) if self.files.count() else None
            self.files.blockSignals(False)
            self.error.clear()
            self.count.setText(f"{'마이티켓' if self.kind == 'my' else '우정티켓'} {len(rows)}장 / 전체 {len(all_rows)}장 · 두 번 클릭하면 QR 리더에 표시합니다.")
            self.show_selected()
            self._signature = signature
        except Exception:
            self.files.blockSignals(False)
            self.users.blockSignals(False)
            self.error.setText('티켓 정보를 불러오지 못했습니다. 저장된 파일은 그대로 보존됩니다.')
        finally:
            self._reload_guard = False

    def show_selected(self):
        item = self.files.currentItem()
        row = item.data(Qt.ItemDataRole.UserRole) if item else None
        self.selected_record = row
        self.selected_path = str(row.get('path')) if row and row.get('path') else None
        self.send_button.setEnabled(bool(self.selected_path and Path(self.selected_path).is_file()))
        if self.generate_button is not None:
            self.generate_button.setEnabled(can_generate(row))
        if not row:
            self.preview.clear()
            self.preview.setText('조건에 맞는 티켓이 없습니다')
            self.caption.clear()
            return
        image = QPixmap(self.selected_path) if self.selected_path else QPixmap()
        if image.isNull():
            self.preview.clear()
            self.preview.setText('티켓 이미지 파일을 찾을 수 없습니다')
        else:
            self.preview.set_source(image)
        lines = [f"{row.get('nickname') or '이름 미확인'} · 유저 ID {display_uid(row)}",
                 row.get('coord_name') or '코디 미확인']
        if not row.get('decoded'):
            lines.append('QR 정보를 확인하지 못해 사용자 ID를 임의로 지정하지 않았습니다.')
        elif not can_generate(row) and self.kind == 'tomo':
            lines.append('이 티켓은 신원을 확인할 수 없어 우정티켓 생성에 사용할 수 없습니다.')
        if self.selected_path:
            lines.append('파일 · ' + Path(self.selected_path).name)
        self.caption.setText('\n'.join(lines))

    def send_selected(self, *_):
        if self.selected_path and Path(self.selected_path).is_file():
            self.send_image.emit(self.selected_path, 'lower')

    def generate_selected(self, *_):
        if self.kind == 'tomo' and can_generate(self.selected_record):
            self.generation_requested.emit(dict(self.selected_record))

    def ticket_menu(self, row):
        menu = QMenu(self)
        send = menu.addAction('QR 리더에 표시')
        path = str(row.get('path') or '')
        send.setEnabled(bool(path and Path(path).is_file()))
        send.triggered.connect(lambda: self.send_image.emit(path, 'lower'))
        save = menu.addAction('다른 이름으로 저장')
        save.setEnabled(bool(path and Path(path).is_file()))
        save.triggered.connect(lambda: self.save_ticket_as(row))
        memo=menu.addAction('메모하기')
        memo.setEnabled(bool(row.get('id')))
        memo.triggered.connect(lambda:self.edit_ticket_note(row))
        delete = menu.addAction('삭제')
        delete.setToolTip('원본 티켓을 앱 휴지통으로 옮깁니다. 같은 이미지의 마이티켓·우정티켓은 함께 이동하며 복원할 수 있습니다.')
        delete.setEnabled(bool(path and Path(path).is_file()))
        delete.triggered.connect(lambda: self.trash_ticket(row))
        if self.kind == 'tomo':
            generate = menu.addAction('우정티켓 생성하기')
            generate.setEnabled(can_generate(row))
            if not can_generate(row):
                generate.setToolTip('유저 ID와 원본 프로필을 확인한 우정티켓이 필요합니다.')
            generate.triggered.connect(lambda: self.generation_requested.emit(dict(row)))
        return menu

    def context_menu(self, point):
        item = self.files.itemAt(point)
        if item is None:
            return
        self.files.setCurrentItem(item)
        row = item.data(Qt.ItemDataRole.UserRole)
        self.ticket_menu(row).exec(self.files.viewport().mapToGlobal(point))

    def preview_context_menu(self, point):
        if self.selected_record:
            self.ticket_menu(dict(self.selected_record)).exec(self.preview.mapToGlobal(point))

    def save_ticket_as(self, row):
        path = Path(str(row.get('path') or ''))
        if not path.is_file():
            self.error.setText('저장할 티켓 이미지 파일을 찾을 수 없습니다.')
            return
        destination, _ = QFileDialog.getSaveFileName(
            self, '다른 이름으로 저장', path.name,
            '원본 이미지 (*' + path.suffix + ');;모든 파일 (*)')
        if not destination:
            return
        try:
            target = Path(destination)
            if target.resolve() != path.resolve():
                shutil.copyfile(path, target)
            self.error.clear()
        except OSError:
            self.error.setText('티켓 이미지를 저장하지 못했습니다. 저장 위치를 확인해 주세요.')

    def trash_ticket(self, row):
        try:
            result = self.store.trash(row)
        except Exception:
            self.error.setText('휴지통으로 옮기지 못했습니다. 원본 티켓과 저장 위치를 확인해 주세요.')
            return
        self._signature = None
        self.reload()
        self.trash_changed.emit(dict(operation='trash', kind=self.kind, result=result))

    def open_trash(self, *_):
        from trash_dialog import TrashDialog
        dialog = TrashDialog(self.store, self)
        dialog.changed.connect(self.trash_changed.emit)
        dialog.changed.connect(lambda _result: self.reload())
        dialog.exec()
        dialog.deleteLater()

    def open_folder(self, *_):
        if self.directory is not None and self.directory.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.directory.resolve())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.selected_record:
            self.show_selected()
