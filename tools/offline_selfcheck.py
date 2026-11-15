"""Portable, generated ZIP checks. No network, settings, identity or messages."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import tempfile
import sys
from unittest.mock import patch
import zipfile


def check(scripts):
    sys.path.insert(0,str(scripts))
    import download_checkpoint as cp
    import native_log_source as native
    from request_model import InputError
    metadata={'vin':'DEM0VEH1CLE000001','start':'2026-10-30T17:00:00','end':'2026-10-30T17:35:00'}
    with tempfile.TemporaryDirectory(prefix='wt-offline-selfcheck-') as temp:
        root=Path(temp)
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w',zipfile.ZIP_STORED) as archive:
            archive.writestr('complete.log',b'complete generated record\n'*100)
        body=output.getvalue()
        selected={name:[] for name in native.LOG_CLASSES}
        selected['log_fsdA']=[{'vin':metadata['vin'],'fileKey':'generated',
            'fileName':'generated.zip','fileSize':len(body),'downloadURL':'https://fixture.invalid/generated'}]
        def fetch(directory,payload):
            def download(url,path,*,environment,max_bytes,byte_budget):
                path.parent.mkdir(parents=True,exist_ok=True)
                byte_budget.consume(len(payload));path.write_bytes(payload);return len(payload)
            with patch.object(native.cloud,'download_to_file',side_effect=download):
                return native.fetch_window(metadata,{'local_analysis':{'download_workers':1}},
                    directory,environment='prod',selected=selected)
        with patch.object(cp,'verify_file',wraps=cp.verify_file) as verify:
            result=fetch(root/'valid',body)
        assert verify.call_count==1
        assert result['files'][0]['sha256']==hashlib.sha256(body).hexdigest()
        corrupt=bytearray(body);corrupt[corrupt.index(b'complete generated')]^=1
        try:fetch(root/'corrupt',bytes(corrupt))
        except InputError:pass
        else:raise AssertionError('CRC corruption was accepted')
        assert not (root/'corrupt/download-manifest.json').exists()
        assert not list((root/'corrupt/download-receipts').glob('*.json'))
    return {'status':'passed','fresh_validation_passes_per_file':1,'sha256_equal':True,
        'crc_corruption_rejected':True,'network_requests':0,'business_jobs_created':0}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--scripts',type=Path)
    args=parser.parse_args()
    scripts=args.scripts or Path(__file__).resolve().parents[1]/'workspace/runtime/wtr096-hardening/skill/scripts'
    print(json.dumps(check(scripts)))
