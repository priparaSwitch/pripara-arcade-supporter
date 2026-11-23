"""Opt-in source/frozen converter check using only generated synthetic tickets.

The two byte-range test keys below are deliberately synthetic fixtures. They
are not any game's shared protocol material and cannot decode actual tickets.
"""
from pathlib import Path
from functools import reduce
from contextlib import ExitStack
from unittest.mock import patch
import hashlib,json,operator,os,shutil,tempfile,time

def run(report_path):
    from offline_check import _profile
    from app import ROOT
    from remote_catalog import Catalog
    from ticket_store import TicketStore
    from ticket_codec import decode_ticket,unpack_payload,pack_body,build_friend,build_my
    from rank_service import rank_for_likes,THRESHOLDS
    from protocol_delivery import _write_radix255
    from conversion_service import ConversionService,ConversionError,MissingKeyMaterial,parse_kr,convert_kr,_crypt,ALPHABET
    from converter_widget import ConverterWidget
    from friend_to_my import FriendToMyDialog
    from PySide6.QtWidgets import QApplication,QDialog
    from PySide6.QtCore import QCoreApplication,QEvent
    from PIL import Image
    import zxingcpp,numpy as np
    result={'passed':False,'checks':[],'synthetic_profiles_only':True,'synthetic_test_keys_only':True,'game_camera_or_reader_access':False,'qr_transmission':False}
    def check(name,condition):
        if not condition:raise AssertionError(name)
        result['checks'].append(name)
    def wait(predicate,application):
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            application.processEvents()
            if predicate():return
            time.sleep(.01)
        raise AssertionError('Synthetic local converter did not settle')
    with tempfile.TemporaryDirectory(prefix='pripara-converter-check-') as temporary:
        root=Path(temporary)
        with _profile(root):
            shutil.copyfile(ROOT/'data.db',root/'data.db');catalog=Catalog(root,strict=True)
            store=TicketStore(root/'data/tickets.sqlite3',catalog=catalog);service=ConversionService(root,store,catalog)
            application=QApplication.instance() or QApplication([])
            check('offscreen Qt',application.platformName()=='offscreen')
            widget=ConverterWidget(root,store,catalog);loaded=[];converted=[]
            widget.load_qr.connect(loaded.append);widget.converted.connect(converted.append)
            check('no-key stable startup',not service.key_status()['available'] and not loaded and not converted)
            try:service.convert_file(root/'absent.png')
            except MissingKeyMaterial:check('no-key actionable feature limitation',True)
            else:raise AssertionError('Missing material accepted')
            keys=(bytes(range(16)),bytes(range(16,32)))
            material=root/'synthetic-decoder.json';material.write_text(json.dumps(dict(format='pripara-kr-decoder',version=1,start_key_hex=keys[0].hex(),end_key_hex=keys[1].hex())),encoding='utf-8')
            service.set_key_path(material);widget.service.set_key_path(material);widget.update_material()
            check('explicit synthetic material loads',service.key_status()['available'])
            outfit=catalog.list_sets()[0]
            source=bytearray([1]*121);source[:3]=b'K21';source[46:48]=b'LS'
            for offset,width,value in ((3,3,1001),(6,3,101001),(9,1,27),(10,1,0)):_write_radix255(source,offset,width,value)
            for index,slot in enumerate(('top','bottom','shoes','head')):
                item=outfit['pieces'][slot];_write_radix255(source,11+index*3,2,item);_write_radix255(source,13+index*3,1,catalog.items[item]['aux'] if item else 0)
            for index,value in enumerate((6,1,4,0,1,0,0,0,0)):_write_radix255(source,48+index,1,value)
            for index,code in enumerate((220,205,219,220,412,0)):
                source[101+index*2]=ord(ALPHABET[code//45]);source[102+index*2]=ord(ALPHABET[code%45])
            def encrypt(body):
                data=bytearray(body);data.append(reduce(operator.xor,data,0));count=len(data)//16*16;remainder=len(data)%16
                data[:count]=_crypt(bytes(data[:count]),keys[0],decrypt=False)
                data[remainder:remainder+count]=_crypt(bytes(data[remainder:remainder+count]),keys[1],decrypt=False)
                return bytes(data)
            for kind in (b'K21',b'K31'):
                body=bytearray(source);body[:3]=kind;encrypted=encrypt(body);parsed=parse_kr(encrypted,keys)
                check(kind.decode()+' decrypt exact 121 bytes',parsed['body']==body)
                raw,profile=convert_kr(parsed)
                check(kind.decode()+' preserves original identity name rank parts and item aux',profile['profile_id']==1001 and profile['make_id']==101001 and profile['manager']==0 and profile['nickname']=='TEST♪' and profile['rank']==27 and profile['appearance']==[6,1,4,0,1,0,0,0,0] and unpack_payload(raw)[4:16]==body[11:23])
                check(kind.decode()+' likes not invented',profile['likes']==0 and profile['likes_recovered'] is False)
            encrypted=encrypt(source);image=root/'synthetic-kr.png'
            Image.fromarray(np.asarray(zxingcpp.create_barcode(encrypted,zxingcpp.BarcodeFormat.QRCode,eci=0).to_image(scale=7))).save(image)
            widget.convert_path(str(image));wait(lambda:bool(converted) or (widget.worker and not widget.worker.isRunning()),application);application.processEvents()
            check('worker converts image and auto-stores friend without sending',len(converted)==1 and len(store.tickets('tomo'))==1 and not loaded)
            output=converted[0];original=output['profile'];saved=Image.open(output['path'])
            check('saved QR image independent byte validation',output['raw'] in [bytes(item.bytes) for item in zxingcpp.read_barcodes(saved,try_invert=True) if item.valid])
            my_dialog=FriendToMyDialog(root,original,store,catalog);my_dialog.reject()
            check('cancel creates no My ticket',not store.tickets('my') and my_dialog.created is None)
            my_dialog=FriendToMyDialog(root,original,store,catalog);my_dialog.likes_input.setText('20000000');my_dialog.generate_button.click()
            check('friend-to-My generation accepted',my_dialog.result()==QDialog.DialogCode.Accepted and bool(my_dialog.created))
            generated=my_dialog.created['profile']
            check('friend-to-My retains identity nickname appearance outfit and aux',all(generated[key]==original[key] for key in ('uid','profile_id','make_id','manager','nickname','appearance','items','aux')))
            check('friend-to-My native rank and first-play protection',generated['kind']=='my' and generated['likes']==20_000_000 and generated['rank']==30 and generated['last_rank']==30 and generated['total_plays']==1)
            check('My result auto-stored without loading reader',len(store.tickets('my'))==1 and not loaded)
            source_body=unpack_payload(output['raw']);target_body=unpack_payload(my_dialog.created['raw'])
            allowed={3,25,26,27,28,29,30,31,32,33,34,35,36,43,119,120}
            check('all other source fields byte-identical',all(a==b for index,(a,b) in enumerate(zip(source_body,target_body)) if index not in allowed))
            manager_body=bytearray(source_body);_write_radix255(manager_body,24,1,7)
            manager_friend=decode_ticket(pack_body(manager_body));manager_my=decode_ticket(build_my(manager_friend,20_000_000))
            check('nonzero friend manager normalized to actual native My identity',manager_my['manager']==0 and manager_my['uid']==manager_friend['player_code'] and manager_my['uid']!=manager_friend['uid'] and all(manager_my[key]==manager_friend[key] for key in ('profile_id','make_id','nickname','appearance','items','aux')))
            manager_dialog=FriendToMyDialog(root,manager_friend,store,catalog)
            check('nonzero-manager clone shows target ID and explicit limitation before generation',manager_dialog.target_uid.text()==manager_my['uid'] and '새 마이티켓 ID' in manager_dialog.findChild(__import__('PySide6.QtWidgets',fromlist=['QLabel']).QLabel,'friendMyIdentityNotice').text() and manager_dialog.created is None)
            manager_dialog.reject()
            bad=bytearray(encrypted);bad[5]^=1
            try:parse_kr(bad,keys)
            except ConversionError:check('tampered QR rejected',True)
            else:raise AssertionError('Tampered QR accepted')
            unknown=bytearray(source);unknown[:3]=b'XXX'
            try:parse_kr(encrypt(unknown),keys)
            except ConversionError:check('unknown decrypted layout rejected',True)
            else:raise AssertionError('Unknown layout accepted')
            check('rank 20m exact and conservative edition cap',rank_for_likes(20_000_000)==30 and rank_for_likes(100_000_000)==33 and rank_for_likes(100_000_000,edition=10)==38)
            for window in (widget,my_dialog,manager_dialog):window.close();window.deleteLater()
            QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);application.processEvents()
            store.close();catalog.close()
    result.update(passed=True,check_count=len(result['checks']),offscreen=True,real_png_decode=True,real_qt_widget=True,real_sqlite=True)
    Path(report_path).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return result


def run_private(image_path,key_path,report_path):
    """Explicit local-material check; caller must keep the report private.

    Actual profile values and keys are never written to the report. All output
    ticket files and the temporary index are removed with the isolated folder.
    """
    from offline_check import _profile
    from app import ROOT

    from remote_catalog import Catalog
    from ticket_store import TicketStore
    from conversion_service import ConversionService,load_key_material,read_kr_image,read_radix255
    import zxingcpp
    from PIL import Image
    report={'passed':False,'permitted_local_material':True,'report_contains_profile_values':False,
            'report_contains_key_values':False,'game_camera_or_reader_access':False}
    with tempfile.TemporaryDirectory(prefix='pripara-private-converter-check-') as temporary:
        root=Path(temporary)
        with _profile(root):
            shutil.copyfile(ROOT/'data.db',root/'data.db');catalog=Catalog(root,strict=True)
            store=TicketStore(root/'data/tickets.sqlite3',catalog=catalog)
            ticket=read_kr_image(image_path,load_key_material(key_path))
            output=ConversionService(root,store,catalog,key_path=key_path).convert_file(image_path)
            source=ticket['body'];profile=output['profile']
            preserved=(profile['profile_id']==read_radix255(source,3,3) and
                profile['make_id']==read_radix255(source,6,3) and profile['manager']==read_radix255(source,10,1) and
                profile['rank']==read_radix255(source,9,1) and profile['name_codes']==ticket['codes'] and
                profile['appearance']==[value-1 for value in source[48:57]] and profile['body'][4:16]==source[11:23])
            with Image.open(output['path']) as picture:
                png_valid=output['raw'] in [bytes(item.bytes) for item in zxingcpp.read_barcodes(picture,try_invert=True) if item.valid]
            count=len(store.tickets('tomo'));store.close();catalog.close()
            report.update(passed=bool(preserved and png_valid and count==1),plaintext_size=len(source),
                plaintext_sha256=hashlib.sha256(source).hexdigest(),output_sha256=hashlib.sha256(output['raw']).hexdigest(),
                all_mapped_source_fields_preserved=preserved,generated_png_decoded_exactly=png_valid,auto_stored_friend_count=count)
    Path(report_path).write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    if not report['passed']:raise AssertionError('Private converter validation failed')
    return report


run_supplied=run_private
