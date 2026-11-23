"""Outfit remote embedded in the QR application; the broker owns game access."""
import json
import os
from collections import OrderedDict
from pathlib import Path
import time
from PySide6.QtCore import Qt,QSize,QTimer,Signal,QRect
from PySide6.QtGui import QColor,QIcon,QPainter,QPixmap
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QGridLayout,QLabel,QTabBar,QLineEdit,QComboBox,QPushButton,QCheckBox,QListWidget,QListWidgetItem,QAbstractItemView,QMenu,QStyledItemDelegate,QInputDialog

SLOTS=('top','bottom','shoes','head')
LABELS={'top':'상의 · 원피스','bottom':'하의','shoes':'슈즈','head':'헤어 액세서리'}

class _FavoriteDelegate(QStyledItemDelegate):
    def paint(self,painter,option,index):
        super().paint(painter,option,index)
        entry=index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(entry,dict):return
        owner=self.parent().owner;rect=owner._badge_rect(index)
        painter.save();painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor('#c38d00'));font=painter.font();font.setPointSize(17);font.setBold(True);painter.setFont(font)
        painter.drawText(rect,Qt.AlignmentFlag.AlignCenter,'★' if owner._entry_key(entry) in owner.favorites else '☆');painter.restore()

class _Gallery(QListWidget):
    def __init__(self,owner):
        super().__init__(owner);self.owner=owner;self._wheel=0;self._favorite_down=False
    def wheelEvent(self,event):
        delta=event.angleDelta().y() or event.pixelDelta().y()
        self._wheel+=delta;steps=int(self._wheel/120);self._wheel-=steps*120
        if steps:self.owner.move_page(-steps)
        event.accept()
    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self.owner,'_fit_timer'):self.owner._fit_timer.start()
    def mousePressEvent(self,event):
        index=self.indexAt(event.position().toPoint())
        if event.button()==Qt.MouseButton.LeftButton and index.isValid() and self.owner._badge_rect(index).adjusted(-5,-5,5,5).contains(event.position().toPoint()):
            self._favorite_down=True;self.owner.toggle_favorite(index.data(Qt.ItemDataRole.UserRole));event.accept();return
        self._favorite_down=False;super().mousePressEvent(event)
    def mouseReleaseEvent(self,event):
        if self._favorite_down:self._favorite_down=False;event.accept();return
        super().mouseReleaseEvent(event)
    def mouseDoubleClickEvent(self,event):
        index=self.indexAt(event.position().toPoint())
        if event.button()==Qt.MouseButton.LeftButton and index.isValid() and self.owner._badge_rect(index).adjusted(-5,-5,5,5).contains(event.position().toPoint()):
            self._favorite_down=True;event.accept();return
        super().mouseDoubleClickEvent(event)

