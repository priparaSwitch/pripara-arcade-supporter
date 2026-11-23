"""Native Qt character picker; creation/preview never transmits a QR."""
from pathlib import Path
from PySide6.QtCore import Qt,QSize,Signal,QPoint
from PySide6.QtGui import QIcon,QPixmap,QColor,QPen,QWheelEvent,QPainter
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,QGroupBox,QLineEdit,QLabel,
    QPushButton,QTabBar,QListWidget,QListWidgetItem,QAbstractItemView,QScrollArea,QSplitter,QDialog,
    QDialogButtonBox,QFileDialog,QApplication,QStyledItemDelegate,QStyleOptionViewItem,QStyle)
from character_creator import CharacterCreator,CATEGORIES,parse_likes,normalize_player_code
from ipc_files import atomic_bytes
RANDOM_CODE_NOTE='임의생성 · 12자리 표시(앞자리 0 포함)\n게임에서 유지되는 번호 범위를 사용해요.'

class _PartsDelegate(QStyledItemDelegate):
    def __init__(self,owner):super().__init__(owner.part_list);self.owner=owner
    def paint(self,painter,option,index):
        clean=QStyleOptionViewItem(option)
        clean.state &= ~(QStyle.StateFlag.State_Selected|QStyle.StateFlag.State_HasFocus|QStyle.StateFlag.State_MouseOver)
        super().paint(painter,clean,index)
        if index.data(Qt.ItemDataRole.UserRole)==self.owner.creator.parts[self.owner.active]:
            painter.save();painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor('#2bc4d5'),2));painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(option.rect.adjusted(3,3,-3,-3),12,12);painter.restore()

class _FormScroll(QScrollArea):
    """Scale this form's wheel travel, including precision-trackpad deltas."""
    def wheelEvent(self,event):
        angle=event.angleDelta();pixel=event.pixelDelta()
        scaled=QWheelEvent(event.position(),event.globalPosition(),QPoint(round(pixel.x()*.6),round(pixel.y()*.6)),
                           QPoint(round(angle.x()*.6),round(angle.y()*.6)),event.buttons(),event.modifiers(),event.phase(),event.inverted())
        super().wheelEvent(scaled);event.setAccepted(scaled.isAccepted())

