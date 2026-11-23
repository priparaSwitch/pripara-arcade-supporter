"""Recover a captured ticket's printed item from verified local provenance."""
import hashlib,json,re,sqlite3
from pathlib import Path
from PIL import Image

def captured_item(root,entry,image):
    if not isinstance(entry,dict) or image is None:return None
    name=Path(str(entry.get('name',''))).name
    match=re.fullmatch(r'myticket_(\d{4}-\d\d-\d\d_\d{6})(_my)?\.png',name)
    if not match:return None
    try:
        from ticket_widgets import settings
        _,directory=settings(root);candidate=directory/name
        with Image.open(candidate) as known:
            # A filename alone is not proof. Renamed or edited images must not
            # silently get another ticket's printed item.
            if known.size!=image.size:return None
            if hashlib.sha256(known.convert('RGBA').tobytes()).digest()!=hashlib.sha256(image.convert('RGBA').tobytes()).digest():return None
        database=directory/'.ticket_capture.sqlite3'
        if not database.is_file():return None
        connection=sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)
        try:row=connection.execute("SELECT details FROM captures WHERE stamp=? AND state='complete'",(match[1],)).fetchone()
        finally:connection.close()
        if not row:return None
        source=Path(json.loads(row[0])['source']).name
        item=re.fullmatch(r'itemid_(\d+)\.bmp',source,re.IGNORECASE)
        value=int(item[1]) if item else 0
        return value if 0<value<255**2 else None
    except (OSError,ValueError,TypeError,KeyError,sqlite3.Error):return None
