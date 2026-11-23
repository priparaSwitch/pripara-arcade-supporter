"""Prepare replaceable suite assets from explicitly supplied local source files.

The native item table is version 0: <uint32 version, uint32 count>, followed
by count records of 144 bytes. Item ID is at +104, its QR auxiliary byte at
+125. No user paths, account defaults, game executables or QR keys are needed.
"""
from __future__ import annotations
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import sys
import tempfile

APPLICATION_ID=0x50435231
SCHEMA_VERSION=1
NATIVE_HEADER=8
NATIVE_STRIDE=144
MAX_RECORDS=1_000_000

class PreparationError(ValueError):
    pass

def digest(path):
    result=hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''):result.update(block)
    return result.hexdigest()

def _source(path,label):
    value=Path(path).expanduser().resolve(strict=True)
    if not value.is_file():raise PreparationError(label+' must be a file.')
    return value

def _json(path,label):
    if path.stat().st_size>16*1024*1024:raise PreparationError(label+' is too large.')
    def pairs(rows):
        value={}
        for key,item in rows:
            if key in value:raise PreparationError(label+' has duplicate object keys.')
            value[key]=item
        return value
    try:value=json.loads(path.read_text(encoding='utf-8'),object_pairs_hook=pairs)
    except (UnicodeError,json.JSONDecodeError) as error:
        raise PreparationError(label+' must contain valid UTF-8 JSON.') from error
    if not isinstance(value,dict):raise PreparationError(label+' must contain an object.')
    return value

def _metadata(parts,name_table):
    part_data=_json(parts,'Parts metadata');groups=part_data.get('parts')
    if not isinstance(groups,dict) or not groups:raise PreparationError('Parts metadata has no part groups.')
    part_count=0
    for records in groups.values():
        if not isinstance(records,dict):raise PreparationError('Parts metadata has an invalid group.')
        for key,record in records.items():
            if not str(key).isdecimal() or not isinstance(record,dict) or not isinstance(record.get('name'),str):
                raise PreparationError('Parts metadata has an invalid part record.')
            part_count+=1
    characters=_json(name_table,'Name table')
    if not characters or any(not str(key).isdecimal() or not isinstance(value,str) or len(value)>32 for key,value in characters.items()):
        raise PreparationError('Name table must map numeric character codes to text.')
    return len(groups),part_count,len(characters)

def _native(path,catalog_ids):
    size=path.stat().st_size
    with path.open('rb') as source:
        header=source.read(NATIVE_HEADER)
        if len(header)!=NATIVE_HEADER:raise PreparationError('Native item table header is truncated.')
        version,count=struct.unpack('<II',header)
        if version!=0:raise PreparationError('Only native item table version 0 is supported.')
        if not 0<count<=MAX_RECORDS or size!=NATIVE_HEADER+count*NATIVE_STRIDE:
            raise PreparationError('Native item table length/count does not match 144-byte records.')
        values={};ambiguous=set();zero_rows=0
        for _ in range(count):
            row=source.read(NATIVE_STRIDE)
            if len(row)!=NATIVE_STRIDE:raise PreparationError('Native item table was truncated while reading.')
            identity=struct.unpack_from('<I',row,104)[0];aux=row[125]
            if not identity:zero_rows+=1;continue
            if identity in values and values[identity]!=aux:ambiguous.add(identity)
            values[identity]=aux
    if catalog_ids&ambiguous:raise PreparationError('Native item table has conflicting aux values for catalog items.')
    missing=catalog_ids-values.keys()
    if missing:raise PreparationError('Native item table is missing '+str(len(missing))+' catalog items.')
    if any(values[identity]>254 for identity in catalog_ids):raise PreparationError('Catalog item aux values must fit the HK QR range 0..254.')
    return {identity:values[identity] for identity in sorted(catalog_ids)},{
        'version':version,'record_count':count,'record_stride':NATIVE_STRIDE,
        'record_start':NATIVE_HEADER,'unique_nonzero_ids':len(values),'zero_id_rows':zero_rows,
        'excluded_non_catalog_ids':len(values.keys()-catalog_ids),'excluded_ambiguous_ids':len(ambiguous-catalog_ids),
    }