class CharacterWidget(QWidget):
    load_qr=Signal(str)
    profile_created=Signal(dict)
    portrait_requested=Signal(dict)
    def __init__(self,root,catalog=None,parent=None):
        super().__init__(parent);self.setObjectName('characterCreator');self.root=Path(root);self.creator=None;self.active='face';self._icons={};self._portrait_path=None
        layout=QVBoxLayout(self);layout.setContentsMargins(12,12,12,12)
        try:self.creator=CharacterCreator(root,catalog)
        except (OSError,ValueError,KeyError,TypeError):
            label=QLabel('캐릭터 생성용 파츠 자료를 불러오지 못했어요. 앱 자료를 다시 확인해 주세요.');label.setWordWrap(True);layout.addWidget(label);return
        heading=QLabel('마이캐릭터 만들기');heading.setStyleSheet('font-size:20px;font-weight:700;');layout.addWidget(heading)
        split=QSplitter(Qt.Orientation.Horizontal);layout.addWidget(split,1)
        form_widget=QWidget();form=QVBoxLayout(form_widget);form.setContentsMargins(8,18,8,18)
        fields=QFormLayout()
        self.code_input=QLineEdit(self.creator.identity['playerCode']);self.code_input.setObjectName('characterPlayerCode');self.code_input.setMaxLength(12);self.code_input.setPlaceholderText('플레이어 코드')
        self.random_button=QPushButton('임의생성');code_row=QHBoxLayout();code_row.addWidget(self.code_input,1);code_row.addWidget(self.random_button)
        fields.addRow('플레이어 코드',code_row)
        self.mode_label=QLabel(RANDOM_CODE_NOTE);self.mode_label.setWordWrap(True);fields.addRow(self.mode_label)
        self.name_input=QLineEdit(self.creator.name);self.name_input.setObjectName('characterName');self.name_input.setMaxLength(6);self.name_input.setPlaceholderText('이름 1~6자');fields.addRow('캐릭터 이름',self.name_input)
        special=QHBoxLayout();self.name_count=QLabel(str(len(self.creator.name))+' / 6');special.addWidget(self.name_count);special.addStretch(1)
        self.special_buttons={}
        for character in ('★','☆','♪'):
            button=QPushButton(character);button.setFixedWidth(34);button.clicked.connect(lambda checked=False,char=character:self._insert_character(char));special.addWidget(button);self.special_buttons[character]=button
        fields.addRow(special);hint=QLabel('영문 대·소문자, 숫자, ! ? ★ ☆ ♪');hint.setWordWrap(True);fields.addRow(hint);form.addLayout(fields)
        self.selected_buttons={}
        for category,label in CATEGORIES:
            button=QPushButton();button.setMinimumHeight(48);button.setIconSize(QSize(36,36));button.clicked.connect(lambda checked=False,key=category:self._show_category(key));form.addWidget(button);self.selected_buttons[category]=button
        likes_form=QFormLayout();self.likes_input=QLineEdit(str(self.creator.likes));self.likes_input.setObjectName('characterLikes');self.likes_input.setMaxLength(10);likes_form.addRow('좋아요♥ 입력',self.likes_input);form.addLayout(likes_form)
        form.addWidget(QLabel('의상은 기본 레슨 코디로 만들어요.'))
        self.generate_button=QPushButton('홍콩판 QR 만들기');self.generate_button.setObjectName('generateCharacter');self.generate_button.setMinimumHeight(42);form.addWidget(self.generate_button)
        self.fresh_button=QPushButton('처음부터 마이캐릭터 생성하기');self.fresh_button.setObjectName('startFreshCharacter');form.addWidget(self.fresh_button)
        note=QLabel('이름·파츠·좋아요는 이 PC에 저장돼요. 플레이어 코드는 저장하지 않아 다음 실행은 새 번호로 시작해요.');note.setWordWrap(True);form.addWidget(note);form.addStretch(1)
        self.form_scroll=scroll=_FormScroll();scroll.setWidgetResizable(True);scroll.setWidget(form_widget);scroll.setMinimumWidth(290);scroll.setMaximumWidth(370);split.addWidget(scroll)
        content=QWidget();right=QVBoxLayout(content);right.setContentsMargins(10,18,0,18)
        self.tabs=QTabBar();self.tabs.setObjectName('characterPartCategories');self.tabs.setExpanding(False);self.tabs.setUsesScrollButtons(True)
        for _,label in CATEGORIES:self.tabs.addTab(label)
        right.addWidget(self.tabs)
        search_row=QHBoxLayout();self.search=QLineEdit();self.search.setObjectName('characterPartSearch');self.search.setPlaceholderText('파츠 이름·일본어·번호 검색');self.count=QLabel();search_row.addWidget(self.search,1);search_row.addWidget(self.count);right.addLayout(search_row)
        self.part_list=QListWidget();self.part_list.setObjectName('characterParts');self.part_list.setViewMode(QListWidget.ViewMode.IconMode);self.part_list.setResizeMode(QListWidget.ResizeMode.Adjust);self.part_list.setMovement(QListWidget.Movement.Static);self.part_list.setIconSize(QSize(90,90));self.part_list.setGridSize(QSize(150,150));self.part_list.setWordWrap(True);self.part_list.setSpacing(5);self.part_list.setMinimumHeight(240);self.part_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.part_list.setDragEnabled(False);self.part_list.setAcceptDrops(False);self.part_list.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop);self.part_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.part_list.setItemDelegate(_PartsDelegate(self));self.part_list.setViewportMargins(0,16,0,16);right.addWidget(self.part_list,1)
        output=QHBoxLayout();qr_box=QGroupBox('마이캐릭터 QR');qr_layout=QHBoxLayout(qr_box)
        self.qr_image=QLabel('파츠를 고른 뒤\nQR 만들기를 눌러 주세요.');self.qr_image.setAlignment(Qt.AlignmentFlag.AlignCenter);self.qr_image.setFixedSize(210,210);qr_layout.addWidget(self.qr_image)
        actions=QVBoxLayout();self.output_info=QLabel('이름·파츠·좋아요로 로그인용 QR을 만들어요.');self.output_info.setWordWrap(True);actions.addWidget(self.output_info)
        self.save_button=QPushButton('PNG 저장');self.copy_button=QPushButton('이미지 복사');self.load_button=QPushButton('QR 리더로 불러오기');self.load_button.setObjectName('loadCharacterQr')
        for button in (self.save_button,self.copy_button,self.load_button):button.setEnabled(False);actions.addWidget(button)
        actions.addStretch(1);qr_layout.addLayout(actions,1);output.addWidget(qr_box,2)
        portrait_box=QGroupBox('캐릭터 미리보기');portrait_layout=QVBoxLayout(portrait_box)
        self.portrait_image=QLabel('선택한 파츠로\n캐릭터를 미리 볼 수 있어요.');self.portrait_image.setObjectName('characterPortrait');self.portrait_image.setAlignment(Qt.AlignmentFlag.AlignCenter);self.portrait_image.setMinimumSize(135,145);self.portrait_image.setMaximumHeight(175);self.portrait_image.setWordWrap(True);portrait_layout.addWidget(self.portrait_image,1)
        self.portrait_button=QPushButton('캐릭터 미리보기');self.portrait_button.setObjectName('previewCharacter');self.portrait_button.clicked.connect(self._request_preview);portrait_layout.addWidget(self.portrait_button);output.addWidget(portrait_box,1);right.addLayout(output)
        self.status_label=QLabel('생성·미리보기는 자동으로 게임에 보내지 않아요.');self.status_label.setWordWrap(True);right.addWidget(self.status_label);split.addWidget(content);split.setStretchFactor(1,1);split.setSizes([310,900])
        self.name_input.textChanged.connect(self._edited);self.likes_input.textChanged.connect(self._edited);self.code_input.textChanged.connect(self._code_edited);self.code_input.editingFinished.connect(self._normalize_code)
        self.random_button.clicked.connect(self._random_code);self.tabs.currentChanged.connect(self._category_changed);self.search.textChanged.connect(self._render_grid);self.part_list.itemClicked.connect(self._choose)
        self.generate_button.clicked.connect(self._generate);self.fresh_button.clicked.connect(self._fresh);self.copy_button.clicked.connect(self._copy);self.save_button.clicked.connect(self._save);self.load_button.clicked.connect(self._load)
        self._refresh_summary();self._render_grid()
    def _icon(self,row):
        path=self.creator.catalog.image(row)
        if not path:return QIcon()
        key=str(path)
        if key not in self._icons:self._icons[key]=QIcon(str(path))
        return self._icons[key]
    def _refresh_summary(self):
        for category,label in CATEGORIES:
            row=self.creator.catalog.row(self.creator.parts,category);button=self.selected_buttons[category];button.setText(label+'  ·  '+row['name']);button.setIcon(self._icon(row));button.setToolTip(row.get('nameJa',''))
    def _render_grid(self,*unused):
        if not self.creator:return
        query=self.search.text().strip().casefold();rows=self.creator.catalog.parts[self.active];self.part_list.clear();face=self.creator.catalog.row(self.creator.parts,'face');visible=0
        for row in rows:
            if query and query not in (row['name']+' '+row.get('nameJa','')+' '+str(row['id'])).casefold():continue
            compatible=self.active!='eye' or self.creator.catalog.compatible(row,face)
            text=row['name']+'\nID '+str(row['id'])
            item=QListWidgetItem(self._icon(row),text);item.setData(Qt.ItemDataRole.UserRole,row['id']);item.setToolTip(row.get('nameJa','')+('' if compatible else '\n현재 눈매에서는 사용할 수 없어요.'))
            if not compatible:item.setFlags(item.flags()&~Qt.ItemFlag.ItemIsEnabled)
            self.part_list.addItem(item)
            visible+=1
        self.count.setText(str(visible)+' / '+str(len(rows))+'개')
    def _show_category(self,category):self.tabs.setCurrentIndex([key for key,_ in CATEGORIES].index(category))
    def _category_changed(self,index):
        self.active=CATEGORIES[index][0];self.search.clear();self._render_grid()
    def _choose(self,item):
        try:
            changed=self.creator.select(self.active,item.data(Qt.ItemDataRole.UserRole));self._invalidate();self._refresh_summary();self.part_list.viewport().update();self._persist()
            self.status_label.setText('눈매에 맞는 아이컬러로 함께 변경했어요.' if changed else '파츠를 선택했어요.')
        except ValueError as error:self.status_label.setText(str(error))
    def _invalidate(self):
        self.creator.output=None;self.qr_image.clear();self.qr_image.setText('파츠를 고른 뒤\nQR 만들기를 눌러 주세요.');self.output_info.setText('이름·파츠·좋아요로 로그인용 QR을 만들어요.')
        for button in (self.save_button,self.copy_button,self.load_button):button.setEnabled(False)
        self.portrait_image.clear();self.portrait_image.setText('선택한 파츠로\n캐릭터를 미리 볼 수 있어요.');self._portrait_path=None
    def _persist(self):
        try:self.creator.save_settings()
        except OSError:self.status_label.setText('설정을 저장하지 못했어요. 만든 QR을 따로 보관해 주세요.')
    def _edited(self,*unused):
        self.creator.name=self.name_input.text();self.name_count.setText(str(len(self.creator.name))+' / 6');self._invalidate()
        try:self.creator.likes=parse_likes(self.likes_input.text());self._persist();self.status_label.setText('설정은 이 PC에만 저장돼요.')
        except ValueError as error:self.status_label.setText(str(error))
    def _code_edited(self,text):
        self.creator.set_code(text);self.mode_label.setText('직접 입력한 번호를 그대로 사용해요.');self._invalidate()
    def _normalize_code(self):
        try:
            value=normalize_player_code(self.code_input.text());self.code_input.blockSignals(True);self.code_input.setText(value);self.code_input.blockSignals(False);self.creator.identity['playerCode']=value
        except ValueError as error:self.status_label.setText(str(error))
    def _random_code(self):
        self.code_input.blockSignals(True);self.code_input.setText(self.creator.randomize_code());self.code_input.blockSignals(False);self.mode_label.setText(RANDOM_CODE_NOTE);self._invalidate()
    def _insert_character(self,character):self.name_input.insert(character);self.name_input.setFocus()
    def _generate(self):
        self.generate_button.setEnabled(False)
        try:
            self.creator.name=self.name_input.text();self.creator.likes=parse_likes(self.likes_input.text());self.creator.identity['playerCode']=self.code_input.text()
            result=self.creator.generate();self.code_input.blockSignals(True);self.code_input.setText(self.creator.identity['playerCode']);self.code_input.blockSignals(False)
            self.qr_image.setPixmap(QPixmap(result['path']).scaled(210,210,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.FastTransformation))
            profile=result['profile'];self.output_info.setText(profile['nickname']+'\n플레이어 코드 '+profile['uid']+'\n좋아요 '+format(profile['likes'],','))
            for button in (self.save_button,self.copy_button,self.load_button):button.setEnabled(True)
            self.status_label.setText('QR이 완성됐어요. 게임으로 보내려면 QR 리더로 불러오기를 눌러 주세요.');self.profile_created.emit(self.current_profile())
        except (OSError,ValueError,RuntimeError) as error:self.status_label.setText(str(error) or 'QR을 만들지 못했어요.')
        finally:self.generate_button.setEnabled(True)
    def _copy(self):
        if self.creator.output:QApplication.clipboard().setPixmap(QPixmap(self.creator.output['path']));self.status_label.setText('QR 이미지를 복사했어요.')
    def _save(self):
        if not self.creator.output:return
        path,_=QFileDialog.getSaveFileName(self,'마이캐릭터 QR 저장','mycharacter.png','PNG 이미지 (*.png)')
        if not path:return
        try:
            target=Path(path)
            if target.suffix.lower()!='.png':target=target.with_suffix('.png')
            atomic_bytes(target,Path(self.creator.output['path']).read_bytes());self.status_label.setText('PNG를 저장했어요.')
        except OSError:self.status_label.setText('PNG를 저장하지 못했어요.')
    def _load(self):
        if self.creator.output:self.load_qr.emit(self.creator.output['path'])
    def _fresh(self):
        dialog=QDialog(self);dialog.setObjectName('freshCharacterDialog');dialog.setWindowTitle('처음부터 마이캐릭터 생성하기');layout=QVBoxLayout(dialog)
        text=QLabel('처음부터 하려면 이 QR코드를 사용해주세요');layout.addWidget(text)
        pixmap=QPixmap(str(self.creator.catalog.root/'images/new-character-qr.png'))
        if pixmap.isNull():self.status_label.setText('시작용 QR 이미지를 불러오지 못했어요. 앱 자료를 확인해 주세요.');return
        picture=QLabel();picture.setAlignment(Qt.AlignmentFlag.AlignCenter);picture.setPixmap(pixmap.scaled(360,360,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.FastTransformation));layout.addWidget(picture)
        notice=QLabel('기존 웹앱에서 제공한 공개 고정 시작용 QR이에요. 현재 편집 중인 캐릭터를 지우거나 게임으로 전송하지 않아요.');notice.setWordWrap(True);layout.addWidget(notice)
        buttons=QDialogButtonBox();copy=buttons.addButton('클립보드로 복사하기',QDialogButtonBox.ButtonRole.ActionRole);close=buttons.addButton('닫기',QDialogButtonBox.ButtonRole.RejectRole);copy.clicked.connect(lambda:QApplication.clipboard().setPixmap(pixmap));close.clicked.connect(dialog.reject);layout.addWidget(buttons);dialog.exec()
    def current_profile(self):
        if not self.creator:return {}
        # Snapshot the actual visible form, even when a caller changed fields
        # with signals blocked. Preview never requires generating a QR first.
        self.creator.name=self.name_input.text();self.creator.identity['playerCode']=self.code_input.text()
        try:self.creator.likes=parse_likes(self.likes_input.text())
        except ValueError:pass  # Invalid likes only prevents QR generation.
        return self.creator.current_profile()
    def _request_preview(self):
        try:
            profile=self.current_profile()
            if not profile:return
            self.set_portrait_status('캐릭터 미리보기 생성 중…');self.portrait_requested.emit(profile)
        except (OSError,ValueError,KeyError,TypeError,StopIteration):self.set_portrait_status('선택한 파츠의 미리보기 자료를 확인해 주세요.')
    def save_settings(self):
        if not self.creator:return False
        try:self.creator.save_settings();return True
        except OSError:
            self.status_label.setText('설정을 저장하지 못했어요. 만든 QR을 따로 보관해 주세요.');return False
    def settings(self):return self.creator.settings() if self.creator else {}
    def set_portrait_image(self,path):
        self._portrait_path=str(path);pixmap=QPixmap(str(path))
        if not pixmap.isNull():self.portrait_image.setPixmap(pixmap.scaled(180,175,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))
    def set_portrait_status(self,text):
        if self.creator:self.portrait_image.setText(str(text))