class RemoteWidget(QWidget):
    create_friend=Signal(dict)
    def __init__(self,catalog,broker,parent=None):
        super().__init__(parent);self.catalog=catalog;self.broker=broker
        self.outfit={slot:0 for slot in SLOTS};self.ready=False;self.page=0;self.page_size=1
        self.rows=[];self._waiting=None;self._requested_at=0;self._last_result=None
        self._icon_cache=OrderedDict();self._scaled_cache=OrderedDict();self._rows_cache=OrderedDict();self._selection_key=None
        self.metrics=dict(png_decodes=0,scaled_cache_hits=0,icon_builds=0,icon_cache_hits=0,selection_paints=0)
        self._settings_path=Path(catalog.root)/'data/remote_settings.json';self._settings={}
        try:
            saved=json.loads(self._settings_path.read_text(encoding='utf-8'))
            if isinstance(saved,dict):self._settings=saved
        except (OSError,ValueError,TypeError):pass
        self._favorite_path=Path(catalog.root)/'data/remote_favorites.json';self.favorites=set()
        candidates=[self._favorite_path]
        if os.environ.get('LOCALAPPDATA'):candidates.append(Path(os.environ['LOCALAPPDATA'])/'PriParaCoordRemote/favorites.json')
        for candidate in candidates:
            try:
                saved=json.loads(candidate.read_text(encoding='utf-8'))
                if isinstance(saved,list):self.favorites={key for key in saved if isinstance(key,str) and len(key)<100 and key[:1] in ('i','s')}
                break
            except (OSError,ValueError,TypeError):pass
        layout=QVBoxLayout(self);layout.setContentsMargins(12,12,12,12)
        header=QHBoxLayout();layout.addLayout(header)
        title=QLabel('코디 리모컨');title.setStyleSheet('font-size:20px;font-weight:bold;');header.addWidget(title)
        self.connection=QLabel('게임 연결 확인 중…');self.connection.setObjectName('remoteStateLabel');self.connection.setWordWrap(True);self.connection.setFixedHeight(40);header.addWidget(self.connection,1)
        body=QHBoxLayout();layout.addLayout(body,1)
        self.left_panel=left=QWidget();left.setFixedWidth(270);left_layout=QVBoxLayout(left);body.addWidget(left)
        left_layout.addWidget(QLabel('선택한 코디'))
        self.slot_labels={};self.slot_images={};self.clear_buttons={}
        for slot in ('head','top','bottom','shoes'):
            block=QWidget();block.setFixedHeight(82);grid=QGridLayout(block);grid.setContentsMargins(0,3,0,3)
            image=QLabel();image.setFixedSize(58,58);image.setAlignment(Qt.AlignmentFlag.AlignCenter);grid.addWidget(image,0,0,2,1)
            label=QLabel();label.setWordWrap(True);label.setFixedSize(130,46);grid.addWidget(QLabel(LABELS[slot]),0,1);grid.addWidget(label,1,1)
            clear=QPushButton('×');clear.setFixedWidth(30);clear.clicked.connect(lambda checked=False,s=slot:self.clear_slot(s));grid.addWidget(clear,0,2,2,1)
            self.slot_labels[slot]=label;self.slot_images[slot]=image;self.clear_buttons[slot]=clear;left_layout.addWidget(block)
        self.auto_send=QCheckBox('선택 즉시 게임에 입히기');self.auto_send.setObjectName('remoteAutoSend');self.auto_send.setChecked(self._settings.get('auto_send') if type(self._settings.get('auto_send')) is bool else True);left_layout.addWidget(self.auto_send)
        self.auto_set=QCheckBox('승인된 세트 자동 맞춤');self.auto_set.setObjectName('remoteAutoSet');self.auto_set.setChecked(True);left_layout.addWidget(self.auto_set)
        self.send_button=QPushButton('게임에 입히기');self.send_button.setObjectName('remoteSend');self.send_button.clicked.connect(self.send_now);left_layout.addWidget(self.send_button)
        hint=QLabel('게임의 코디 화면에서 사용할 수 있어요.\nDEBUG OFF / ON 모두 지원해요.\n게임 연결이 확인될 때만 전송해요.');hint.setWordWrap(True);left_layout.addWidget(hint)
        self.data_label=QLabel();self.data_label.setWordWrap(True);self.data_label.setFixedHeight(86);left_layout.addWidget(self.data_label);left_layout.addStretch(1)
        right=QWidget();right_layout=QVBoxLayout(right);right_layout.setContentsMargins(0,0,0,0);body.addWidget(right,1)
        self.categories=QTabBar();self.categories.setObjectName('remoteCategoryTabs');self.categories.setExpanding(False);self.categories.setUsesScrollButtons(True)
        for name,key in [('의상세트','sets'),('전체','all'),('헤어 액세서리','head'),('상의 원피스','top'),('하의','bottom'),('슈즈','shoes'),('★ 즐겨찾기','favorites')]:
            index=self.categories.addTab(name);self.categories.setTabData(index,key)
        right_layout.addWidget(self.categories)
        filters=QHBoxLayout();right_layout.addLayout(filters)
        self.search=QLineEdit();self.search.setObjectName('remoteSearch');self.search.setPlaceholderText('의상 이름 · ID 검색');filters.addWidget(self.search,1)
        self.brand=QComboBox();self.brand.setObjectName('remoteBrand');self.brand.addItem('모든 브랜드','')
        for name in sorted({item['brandName'] for item in catalog.items.values() if item['brandName']}):self.brand.addItem(name,name)
        filters.addWidget(self.brand)
        self.sort=QComboBox();self.sort.setObjectName('remoteSort')
        for name,key in [('ID순서','id'),('이름순서','name'),('등급순서','grade')]:self.sort.addItem(name,key)
        filters.addWidget(self.sort)
        self.direction=QPushButton('↑');self.direction.setObjectName('remoteDescending');self.direction.setCheckable(True);self.direction.setFixedWidth(34);filters.addWidget(self.direction)
        self.gallery=_Gallery(self);self.gallery.setObjectName('remoteGallery');self.gallery.setViewMode(QListWidget.ViewMode.IconMode);self.gallery.setResizeMode(QListWidget.ResizeMode.Adjust);self.gallery.setMovement(QListWidget.Movement.Static)
        self.gallery.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection);self.gallery.setIconSize(QSize(112,96));self.gallery.setGridSize(QSize(152,165));self.gallery.setWordWrap(True)
        self.gallery.setMinimumSize(152,165);self.gallery.setSpacing(0);self.gallery.setUniformItemSizes(True)
        self.gallery.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff);self.gallery.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.gallery.setItemDelegate(_FavoriteDelegate(self.gallery))
        self.gallery.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu);self.gallery.customContextMenuRequested.connect(self.context_menu);self.gallery.itemClicked.connect(self.choose_entry);right_layout.addWidget(self.gallery,1)
        foot=QHBoxLayout();right_layout.addLayout(foot)
        self.previous=QPushButton('이전');self.previous.clicked.connect(lambda:self.move_page(-1));foot.addWidget(self.previous)
        self.page_label=QLabel();foot.addWidget(self.page_label)
        self.next=QPushButton('다음');self.next.clicked.connect(lambda:self.move_page(1));foot.addWidget(self.next)
        self.favorite_button=QPushButton('☆ 즐겨찾기 추가/해제');self.favorite_button.clicked.connect(self.toggle_current_favorite);foot.addWidget(self.favorite_button);foot.addStretch(1)
        self.result=QLabel('코디를 고르면 선택 내용을 확인할 수 있어요.');self.result.setObjectName('remoteResultLabel');self.result.setWordWrap(True);self.result.setFixedHeight(44);layout.addWidget(self.result)
        self.debounce=QTimer(self);self.debounce.setSingleShot(True);self.debounce.setInterval(320);self.debounce.timeout.connect(self.send_now)
        self.search_delay=QTimer(self);self.search_delay.setSingleShot(True);self.search_delay.setInterval(170);self.search_delay.timeout.connect(self.filter)
        self.search.textChanged.connect(lambda:self.search_delay.start())
        for control in [self.brand,self.sort]:control.currentIndexChanged.connect(self.filter)
        self.categories.currentChanged.connect(self.filter)
        self.direction.toggled.connect(lambda checked:(self.direction.setText('↓' if checked else '↑'),self.filter()))
        self.auto_send.toggled.connect(self._save_auto_send)
        self._fit_timer=QTimer(self);self._fit_timer.setSingleShot(True);self._fit_timer.setInterval(0);self._fit_timer.timeout.connect(self._fit_page)
        self.timer=QTimer(self);self.timer.setInterval(1000);self.timer.timeout.connect(self.poll_status);self.timer.start()
        self.refresh_selection();self.filter();self.poll_status();self._fit_timer.start()
    def _entry_key(self,entry):return ('s' if entry['kind']=='set' else 'i')+str(entry['record']['id'])
    def _save_auto_send(self,checked):
        self.debounce.stop();self._settings['auto_send']=bool(checked)
        try:
            from ipc_files import atomic_json
            self._settings_path.parent.mkdir(parents=True,exist_ok=True);atomic_json(self._settings_path,self._settings)
        except OSError:self.result.setText('자동 전송 설정을 저장하지 못했어요. 현재 선택은 유지해요.')
    def _fit_page(self):
        width=self.gallery.viewport().width();height=self.gallery.viewport().height()
        grid=self.gallery.gridSize();columns=max(1,width//grid.width());rows=max(1,height//grid.height())
        capacity=columns*rows
        if capacity!=self.page_size:
            first=self.page*self.page_size;self.page_size=capacity;self.page=first//capacity;self.populate()
    def _badge_rect(self,index):
        cell=self.gallery.visualRect(index)
        return QRect(cell.x()+max(4,(cell.width()-112)//2),cell.y()+4,25,25)
    def _scaled(self,identity,width,height):
        key=(identity,width,height)
        if key in self._scaled_cache:
            value=self._scaled_cache.pop(key);self._scaled_cache[key]=value;self.metrics['scaled_cache_hits']+=1;return value
        raw=self.catalog.art_bytes(identity);art=QPixmap();value=None
        if raw:
            self.metrics['png_decodes']+=1
            if art.loadFromData(raw):value=art.scaled(width,height,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation)
        self._scaled_cache[key]=value
        while len(self._scaled_cache)>1024:self._scaled_cache.popitem(last=False)
        return value
    def _icon(self,entry):
        key=self._entry_key(entry)
        if key in self._icon_cache:
            value=self._icon_cache.pop(key);self._icon_cache[key]=value;self.metrics['icon_cache_hits']+=1;return value
        self.metrics['icon_builds']+=1
        record=entry['record'];pixmap=QPixmap(112,96);pixmap.fill(QColor('#ffffff'));painter=QPainter(pixmap)
        ids=[record['pieces'][slot] for slot in ('head','top','bottom','shoes')] if entry['kind']=='set' else [record['id']]
        shown=False
        for index,identity in enumerate(ids):
            if not identity:continue
            if len(ids)==1:rect=(0,0,112,96)
            else:rect=((index%2)*56,(index//2)*48,56,48)
            scaled=self._scaled(identity,rect[2],rect[3])
            if scaled is None:continue
            painter.drawPixmap(rect[0]+(rect[2]-scaled.width())//2,rect[1]+(rect[3]-scaled.height())//2,scaled);shown=True
        if not shown:
            painter.setPen(QColor('#a8a1b7'));font=painter.font();font.setPointSize(28);painter.setFont(font)
            painter.drawText(pixmap.rect(),Qt.AlignmentFlag.AlignCenter,'✦')
        painter.end();value=QIcon(pixmap);self._icon_cache[key]=value
        while len(self._icon_cache)>768:self._icon_cache.popitem(last=False)
        return value
    def filter(self,*unused,preserve_page=False):
        category=self.categories.tabData(self.categories.currentIndex());query=self.search.text().strip();brand=self.brand.currentData() or '';order=self.sort.currentData();descending=self.direction.isChecked()
        options=dict(brand=brand,sort=order,descending=descending)
        cache_key=(category,query,brand,order,descending)
        if category!='favorites' and cache_key in self._rows_cache:
            rows=self._rows_cache.pop(cache_key);self._rows_cache[cache_key]=rows
        elif category=='sets':rows=[dict(kind='set',record=row) for row in self.catalog.list_sets(query,**options)]
        elif category=='favorites':
            rows=[dict(kind='set',record=row) for row in self.catalog.list_sets(query,**options)]+[dict(kind='item',record=row) for row in self.catalog.list_items('all',query,**options)]
            rows=[row for row in rows if self._entry_key(row) in self.favorites]
            if order=='name':key=lambda row:(row['record']['name'],str(row['record']['id']))
            elif order=='grade':key=lambda row:(row['record'].get('grade',0),str(row['record']['id']))
            else:key=lambda row:(row['record']['id'] if row['kind']=='item' else 0x7fffffff,str(row['record']['id']))
            rows.sort(key=key,reverse=descending)
        else:rows=[dict(kind='item',record=row) for row in self.catalog.list_items(category,query,**options)]
        if category!='favorites':
            self._rows_cache[cache_key]=rows
            while len(self._rows_cache)>20:self._rows_cache.popitem(last=False)
        self.rows=rows
        if not preserve_page:self.page=0
        self.populate()
        self._text(self.data_label,(('데이터 '+self.catalog.data_version+' · ') if self.catalog.available else '')+(self.catalog.error or self.catalog.image_status))
    def populate(self):
        pages=max(1,(len(self.rows)+self.page_size-1)//self.page_size);self.page=max(0,min(pages-1,self.page))
        self.gallery.setUpdatesEnabled(False);self.gallery.clear();first=self.page*self.page_size
        for entry in self.rows[first:first+self.page_size]:
            record=entry['record'];details=record['name']+'\n'+record['brandName']
            if entry['kind']=='item':details+='\nID '+str(record['id'])
            item=QListWidgetItem(details);item.setIcon(self._icon(entry));item.setData(Qt.ItemDataRole.UserRole,entry);item.setSizeHint(self.gallery.gridSize())
            item.setToolTip(record['name']+('\nID '+str(record['id']) if entry['kind']=='item' else ''));self.gallery.addItem(item)
        self.gallery.doItemsLayout();self.gallery.setUpdatesEnabled(True)
        self.page_label.setText(str(len(self.rows))+'개 · '+str(self.page+1)+' / '+str(pages))
        self.previous.setEnabled(self.page>0);self.next.setEnabled(self.page+1<pages)
    def move_page(self,change):
        pages=max(1,(len(self.rows)+self.page_size-1)//self.page_size);page=max(0,min(pages-1,self.page+change))
        if page!=self.page:self.page=page;self.populate()
    def choose_entry(self,item):
        entry=item.data(Qt.ItemDataRole.UserRole)
        if entry['kind']=='set':self.choose_set(entry['record'])
        else:self.choose_item(entry['record'])
    def choose_set(self,record):
        self.outfit={slot:int(record['pieces'][slot]) for slot in SLOTS};self.refresh_selection();self.changed()
    def choose_item(self,item):
        candidates=self.catalog.sets_for_item(item['id']) if self.auto_set.isChecked() else []
        if len(candidates)==1:self.outfit=dict(candidates[0]['pieces'])
        else:
            slot=item['category'];self.outfit[slot]=item['id']
            if slot=='top' and item['isDress']:self.outfit['bottom']=0
            elif slot=='bottom' and self.outfit['top'] and self.catalog.items[self.outfit['top']]['isDress']:self.outfit['top']=0
        self.refresh_selection();self.changed()
    def clear_slot(self,slot):
        self.outfit[slot]=0;self.refresh_selection();self.changed()
    def current_outfit(self):return dict(self.outfit)
    @staticmethod
    def _text(label,value):
        if label.text()!=value:label.setText(value)
    def refresh_selection(self):
        key=tuple(self.outfit[slot] for slot in SLOTS)
        if key!=self._selection_key:
            self._selection_key=key;self.metrics['selection_paints']+=1
            for slot in SLOTS:
                identity=self.outfit[slot]
                if identity:
                    item=self.catalog.items[identity];text=item['name']+'\nID '+str(identity)
                    self.slot_labels[slot].setText(text);self.slot_labels[slot].setToolTip(text);art=self._scaled(identity,58,58)
                    if art is not None:self.slot_images[slot].setPixmap(art)
                    else:self.slot_images[slot].clear();self.slot_images[slot].setText('✦')
                else:
                    text='원피스에 포함' if slot=='bottom' and self.outfit['top'] and self.catalog.items[self.outfit['top']]['isDress'] else '선택 안 함'
                    self.slot_labels[slot].setText(text);self.slot_labels[slot].setToolTip(text);self.slot_images[slot].clear();self.slot_images[slot].setText('—')
                self.clear_buttons[slot].setEnabled(bool(identity))
        self.send_button.setEnabled(self.ready and self._waiting is None and any(self.outfit.values()))
    def changed(self):
        self.debounce.stop()
        if self.auto_send.isChecked() and self.ready and any(self.outfit.values()):self.debounce.start()
        else:self.result.setText('코디를 골랐어요. ‘게임에 입히기’를 누르면 전송해요.' if self.ready else '코디를 골랐어요. 게임 연결 후 ‘게임에 입히기’를 눌러 주세요.')
    def send_now(self):
        self.debounce.stop()
        if self._waiting is not None:return
        try:
            selection=self.catalog.validate_outfit(self.outfit)
            if not self.ready:raise ValueError('게임에서 코디 화면을 열어 주세요.')
            if not self.broker:raise ValueError('송출 도우미에 연결되지 않았어요.')
            self._waiting=self.broker.request_remote(selection);self._requested_at=time.monotonic();self._last_result=None
            self.result.setText('게임에 코디를 전달하고 있어요…')
        except (OSError,ValueError,TypeError) as error:self.result.setText(str(error))
        self.refresh_selection()
    def poll_status(self):
        try:
            producer=self.broker.status() if self.broker else {};remote=producer.get('remote',{});state=remote.get('state',remote)
            fresh=bool(producer.get('at')) and -1<=time.time()-float(producer['at'])<3
            self.ready=bool(fresh and state.get('ready'));self._text(self.connection,state.get('message','게임 연결 확인 중…') if fresh else '송출 도우미 연결 대기')
            if self._waiting is not None and remote.get('revision')==self._waiting:
                result=remote.get('result');error=remote.get('error')
                if isinstance(result,dict) and not remote.get('busy'):
                    self.result.setText(result.get('message','코디 전달 결과를 확인해 주세요.'));self._waiting=None
                elif isinstance(error,str) and error:
                    self.result.setText(error);self._waiting=None
            if self._waiting is not None and time.monotonic()-self._requested_at>6:
                self.result.setText('전달 결과가 늦어지고 있어요. 게임을 확인한 뒤 다시 눌러 주세요.');self._waiting=None
            self.refresh_selection()
        except (OSError,ValueError,TypeError,AttributeError):
            self.ready=False;self._text(self.connection,'게임 연결 상태를 읽지 못했어요.');self.refresh_selection()
    def context_menu(self,position):
        item=self.gallery.itemAt(position)
        if not item:return
        entry=item.data(Qt.ItemDataRole.UserRole);menu=QMenu(self)
        generate=menu.addAction('우정티켓 생성하기')
        generate.triggered.connect(lambda checked=False:self.friend_from_entry(entry))
        favorite=menu.addAction('즐겨찾기 해제' if self._entry_key(entry) in self.favorites else '즐겨찾기 추가')
        favorite.triggered.connect(lambda checked=False:self.toggle_favorite(entry));menu.exec(self.gallery.viewport().mapToGlobal(position))
    def friend_from_entry(self,entry):
        if entry['kind']=='set':self.create_friend.emit(entry['record']);return
        item=entry['record'];candidates=self.catalog.sets_for_item(item['id'])
        if len(candidates)==1:self.create_friend.emit(candidates[0]);return
        if candidates:
            labels=[str(index+1)+'. '+record['name'] for index,record in enumerate(candidates)]
            chosen,accepted=QInputDialog.getItem(self,'우정티켓 생성하기','이 의상이 포함된 코디세트를 선택하세요.',labels,0,False)
            if accepted:self.create_friend.emit(candidates[labels.index(chosen)])
            return
        # A catalog item without an approved full set remains exactly that
        # item. Do not change the selected outfit or trigger automatic sending.
        pieces={slot:0 for slot in SLOTS};pieces[item['category']]=item['id']
        self.create_friend.emit(dict(id='item-'+str(item['id']),name=item['name'],pieces=pieces,brandName=item['brandName']))
    def toggle_current_favorite(self):
        item=self.gallery.currentItem()
        if item:self.toggle_favorite(item.data(Qt.ItemDataRole.UserRole))
    def toggle_favorite(self,entry):
        key=self._entry_key(entry)
        if key in self.favorites:self.favorites.remove(key)
        else:self.favorites.add(key)
        try:
            from ipc_files import atomic_json
            self._favorite_path.parent.mkdir(parents=True,exist_ok=True)
            atomic_json(self._favorite_path,sorted(self.favorites))
        except OSError:self.result.setText('즐겨찾기를 저장하지 못했어요. 현재 화면에서는 계속 사용할 수 있어요.')
        if self.categories.tabData(self.categories.currentIndex())=='favorites':self.filter(preserve_page=True)
        else:self.gallery.viewport().update()
    def closeEvent(self,event):
        self.timer.stop();self.debounce.stop();self.search_delay.stop();self._fit_timer.stop();super().closeEvent(event)
