"""Copy only the public character picker metadata and its referenced images."""
from pathlib import Path,PurePosixPath
import argparse,hashlib,json,shutil

CATEGORIES=('face','hair','hairColor','eye','skin','glasses','accent')
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def prepare(source,output):
    source=Path(source).resolve(strict=True);output=Path(output).resolve()
    metadata=source/'data/parts.json';data=json.loads(metadata.read_text(encoding='utf-8'))
    if set(data)!={'version','parts','defaults','defaultOutfit'} or set(data['parts'])!=set(CATEGORIES):raise ValueError('Expected the public character picker catalog.')
    images=set()
    for category in CATEGORIES:
        rows=data['parts'][category]
        if not isinstance(rows,list) or not rows:raise ValueError('Part category is empty.')
        ids=set()
        for row in rows:
            if set(row)-{'id','name','nameJa','image','hkValue','model','supportedFaceModels'}:raise ValueError('Unexpected non-catalog metadata.')
            if type(row.get('id')) is not int or row['id'] in ids or not isinstance(row.get('name'),str) or type(row.get('hkValue')) is not int or not 0<=row['hkValue']<=254:raise ValueError('Invalid public part row.')
            ids.add(row['id']);image=PurePosixPath(row['image'])
            if image.is_absolute() or '..' in image.parts or image.parts[0]!='images' or image.suffix.lower() not in ('.webp','.png') or '\\' in row['image'] or ':' in row['image']:raise ValueError('Only local public thumbnails may be copied.')
            if not (source/str(image)).resolve().is_relative_to(source) or not (source/str(image)).is_file():raise ValueError('Public thumbnail is unavailable.')
            images.add(str(image))
        if data['defaults'].get(category) not in ids:raise ValueError('Default part is absent.')
    if len(data['defaultOutfit'])!=4 or any(type(i) is not int or not 0<=i<=65024 for i in data['defaultOutfit']):raise ValueError('Invalid default outfit.')
    starter='images/new-character-qr.png'
    if not (source/starter).is_file():raise ValueError('User-approved public starter PNG is required.')
    files={'parts.json':metadata,**{name:source/name for name in sorted(images|{starter})}}
    if any((output/name).resolve()==path.resolve() for name,path in files.items()):raise ValueError('Keep source and destination separate.')
    output.mkdir(parents=True,exist_ok=True);trace=[]
    for name,path in files.items():
        target=output/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,target)
        fingerprint=sha(path)
        if sha(target)!=fingerprint:raise ValueError('Copied asset hash mismatch.')
        trace.append({'path':name,'bytes':target.stat().st_size,'sha256':fingerprint})
    proof={'format':'character-public-assets-v1','source_kind':'public-character-picker-catalog','source_paths_recorded':False,
           'category_count':len(CATEGORIES),'part_count':sum(len(data['parts'][category]) for category in CATEGORIES),
           'unique_thumbnail_count':len(images),'starter_qr_classification':'user-approved-existing-public-fixed-starter',
           'starter_contains_fixed_protocol_identity':True,'personal_runtime_profiles_included':False,'files':trace}
    (output/'ASSET_MANIFEST.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return proof
def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--source',required=True,type=Path,help='Public web-app directory containing data/ and images/');parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();proof=prepare(args.source,args.output);print(json.dumps({key:value for key,value in proof.items() if key!='files'}))
if __name__=='__main__':main()
