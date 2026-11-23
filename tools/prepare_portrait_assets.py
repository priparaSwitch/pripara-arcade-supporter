"""Prepare head-only, key-free render components from explicit local inputs.

The source manifest, Unity bundles and private decode cache are build inputs
only. Published scene DTOs contain derived geometry/textures and content URLs.
No user profile, SQLite database or whole-avatar combination is processed.
"""
from __future__ import annotations
from pathlib import Path,PurePosixPath
import argparse,copy,hashlib,importlib.util,io,json,os,re,shutil,sys,time,uuid,zipfile

BASE=[6,0,1,1,1,0,0,40027,0,0,0]
PREFIX='_portrait/'
TEXTURES=('faceTexture','bodyTexture','leftTexture','rightTexture','highlightTexture','makeupTexture','texture')
HEAD_ROLES={'face','hair','glasses','accent','top'}

def digest(data):return hashlib.sha256(data).hexdigest()
def encoded(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
def checked(path):
    path=Path(path).expanduser().resolve(strict=True)
    if not path.is_file() or path.is_symlink():raise ValueError('Input must be an ordinary file')
    return path
def url(ident):return '/front-cache/'+ident.split('.')[0]+'/asset.'+ident.split('.')[-1]

class DerivedAssets:
    def __init__(self,raw_root,output,max_texture):
        self.raw_root=raw_root;self.output=output;self.maximum=max_texture
        self.output.mkdir(parents=True,exist_ok=True)
        self.assets={};self.models={};self.textures={};self.scaled=0
    def put(self,data,extension):
        ident=digest(data)+'.'+extension;target=self.output/ident
        if target.exists():
            if target.read_bytes()!=data:raise ValueError('Derived content address collision')
        else:target.write_bytes(data)
        self.assets[ident]=dict(id=ident,url=url(ident),sha256=ident.split('.')[0],bytes=len(data))
        return url(ident)
    def read(self,address):
        match=re.fullmatch(r'/front-cache/([0-9a-f]{64})/asset\.(json|bin|png|webp)',address)
        if not match:raise ValueError('Invalid public content URL')
        path=self.raw_root/(match[1]+'.'+match[2]);data=path.read_bytes()
        if digest(data)!=match[1]:raise ValueError('Public DTO content hash mismatch')
        return data,match[2]
    def blob(self,address):
        data,extension=self.read(address);return self.put(data,extension)
    def texture(self,info):
        key=(info['url'],info.get('rawRgbaUrl'),tuple(info['size']),info.get('filter'))
        if key in self.textures:return copy.deepcopy(self.textures[key])
        result=copy.deepcopy(info);width,height=info['size']
        if max(width,height)>self.maximum:
            from PIL import Image
            if info.get('rawRgbaUrl'):
                raw,_=self.read(info['rawRgbaUrl'])
                if len(raw)!=width*height*4:raise ValueError('Raw texture dimensions mismatch')
                source=Image.frombytes('RGBA',(width,height),raw)
            else:
                png,_=self.read(info['url']);source=Image.open(io.BytesIO(png)).convert('RGBA')
            factor=self.maximum/max(width,height)
            size=(max(1,round(width*factor)),max(1,round(height*factor)))
            method=Image.Resampling.NEAREST if info.get('filter')==0 else Image.Resampling.BILINEAR
            # Independent L channels retain RGB under alpha zero. RGBA resize
            # would premultiply and irreversibly discard those shader inputs.
            channels=[channel.resize(size,resample=method) for channel in source.split()]
            image=Image.merge('RGBA',channels);raw=image.tobytes();buffer=io.BytesIO();image.save(buffer,'PNG',optimize=True)
            result.update(url=self.put(buffer.getvalue(),'png'),size=list(size))
            if info.get('rawRgbaUrl'):result.update(rawRgbaUrl=self.put(raw,'bin'),rawRgbaBytes=len(raw))
            for channel in channels:channel.close()
            image.close();source.close();self.scaled+=1
        else:
            result['url']=self.blob(info['url'])
            if info.get('rawRgbaUrl'):result['rawRgbaUrl']=self.blob(info['rawRgbaUrl'])
        self.textures[key]=copy.deepcopy(result);return result
    def model(self,entry):
        address=entry['url']
        if address not in self.models:
            raw,extension=self.read(address)
            if extension!='json':raise ValueError('Expected a model DTO')
            model=json.loads(raw);model['geometryUrl']=self.blob(model['geometryUrl'])
            model['textures']={name:self.texture(info) for name,info in model['textures'].items()}
            self.models[address]=self.put(encoded(model),'json')
        result=copy.deepcopy(entry);result['url']=self.models[address];return result
    def scene(self,scene):
        result=copy.deepcopy(scene)
        result['models']=[self.model(entry) for entry in scene.get('models',[]) if entry['role'] in HEAD_ROLES]
        for info in result.get('appearance',{}).values():
            for name in TEXTURES:
                if name in info:info[name]=self.texture(info[name])
        return result

def load_exporter(editor,manifest,cache,public):
    # All locations derive from explicit CLI inputs, never a developer home.
    sys.path[:0]=[str(cache.parent/'portrait_tool_deps'),str(editor),str(editor/'render_lab'),str(editor.parent/'_temp/unity_research_deps')]
    # Load the explicitly staged decoder before the editor adds its own cache
    # directories; an incomplete namespace there must not shadow dependencies.
    importlib.import_module('UnityPy')
    def module(name,path):
        spec=importlib.util.spec_from_file_location(name,path);value=importlib.util.module_from_spec(spec);sys.modules[name]=value;spec.loader.exec_module(value);return value
    native=module('_portrait_native_export',editor/'render_lab/front_export.py')
    native.CACHE_ROOT=cache;native.SOURCE_MANIFEST=str(manifest)
    exporter=native.FrontExporter()
    # resolve_selection normally adds baseline legs even without an outfit.
    # They are not portrait assets and must not be decoded or published.
    original_model=exporter.export_model;original_texture=exporter.export_texture
    models={};textures={}
    def export_model(model):
        if model=='0001_S01':raise ValueError('Portrait export excludes baseline legs')
        if model not in models:models[model]=original_model(model)
        return copy.deepcopy(models[model])
    def export_texture(relative):
        if relative not in textures:textures[relative]=original_texture(relative)
        return copy.deepcopy(textures[relative])
    exporter.export_model=export_model;exporter.export_texture=export_texture
    dto=module('_portrait_public_scene',editor/'server_render/public_scene.py')
    def builder(values,component=None):
        result=exporter.resolve_selection(values)
        roles=HEAD_ROLES if component is None else ({component} if component in HEAD_ROLES else set())
        result['models']=[entry for entry in result['models'] if entry['role'] in roles]
        result['appearance']={key:value for key,value in result['appearance'].items() if component is None or key==component}
        result['unavailable']=[row for row in result['unavailable'] if row['role']!='legs' and (component is None or row['role']==component)]
        result['warnings']=[]
        return result
    return exporter,dto.PublicSceneStore(cache,public,builder,exporter.sources.revision)

def seed_cache(seed,destination):
    destination.mkdir(parents=True,exist_ok=True)
    if seed is None:return 0
    seed=Path(seed).expanduser().resolve(strict=True)
    if seed==destination.resolve() or destination.resolve().is_relative_to(seed):raise ValueError('Cache seed and output must be separate')
    copied=0
    for source in seed.rglob('*'):
        if source.is_symlink():raise ValueError('Cache seed symlinks are not allowed')
        if not source.is_file():continue
        relative=source.relative_to(seed);target=destination/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        if not target.is_file() or target.stat().st_size!=source.stat().st_size:
            shutil.copyfile(source,target);copied+=1
    return copied

def prepare(args):
    editor=Path(args.editor_root).expanduser().resolve(strict=True)
    manifest=checked(args.source_manifest);parts=checked(args.hk_parts);images=checked(args.images)
    output=Path(args.output_images).expanduser().resolve();output.parent.mkdir(parents=True,exist_ok=True)
    if output==images:raise ValueError('Keep the input image archive immutable; select a separate output')
    for path in (editor/'render_lab/front_export.py',editor/'server_render/public_scene.py'):checked(path)
    source_hash=digest(manifest.read_bytes());images_hash=digest(images.read_bytes())
    hk=json.loads(parts.read_bytes()).get('parts',{})
    work=output.parent.parent/'build/portrait_assets';cache=work.parent/'portrait_decode_cache'
    work.mkdir(parents=True,exist_ok=True);copied=seed_cache(args.cache_seed,cache)
    exporter,store=load_exporter(editor,manifest,cache,work/'public_dto')
    assets=DerivedAssets(work/'public_dto',work/'assets',args.max_texture)
    def scene(values,component=None):return assets.scene(store.build(dict(values=values,component=component))['scene'])
    base=scene(BASE)
    if {row['role'] for row in base['models']}!={'face','hair','top'}:raise ValueError('Base portrait face/hair/standard top is unavailable')
    if args.reuse_components:
        previous=json.loads((work/'manifest.json').read_bytes())
        if not previous.get('complete') or previous.get('sourceRevision')!=exporter.sources.revision:raise ValueError('No compatible complete component export to reuse')
        components=previous['components']
        for row in previous['assets']:
            blob=(assets.output/row['id']).read_bytes()
            if digest(blob)!=row['sha256']:raise ValueError('Existing component content hash mismatch')
            assets.assets[row['id']]=row
    else:components={key:{} for key in ('face','hair','skin','hairColor','eye','glasses','accent')}
    for row in base['models']:
        if row['role'] in components:components[row['role']][str(row['id'])]=dict(row,supported=True)
    for key in ('skin','hairColor'):components[key][str(base['appearance'][key]['id'])]=dict(base['appearance'][key],supported=True)
    components['eye']['1:0']=dict(base['appearance']['eye'],supported=True)
    components['glasses']['6:0']=None
    components['accent']['0']=dict(appearance=base['appearance'].get('accent',dict(id=0,enabled=False)),model=None,supported=True)
    def unavailable():return dict(supported=False,reason='선택한 파츠의 3D 자료를 준비하지 못했습니다.')
    def part(kind,ident):
        values=list(BASE);values[{'face':0,'skin':1,'hair':2,'hairColor':3,'eye':4,'glasses':5,'accent':6}[kind]]=ident
        return values
    def model_part(kind,ident,values=None):
        result=scene(values or part(kind,ident),kind)
        entry=next((entry for entry in result['models'] if entry['role']==kind),None)
        return dict(entry,supported=True) if entry else unavailable()
    if args.reuse_components:
        for ident in range(4):
            result=scene(part('skin',ident),'skin')['appearance'].get('skin')
            components['skin'][str(ident)]=dict(result,supported=True) if result else unavailable()
    elif not args.base_only:
        for ident in range(1,41):components['face'][str(ident)]=model_part('face',ident)
        for ident in range(1,81):components['hair'][str(ident)]=model_part('hair',ident)
        for kind,ids in (('skin',range(4)),('hairColor',range(64))):
            for ident in ids:
                result=scene(part(kind,ident),kind)['appearance'].get(kind)
                components[kind][str(ident)]=dict(result,supported=True) if result else unavailable()
        for eye in range(1,43):
            for skin in range(4):
                values=part('eye',eye);values[1]=skin
                result=scene(values,'eye')['appearance'].get('eye')
                components['eye'][f'{eye}:{skin}']=dict(result,supported=True) if result else unavailable()
        glass_ids=sorted({0,*[int(key) for key,value in hk.get('glasses',{}).items() if str(key).isdigit() and value.get('supported',True)]})
        for face in range(1,41):
            for glass in glass_ids:
                if glass==0:components['glasses'][f'{face}:0']=None;continue
                values=part('glasses',glass);values[0]=face
                components['glasses'][f'{face}:{glass}']=model_part('glasses',glass,values)
        for ident,row in sorted(exporter.parts.get('accent',{}).items()):
            if row.get('available') is False:components['accent'][str(ident)]=unavailable();continue
            result=scene(part('accent',ident),'accent')
            appearance=result['appearance'].get('accent')
            entry=next((entry for entry in result['models'] if entry['role']=='accent'),None)
            components['accent'][str(ident)]=dict(appearance=appearance,model=entry,supported=appearance is not None)
    web={}
    for name in ('front_renderer.mjs','front_motion.mjs','front_hair_physics.mjs','vendor/three.module.js','vendor/three.core.js','vendor/LICENSE'):
        path=checked(editor/'render_lab'/name);web[PREFIX+'web/'+name]=path.read_bytes()
    result=dict(format='pripara-portrait-components',version=1,complete=not args.base_only,
        baseValues=BASE,baseScene=base,components=components,
        hkMap=dict(face={str(i):i+1 for i in range(40)},hair={str(i):i+1 for i in range(80)},
                   hairColor={str(i):i for i in range(64)},eye={str(i):i+1 for i in range(42)},skin={str(i):i for i in range(4)}),
        assets=sorted(assets.assets.values(),key=lambda row:row['id']),
        textureMaximum=args.max_texture,geometryUnmodified=True,sourceRevision=exporter.sources.revision,
        shader='original front renderer public DTO',profileOrSourcePathsIncluded=False)
    raw=encoded(result);(work/'manifest.json').write_bytes(raw)
    temporary=output.with_name(output.name+'.'+uuid.uuid4().hex+'.tmp')
    original={}
    try:
        with zipfile.ZipFile(images) as source,zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as target:
            names=set()
            for entry in source.infolist():
                name=entry.filename
                if name.startswith(PREFIX):continue
                if name in names or PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts:raise ValueError('Invalid image archive member')
                names.add(name);blob=source.read(entry);original[name]=digest(blob);target.writestr(entry,blob)
            target.writestr(PREFIX+'manifest.json',raw)
            for row in result['assets']:target.writestr(PREFIX+'assets/'+row['id'],(assets.output/row['id']).read_bytes())
            for name,blob in web.items():target.writestr(name,blob)
        with zipfile.ZipFile(temporary) as check:
            if check.testzip() is not None or any(digest(check.read(name))!=sha for name,sha in original.items()):raise ValueError('Existing image bytes changed')
        if digest(manifest.read_bytes())!=source_hash or digest(images.read_bytes())!=images_hash:raise ValueError('A source input changed during preparation')
        os.replace(temporary,output)
    finally:
        if temporary.exists():temporary.unlink()
    proof=dict(passed=True,baseOnly=args.base_only,complete=not args.base_only,sourceManifestSha256=source_hash,
        inputImagesSha256=images_hash,outputImagesSha256=digest(output.read_bytes()),outputBytes=output.stat().st_size,
        existingImages=len(original),existingImageBytesPreserved=True,sourceInputsUnchanged=True,
        components={kind:len(rows) for kind,rows in components.items()},assets=len(assets.assets),
        texturesResized=assets.scaled,seedFilesCopied=copied,privateSourcePathsIncluded=False,
        SQLiteOrUserProfilesRead=False,geometryAndUvUnmodified=True)
    (work/'proof.json').write_bytes(encoded(proof));print(json.dumps(proof,ensure_ascii=False,indent=2));return proof

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('editor-root','source-manifest','hk-parts','images','output-images'):parser.add_argument('--'+name,required=True)
    parser.add_argument('--cache-seed');parser.add_argument('--base-only',action='store_true')
    parser.add_argument('--reuse-components',action='store_true',help='Reuse a complete local component export and update only the fixed base and skin maps')
    parser.add_argument('--max-texture',type=int,choices=(128,256,512),default=256)
    args=parser.parse_args();prepare(args)
if __name__=='__main__':main()
