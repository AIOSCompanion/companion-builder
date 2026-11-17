"""Read existing deployment state without emitting credentials or application data."""
import os
from pathlib import Path
import shlex
import subprocess
import sys

from cloud_ssh import access

REMOTE = r'''
import hashlib,itertools,json,pathlib,subprocess
def command(args):
    return subprocess.run(args,capture_output=True,text=True,check=True,timeout=30).stdout
ids=command(['docker','ps','-aq']).split()
containers=json.loads(command(['docker','inspect',*ids])) if ids else []
report={'containers':[],'deployment_state':[]}
for item in containers:
    env=[value.split('=',1)[0] for value in item['Config'].get('Env',[]) if '=' in value]
    report['containers'].append({'id':item['Id'],'name':item['Name'],
        'image':item['Config']['Image'],'status':item['State']['Status'],
        'started_at':item['State']['StartedAt'],'restart':item['HostConfig']['RestartPolicy'],
        'compose':{k:v for k,v in (item['Config'].get('Labels') or {}).items() if k.startswith('com.docker.compose.')},
        'mounts':[{'type':v['Type'],'source':v['Source'],'destination':v['Destination'],'rw':v['RW']} for v in item['Mounts']],
        'retired_provider_environment_fields':[k for k in env if k.startswith(('CLOUDFLARE_','CF_','LIVEKIT_','VOICE_','DEEPGRAM_','ELEVENLABS_'))]})
allowed={'phase','outcome','revision','request_id','action','controller','container','image','compose_project'}
seen=set()
for value in ['/var/lib/companion-deploy','/var/snap/docker/common/companion-deploy']:
    root=pathlib.Path(value)
    if not root.exists() or root.resolve() in seen:continue
    seen.add(root.resolve())
    state={'root':str(root.resolve()),'files':[]}
    for relative in ['current.json','journal.json','request.json','voice/pending.json']:
        file=root/relative
        if not file.exists():continue
        row={'file':relative,'size':file.stat().st_size}
        if file.is_file() and not file.is_symlink() and row['size']<=1024*1024:
            try:
                body=json.loads(file.read_text())
                row['state']={k:v for k,v in body.items() if k in allowed and isinstance(v,(str,int,bool))}
            except (ValueError,AttributeError):row['unreadable_structure']=True
        state['files'].append(row)
    for relative in ['voice','incoming','results']:
        directory=root/relative
        if directory.is_dir() and not directory.is_symlink():
            state[relative+'_entries']=[{'name':p.name,'size':p.lstat().st_size,'symlink':p.is_symlink()} for p in itertools.islice(directory.iterdir(),100)]
    report['deployment_state'].append(state)
site=pathlib.Path('/etc/nginx/sites-enabled/aegra-tls')
if site.is_file():report['nginx_site_sha256']=hashlib.sha256(site.read_bytes()).hexdigest()
report['bridge_proxy_present']=pathlib.Path('/etc/nginx/snippets/companion-builder-bridge.conf').is_file()
report['services']={name:subprocess.run(['systemctl','is-active',name],capture_output=True,text=True,timeout=10).stdout.strip() for name in ['nginx','companion-deploy','companion-builder-bridge']}
print(json.dumps(report))
'''


def main():
    with access(dict(os.environ)) as login:
        result = subprocess.run(login['args'] + ['sudo -n python3 -c ' + shlex.quote(REMOTE)],
                                capture_output=True, text=True, timeout=90)
        if result.returncode:
            raise RuntimeError('Read-only runtime inventory failed; remote output withheld')
        # The approved workflow seals stdout before retaining any report.
        print(result.stdout, end='')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Runtime inventory failed; diagnostic values withheld', file=sys.stderr)
        sys.exit(1)
