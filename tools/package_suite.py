"""Package an application-only update; never include user state or drivers."""
from pathlib import Path
import argparse,hashlib,json,shutil,zipfile

ROOT=Path(__file__).resolve().parents[1]
SOURCE_TOP=('app','native','tools','scripts','docs','licenses')
SOURCE_FILES=('README.md','THIRD_PARTY_NOTICES.md','VERSION','requirements.txt','requirements-build.txt','PriParaImageCamera.spec','.gitignore','.gitattributes')
OLD_SCRIPTS={'assemble_release.py','build_native.py','run_offline_tests.py','verify_release.py'}

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def sources():
    for name in SOURCE_FILES:yield ROOT/name
    for top in SOURCE_TOP:
        for path in sorted((ROOT/top).rglob('*')):
            rel=path.relative_to(ROOT)
            if not path.is_file() or path.is_symlink():continue
            if any(part in ('__pycache__','data','ipc','channels','TicketData','.git') for part in rel.parts):continue
            character_asset=top=='app' and 'character_assets' in rel.parts
            if top=='app' and ('native' in rel.parts or (not character_asset and path.suffix not in ('.py','.json'))):continue
            if path.name in OLD_SCRIPTS:continue
            if path.suffix.lower() in ('.pyc','.pyo','.exe','.dll','.bin','.db','.sqlite3','.bmp','.log','.jsonl','.zip') or (path.suffix.lower()=='.png' and not character_asset):continue
            yield path

def package(qt_sources,proof):
    version=(ROOT/'VERSION').read_text('utf-8').strip()
    report=json.loads(Path(proof).read_text('utf-8'))
    if not report.get('passed') or report.get('version')!=version or not report.get('frozen'):raise ValueError('A passing frozen self-test is required')
    bundle=ROOT/'build/app-dist/PriParaImageCamera'
    exe=bundle/'PriParaImageCamera.exe'
    if not exe.is_file():raise FileNotFoundError('Build the application first')
    qt_sources=Path(qt_sources)
    if qt_sources.name!='PriParaImageCamera_QtSources_6.12.0.zip':raise ValueError('Expected corresponding Qt/PySide source archive')
    with zipfile.ZipFile(qt_sources) as archive:
        if archive.testzip():raise ValueError('Invalid corresponding source archive')
        if not any(p.endswith('source-manifest.json') for p in archive.namelist()):raise ValueError('Corresponding source manifest missing')
    release=ROOT/'dist/release'/('v'+version);release.mkdir(parents=True,exist_ok=True)
    for name in ('README.md','THIRD_PARTY_NOTICES.md'):shutil.copy2(ROOT/name,bundle/name)
    shutil.copytree(ROOT/'licenses',bundle/'licenses',dirs_exist_ok=True)
    shutil.copytree(ROOT/'docs',bundle/'docs',dirs_exist_ok=True)
    files=[]
    for path in sorted(bundle.rglob('*')):
        if path.is_file() and path!=bundle/'manifest.json':files.append(dict(path=path.relative_to(bundle).as_posix(),bytes=path.stat().st_size,sha256=sha(path)))
    # A build folder must stay pristine: no runtime library/index/profile.
    forbidden={'data','channels','ipc','TicketData'}
    if any(set(Path(row['path']).parts)&forbidden for row in files):raise ValueError('Runtime user data in application bundle')
    manifest=dict(version=version,application_only=True,driver_registration=False,game_patch=False,files=files)
    (bundle/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    portable=release/f'pripara-arcade-supporter-v{version}-win64.zip'
    with zipfile.ZipFile(portable,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for path in sorted(bundle.rglob('*')):
            if path.is_file():archive.write(path,'PriParaImageCamera/'+path.relative_to(bundle).as_posix())
    source=release/f'pripara-arcade-supporter-v{version}-source.zip'
    source_rows=[]
    with zipfile.ZipFile(source,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for path in sorted(set(sources())):
            rel=path.relative_to(ROOT).as_posix();archive.write(path,'source/'+rel)
            source_rows.append(dict(path=rel,bytes=path.stat().st_size,sha256=sha(path)))
        archive.writestr('source/SOURCE_MANIFEST.json',json.dumps(dict(version=version,files=source_rows),ensure_ascii=False,indent=2)+'\n')
    target=release/qt_sources.name
    if target.resolve()!=qt_sources.resolve():shutil.copy2(qt_sources,target)
    assets=[portable,source,target]
    result=dict(project='pripara-arcade-supporter',version=version,application_only=True,game_tested=False,offline_tested=True,
        source_files=len(source_rows),assets=[dict(name=p.name,bytes=p.stat().st_size,sha256=sha(p)) for p in assets])
    (release/'release-manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (release/'SHA256SUMS.txt').write_text(''.join(sha(p)+'  '+p.name+'\n' for p in assets),encoding='utf-8')
    print(json.dumps(result));return result

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--qt-sources',type=Path,required=True);parser.add_argument('--proof',type=Path,required=True)
    options=parser.parse_args();package(options.qt_sources,options.proof)