def _catalog(db):
    if db.execute('PRAGMA application_id').fetchone()[0]!=APPLICATION_ID or db.execute('PRAGMA user_version').fetchone()[0]!=SCHEMA_VERSION:
        raise PreparationError('Catalog is not a supported PCR1 schema version 1 database.')
    if db.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:raise PreparationError('Catalog integrity check failed.')
    tables={row[0]:row[1] for row in db.execute('SELECT name,type FROM sqlite_master')}
    if any(tables.get(name)!='table' for name in ('meta','items','sets')):raise PreparationError('Catalog is missing required tables.')
    metadata={}
    for key,value in db.execute('SELECT key,value FROM meta'):
        if not isinstance(key,str) or not isinstance(value,str) or key in metadata:raise PreparationError('Catalog metadata must contain unique text keys and values.')
        metadata[key]=value
    if metadata.get('schema_version')!='1':raise PreparationError('Catalog metadata schema version is unsupported.')
    for key in ('item_count','set_count','image_count'):
        if not metadata.get(key,'').isdecimal():raise PreparationError('Catalog count metadata is invalid.')
    ids=[row[0] for row in db.execute('SELECT id FROM items')]
    if not ids or any(type(identity) is not int or identity<=0 for identity in ids) or len(ids)!=len(set(ids)):
        raise PreparationError('Catalog contains missing, invalid or duplicate item IDs.')
    if len(ids)!=int(metadata['item_count']) or db.execute('SELECT COUNT(*) FROM sets').fetchone()[0]!=int(metadata['set_count']):
        raise PreparationError('Catalog item/set counts do not match metadata.')
    return set(ids),metadata

def _enrich(catalog,item_table,target):
    with closing(sqlite3.connect(catalog.as_uri()+'?mode=ro',uri=True)) as source:
        with closing(sqlite3.connect(target)) as db:
            source.backup(db)
            ids,original_metadata=_catalog(db)
            aux,native_proof=_native(item_table,ids)
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='item_meta'").fetchone():
                db.execute('CREATE TABLE item_meta(id INTEGER PRIMARY KEY REFERENCES items(id),grade INTEGER NOT NULL DEFAULT 0,hidden INTEGER NOT NULL DEFAULT 0,aux INTEGER)')
            columns={row[1] for row in db.execute('PRAGMA table_info(item_meta)')}
            if not {'id','grade','hidden'}<=columns:raise PreparationError('Catalog item_meta schema is unsupported.')
            if db.execute('SELECT COUNT(*) FROM item_meta WHERE id NOT IN (SELECT id FROM items) OR id IS NULL').fetchone()[0]:
                raise PreparationError('Catalog item_meta contains orphan rows.')
            if 'aux' not in columns:db.execute('ALTER TABLE item_meta ADD COLUMN aux INTEGER')
            for identity,value in aux.items():
                if db.execute('SELECT 1 FROM item_meta WHERE id=?',(identity,)).fetchone():
                    db.execute('UPDATE item_meta SET aux=? WHERE id=?',(value,identity))
                else:db.execute('INSERT INTO item_meta(id,grade,hidden,aux) VALUES (?,0,0,?)',(identity,value))
            db.commit()
            if db.execute('PRAGMA foreign_key_check').fetchall():raise PreparationError('Prepared catalog failed its foreign key check.')
            output_ids,output_metadata=_catalog(db)
            if output_ids!=ids or output_metadata!=original_metadata:raise PreparationError('Catalog contents or metadata changed unexpectedly.')
            known=db.execute('SELECT COUNT(*) FROM item_meta WHERE aux BETWEEN 0 AND 254').fetchone()[0]
            if known!=len(ids) or db.execute('SELECT COUNT(*) FROM item_meta').fetchone()[0]!=len(ids):
                raise PreparationError('Prepared catalog aux coverage is incomplete.')
            db.execute('PRAGMA journal_mode=DELETE').fetchone()
            return {'item_count':len(ids),'set_count':int(original_metadata['set_count']),'known_aux_count':known,
                    'unknown_aux_count':0,'orphan_aux_count':0,'integrity_check':'ok','foreign_key_check':'ok'},native_proof

def prepare(catalog,images,item_table,parts,name_table,output):
    """Build assets in a staging directory, then replace only named output files."""
    sources={'catalog':_source(catalog,'Catalog'),'item_table':_source(item_table,'Native item table'),
             'parts':_source(parts,'Parts metadata'),'name_table':_source(name_table,'Name table')}
    if images is not None:sources['images']=_source(images,'Image archive')
    if any(Path(str(sources['catalog'])+suffix).exists() for suffix in ('-wal','-shm','-journal')):
        raise PreparationError('Source catalog has SQLite sidecar files; close/checkpoint it before preparing reproducible assets.')
    output=Path(output).expanduser().resolve()
    targets=[output/'data.db',output/'app/ticket_assets/parts.json',output/'app/ticket_assets/name_characters.json',output/'suite-data-proof.json']
    if images is not None:targets.append(output/'images.bin')
    if any(target.resolve() in sources.values() for target in targets):raise PreparationError('Output files must be separate from all source files.')
    if any((output/('data.db'+suffix)).exists() for suffix in ('-wal','-shm','-journal')):
        raise PreparationError('Output catalog has SQLite sidecar files; close its database or choose another output directory.')
    if images is None and (output/'images.bin').exists():
        raise PreparationError('Output already has images.bin; supply --images explicitly or use another output directory for a lightweight build.')
    fingerprints={name:{'sha256':digest(path),'bytes':path.stat().st_size} for name,path in sources.items()}
    group_count,part_count,character_count=_metadata(sources['parts'],sources['name_table'])
    output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.suite-data-',dir=output) as staging:
        stage=Path(staging);assets=stage/'app/ticket_assets';assets.mkdir(parents=True)
        catalog_proof,native_proof=_enrich(sources['catalog'],sources['item_table'],stage/'data.db')
        shutil.copyfile(sources['parts'],assets/'parts.json');shutil.copyfile(sources['name_table'],assets/'name_characters.json')
        if images is not None:shutil.copyfile(sources['images'],stage/'images.bin')
        for name,path in sources.items():
            if digest(path)!=fingerprints[name]['sha256']:raise PreparationError('A source file changed during preparation; no output files were replaced.')
        relative=['data.db','app/ticket_assets/parts.json','app/ticket_assets/name_characters.json']
        if images is not None:relative.append('images.bin')
        artifacts={name:{'sha256':digest(stage/name),'bytes':(stage/name).stat().st_size} for name in relative}
        proof={'format':'pripara-suite-data-v1','sources':fingerprints,'artifacts':artifacts,'catalog':catalog_proof,
               'native_table':native_proof,'metadata':{'part_groups':group_count,'part_count':part_count,'name_character_count':character_count},
               'images_included':images is not None,'source_files_modified':False,'paths_recorded':False}
        (stage/'suite-data-proof.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2,sort_keys=True)+'\n',encoding='utf-8')
        for name in relative+['suite-data-proof.json']:
            target=output/name;target.parent.mkdir(parents=True,exist_ok=True);os.replace(stage/name,target)
    return proof

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog',required=True,help='Existing PCR1 data.db source')
    parser.add_argument('--images',help='Optional images.bin source; omitted for a lightweight build')
    parser.add_argument('--item-table',required=True,help='Native version-0 item table source')
    parser.add_argument('--parts',required=True,help='Character-part names JSON source')
    parser.add_argument('--name-table',required=True,help='Native name-character JSON source')
    parser.add_argument('--output',required=True,help='Asset destination, kept separate from sources')
    args=parser.parse_args(argv)
    try:proof=prepare(args.catalog,args.images,args.item_table,args.parts,args.name_table,args.output)
    except (PreparationError,OSError,sqlite3.Error) as error:
        # Do not serialize machine-specific exception filenames into public logs.
        message=str(error) if isinstance(error,PreparationError) else 'A local file or database operation failed.'
        parser.exit(2,'Data preparation failed: '+message+'\n')
    print(json.dumps({'prepared':True,'items':proof['catalog']['item_count'],'known_aux':proof['catalog']['known_aux_count'],
                      'images_included':proof['images_included'],'catalog_sha256':proof['artifacts']['data.db']['sha256']},sort_keys=True))
    return 0

if __name__=='__main__':sys.exit(main())
