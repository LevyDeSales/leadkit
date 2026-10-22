"""deploy.sh e backup.sh: o nome da stack vem da pasta de configuração, e o que está errado para antes de mexer.

O docker não está disponível na suíte: um `docker` de mentira no PATH registra cada chamada e
responde o que os scripts perguntam. O CI roda os mesmos scripts num Swarm de verdade (job `swarm`
do deploy-image.yml); aqui fica o que se confere sem Swarm: o nome que chega ao `docker stack
deploy`, e que as recusas acontecem antes de qualquer migration ou deploy.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

SWARM_DIR = Path(__file__).resolve().parent.parent / "deploy" / "swarm"

FAKE_DOCKER = r"""#!/usr/bin/env python3
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

args = sys.argv[1:]
calls = Path(os.environ['STUB_CALLS'])
with calls.open('a') as file:
    file.write(' '.join(args) + '\n')
state_path = Path(str(calls) + '.state')
a = 'sha256:' + 'a' * 64
b = 'sha256:' + 'b' * 64
c = 'sha256:' + 'c' * 64
node = 'local-node'

def spec(image, replicas):
    return {'TaskTemplate':{'ContainerSpec':{'Image':image}}, 'Mode':{'Replicated':{'Replicas':replicas}}}

def image_id(ref):
    return b if ref in {b, 'leadkit:old'} else c if ref in {c, 'leadkit:one'} else a

def apply(state, role, image, replicas):
    svc = state['services'].setdefault(role, {'ID':role + '-service', 'Version':{'Index':0}, 'Spec':spec(image, 0)})
    svc['PreviousSpec'] = copy.deepcopy(svc['Spec'])
    svc['Spec'] = spec(image, replicas)
    svc['Version']['Index'] += 1
    svc['UpdateStatus'] = {'State':'completed'}
    for task in state['tasks'].values():
        if task['role'] == role and task['DesiredState'] == 'running':
            task['DesiredState'] = 'shutdown'
            task['Status']['State'] = 'shutdown'
            cid = task['Status']['ContainerStatus']['ContainerID']
            state['containers'][cid]['State']['Running'] = bool(os.getenv('STUB_OLD_RUNNING'))
    if replicas:
        tid = role + '-' + str(svc['Version']['Index'])
        cid = hashlib.sha256(tid.encode()).hexdigest()
        state['tasks'][tid] = {'ID':tid,'role':role,'NodeID':node, 'DesiredState':'running',
                              'Status':{'State':'running','ContainerStatus':{'ContainerID':cid}}}
        state['containers'][cid] = {'Image':image_id(image), 'State':{'Running':True,'Health':{'Status':'healthy'}}}

def reap_during_observation(state, cid):
    state['containers'].pop(cid, None)
    state.setdefault('destroyed', []).append(cid)
    state.setdefault('destroyed_at', {})[cid] = time.time_ns()
    state['removed_during_observation'] = True
    for tid, task in list(state['tasks'].items()):
        if task['Status']['ContainerStatus']['ContainerID'] == cid:
            state.setdefault('reaped_tasks', {})[cid] = copy.deepcopy(task)
            outcome=os.getenv('STUB_RACE_OUTCOME','removed')
            if os.getenv('STUB_TASK_LAG') and task['Status']['State'] == 'shutdown':
                outcome='terminal'
            if outcome in {'active','terminal'}:
                task['DesiredState']='remove'
                task['Status']['State']='running' if outcome=='active' else 'shutdown'
            else:
                del state['tasks'][tid]
            if outcome=='extra':
                extra=copy.deepcopy(task)
                extra['ID']=task['role']+'-refresh'
                extra['DesiredState']='running'
                extra['Status']['State']='running'
                extra_cid=hashlib.sha256(extra['ID'].encode()).hexdigest()
                extra['Status']['ContainerStatus']['ContainerID']=extra_cid
                state['tasks'][extra['ID']]=extra
                state['containers'][extra_cid]={'Image':b,'State':{'Running':True,'Health':{'Status':'healthy'}}}

if state_path.exists():
    state = json.loads(state_path.read_text())
else:
    floor = int(os.getenv('STUB_CONTRACT_FLOOR', '1' if os.getenv('STUB_NETWORK') else '0'))
    state = {'floor':floor, 'network':bool(os.getenv('STUB_NETWORK')), 'services':{}, 'tasks':{}, 'containers':{}, 'epoch':None}
    if state['network']:
        for role in ('api','worker'):
            apply(state, role, 'leadkit:old' if floor == 0 else 'leadkit:one' if int(os.getenv('STUB_INITIAL_CONTRACT', str(floor))) == 1 and os.getenv('STUB_TARGET_CONTRACT') == '2' else 'gerador-planos:abc123', 1)
            state['services'][role]['PreviousSpec'] = copy.deepcopy(state['services'][role]['Spec'])
            if os.getenv('STUB_INITIAL_PREVIOUS_MISSING'):
                state['services'][role].pop('PreviousSpec', None)

code = 0
out = ''
err = ''
head = args[:2]
if head == ['image','inspect']:
    if '--format' in args:
        fmt = args[args.index('--format')+1]
        out = '[]' if 'RepoDigests' in fmt else image_id(args[-1])
    else:
        out = json.dumps([{'Id': image_id(args[-1])}])
    if os.getenv('STUB_OLD_IMAGE_UNRESOLVABLE') and args[-1] in {'leadkit:old', 'leadkit:one'} and not state.get('scaled'):
        code=1
elif head == ['image','tag']:
    pass
elif head == ['network','inspect']:
    if 'traefik-public' in args:
        code = 1 if os.getenv('STUB_NO_TRAEFIK_NETWORK') else 0
        out = '' if code else os.getenv('STUB_TRAEFIK_SUBNETS','10.0.1.0/24')
    else:
        code = 0 if state['network'] else 1
elif head == ['service','inspect']:
    if any('Spec.Labels' in x for x in args):
        out = os.getenv('STUB_LIVE_RULE','')
        code = 0 if out else 1
    else:
        role = args[-1].rsplit('_',1)[-1]
        svc = copy.deepcopy(state['services'].get(role))
        if svc is None: code = 1
        else:
            if os.getenv('STUB_SPEC_DRIFT') and state.get('deployed'):
                state['services'][role]['Version']['Index'] += 1
                svc['Version']['Index'] += 1
            if os.getenv('STUB_PREVIOUS_OLD') and state.get('deployed'):
                svc['PreviousSpec'] = spec('leadkit:old',0)
            if os.getenv('STUB_MISSING_REPLICA'):
                svc['Spec']['Mode']['Replicated']['Replicas'] = 2
            out = json.dumps([svc])
elif head == ['service','ls']:
    stack = os.getenv('STACK','gerador-planos')
    out = '\n'.join(stack+'_'+r for r in state['services'])
elif head == ['service','ps']:
    role = args[-1].rsplit('_',1)[-1]
    out = '\n'.join(t['ID'] for t in state['tasks'].values() if t['role']==role)
    if state.get('removed_during_observation') and os.getenv('STUB_REFRESH_FAIL'): code=1
    if state.get('removed_during_observation') and os.getenv('STUB_REFRESH_PARTIAL')=='requested_duplicate': out+='\n'+out
elif head == ['service','scale']:
    for setting in args[2:]:
        if '=' in setting and not setting.startswith('--'):
            name, replicas = setting.rsplit('=',1)
            role = name.rsplit('_',1)[-1]
            apply(state, role, state['services'][role]['Spec']['TaskTemplate']['ContainerSpec']['Image'],int(replicas))
    if os.getenv('STUB_SCALE_FAIL'): code=1
    state['scaled'] = True
    if os.getenv('STUB_REAP_RACE') or os.getenv('STUB_TASK_LAG'):
        for task in state['tasks'].values():
            if task['DesiredState'] == 'shutdown':
                task['Status']['State'] = 'running'
                state['containers'][task['Status']['ContainerStatus']['ContainerID']]['State']['Running'] = True
    if os.getenv('STUB_REAP'):
        for tid, task in list(state['tasks'].items()):
            if task['DesiredState'] == 'shutdown':
                cid = task['Status']['ContainerStatus']['ContainerID']
                if os.getenv('STUB_REAP') != 'visible':
                    state['containers'].pop(cid, None)
                state.setdefault('destroyed', []).append(cid)
                state.setdefault('destroyed_at', {})[cid] = time.time_ns()
                if os.getenv('STUB_REAP_ACTIVE'):
                    task['Status']['State']='running'
                    task['DesiredState']='remove'
                if os.getenv('STUB_REAP_REMOTE'): task['NodeID']='remote-node'
                if os.getenv('STUB_REAP') == 'task':
                    del state['tasks'][tid]
elif head == ['service','update']:
    role = args[-1].rsplit('_',1)[-1]
    svc = state['services'][role]
    apply(state, role, svc['Spec']['TaskTemplate']['ContainerSpec']['Image'],svc['Spec']['Mode']['Replicated']['Replicas'])
    if os.getenv('STUB_FORCE_FAIL') and state['floor'] >= 1: code=1
elif head == ['stack','deploy']:
    state['network'] = True
    state['deployed'] = True
    with calls.open('a') as file:
        file.write('STACK='+os.getenv('STACK','')+' DOMAIN='+os.getenv('DOMAIN','')+'\n')
    settings = {k:v for k,v in os.environ.items() if k.startswith('RATE_LIMIT_') or '_MEMORY_' in k or k=='FORWARDED_ALLOW_IPS'}
    Path(str(calls)+'.settings').write_text('\n'.join(k+'='+v for k,v in sorted(settings.items())))
    if os.getenv('STUB_STACK_FAIL') and state['floor'] >= 1:
        code=1
    else:
        replicas = 0 if args.count('-c') == 2 else 1
        for role in ('api','worker'):
            apply(state, role, os.environ['IMAGE'],replicas)
        if os.getenv('STUB_AUTO_ROLLBACK') and replicas:
            for role in ('api','worker'):
                svc=state['services'][role]
                old=copy.deepcopy(svc['PreviousSpec'])
                apply(state,role,old['TaskTemplate']['ContainerSpec']['Image'],old['Mode']['Replicated']['Replicas'])
                svc['UpdateStatus']={'State':'rollback_completed'}
                svc.pop('PreviousSpec', None)
elif args[:1] == ['info']:
    out='different-node' if os.getenv('STUB_NODE_CHANGE') and state.get('destroyed') else node
elif head == ['node','inspect']:
    out=json.dumps([{'Status':{'State':'down' if os.getenv('STUB_NODE_DOWN') or (os.getenv('STUB_NODE_LOST') and state.get('destroyed')) else 'ready'},'ManagerStatus':{'Reachability':'reachable'}}])
elif args[:1] == ['inspect']:
    kind=args[args.index('--type')+1]
    names=args[args.index('--type')+2:]
    if os.getenv('STUB_LATE_TASK') and kind=='container' and state.get('scaled') and state['floor'] < int(os.getenv('STUB_TARGET_CONTRACT', '1')):
        for old_cid in names:
            prior=state.get('reaped_tasks',{}).get(old_cid)
            if prior and prior['role']=='api':
                count=state.get('missing_api_inspects',0)+1
                state['missing_api_inspects']=count
                if count==2:
                    extra=copy.deepcopy(prior)
                    extra['ID']='api-late-refresh'
                    extra['DesiredState']='remove'
                    extra['Status']['State']='running'
                    cid=hashlib.sha256(extra['ID'].encode()).hexdigest()
                    extra['Status']['ContainerStatus']['ContainerID']=cid
                    state['tasks'][extra['ID']]=extra
                    state['containers'][cid]={'Image':b,'State':{'Running':True,'Health':{'Status':'healthy'}}}
                    state['late_live_cid']=cid
    if kind=='task':
        for cid, count in list(state.get('waited', {}).items()):
            state['waited'][cid] += 1
            if count >= 3 and os.getenv('STUB_TASK_LAG') != 'forever':
                for task in state['tasks'].values():
                    if task['Status']['ContainerStatus']['ContainerID'] == cid:
                        task['Status']['State'] = 'shutdown'
        values=[copy.deepcopy(state['tasks'][x]) for x in names]
        if os.getenv('STUB_REMOTE_NODE'):
            for value in values: value['NodeID']='remote-node'
        if os.getenv('STUB_MISSING_CONTAINER'):
            for value in values: value['Status']['ContainerStatus']['ContainerID']=''
        if state.get('removed_during_observation') and os.getenv('STUB_REFRESH_INSPECT_FAIL'): code=1
        if state.get('removed_during_observation'):
            fault=os.getenv('STUB_REFRESH_PARTIAL')
            if fault=='omitted': values=[]
            if fault=='duplicate': values=values*2
            if fault=='wrong':
                for value in values: value['ID']='different-task'
    else:
        if os.getenv('STUB_REAP_RACE') == 'inspect' and state.get('scaled') and state['floor'] == 0:
            for cid in names:
                if cid in state['containers']:
                    reap_during_observation(state, cid)
        if os.getenv('STUB_INITIAL_INSPECT_FAIL') or any(x not in state['containers'] for x in names) or (os.getenv('STUB_REAP')=='visible' and state.get('destroyed')):
            code=1
            values=[]
        else:
            values=[copy.deepcopy(state['containers'][x]) for x in names]
            for cid, value in zip(names, values):
                value['Id']=cid
                value['State']['ExitCode']=0
                value['State'].setdefault('StartedAt', '2026-01-01T00:00:00Z')
                if value['State']['Running']:
                    fault = os.getenv('STUB_RUNNING_START_FAULT')
                    if fault == 'missing': value['State'].pop('StartedAt')
                    if fault == 'invalid': value['State']['StartedAt'] = 'SYNTHETIC_PRIVATE_ERROR'
                    if fault == 'future': value['State']['StartedAt'] = '2999-01-01T00:00:00Z'
                value['State']['FinishedAt']='0001-01-01T00:00:00Z' if value['State']['Running'] else datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
                if not value['State']['Running']:
                    value['State']['FinishedAt'] = state['containers'][cid]['State'].setdefault('FinishedAt', value['State']['FinishedAt'])
                    fault = os.getenv('STUB_STOP_FAULT')
                    if fault == 'id': value['Id'] = 'f' * 64
                    if fault == 'image': value['Image'] = 'unknown'
                    if fault == 'exit_missing': value['State'].pop('ExitCode')
                    if fault == 'exit_type': value['State']['ExitCode'] = False
                    if fault == 'exit_range': value['State']['ExitCode'] = -1
                    if fault == 'finished_missing': value['State'].pop('FinishedAt')
                    if fault == 'finished_zero': value['State']['FinishedAt'] = '0001-01-01T00:00:00Z'
                    if fault == 'finished_future': value['State']['FinishedAt'] = '2999-01-01T00:00:00Z'
                    if fault == 'finished_invalid': value['State']['FinishedAt'] = 'SYNTHETIC_PRIVATE_ERROR'
                    if fault == 'started_missing': value['State'].pop('StartedAt')
                    if fault == 'started_zero': value['State']['StartedAt'] = '0001-01-01T00:00:00Z'
                    if fault == 'started_invalid': value['State']['StartedAt'] = 'SYNTHETIC_PRIVATE_ERROR'
                    if fault == 'started_future': value['State']['StartedAt'] = '2999-01-01T00:00:00Z'
                    if fault == 'started_after_finish': value['State']['FinishedAt'] = '2025-12-31T00:00:00Z'
                    if fault == 'started_after_finish_ns':
                        value['State']['StartedAt'] = '2026-01-01T00:00:00.000000002Z'
                        value['State']['FinishedAt'] = '2026-01-01T00:00:00.000000001Z'
                if os.getenv('STUB_REAP_AFTER_STOP') and not value['State']['Running']:
                    reap_during_observation(state, cid)
            if os.getenv('STUB_OBSERVED_IMAGE_DRIFT') and state.get('scaled'):
                for value in values: value['Image']=a if value['Image']==b else b
    out=json.dumps(values)
elif head == ['container','wait']:
    out=os.getenv('STUB_WAIT_CODE', '0')
    if os.getenv('STUB_TASK_LAG'):
        state['containers'][args[-1]]['State']['Running'] = False
        state.setdefault('waited', {})[args[-1]] = 0
    if os.getenv('STUB_REAP_RACE') == 'wait': reap_during_observation(state, args[-1])
    fault=os.getenv('STUB_WAIT_FAULT')
    if fault == 'error': code=1
    if fault == 'no_such': code=1; out=''; err='Error: No such container'
    if fault == 'empty': out=''
    if fault == 'invalid': out='SYNTHETIC_PRIVATE_ERROR'
    if fault == 'multiple': out='0\n1'
    if fault == 'negative': out='-1'
    if fault == 'range': out='256'
    if fault == 'stderr': err='SYNTHETIC_PRIVATE_ERROR'
elif args[:1] == ['ps']:
    if '-aq' in args:
        if args[-1].startswith('id='):
            cid=args[-1].split('=',1)[1]
            out=cid if cid in state['containers'] else ''
            if os.getenv('STUB_LOCAL_LIST_FAIL'): code=1
        else:
            role=args[-1].rsplit('_',1)[-1]
            out='\n'.join(t['Status']['ContainerStatus']['ContainerID'] if '--no-trunc' in args else t['Status']['ContainerStatus']['ContainerID'][:12] for t in state['tasks'].values() if t['role']==role and t['Status']['ContainerStatus']['ContainerID'] in state['containers'])
            if state.get('removed_during_observation') and os.getenv('STUB_REFRESH_LOCAL_FAIL'): code=1
    elif 'service.name='+os.getenv('STUB_DB_SERVICE','never') in ' '.join(args):
        out='container'
elif args[:1] == ['events']:
    if os.getenv('STUB_EVENTS_FAIL'): code=1
    elif not os.getenv('STUB_NO_DESTROY'):
        out='\n'.join(json.dumps({'Type':'container','Action':'destroy','Actor':{'ID':cid},'scope':'local','timeNano':state['destroyed_at'][cid]}) for cid in state.get('destroyed',[]))
        fault=os.getenv('STUB_EVENT_FAULT')
        if fault=='id': out=out.replace('"ID": "','"ID": "wrong-')
        if fault=='action': out=out.replace('destroy','die')
        if fault=='type': out=out.replace('container','image')
        if fault=='json': out='SYNTHETIC_PRIVATE_ERROR'
        if fault=='old': out='\n'.join(json.dumps({'Type':'container','Action':'destroy','Actor':{'ID':cid},'scope':'local','timeNano':0}) for cid in state.get('destroyed',[]))
        if fault=='scope': out=out.replace('local','swarm')
        if fault=='future': out='\n'.join(json.dumps({'Type':'container','Action':'destroy','Actor':{'ID':cid},'scope':'local','timeNano':time.time_ns()+10**12}) for cid in state.get('destroyed',[]))
        if fault=='time_type': out='\n'.join(json.dumps({'Type':'container','Action':'destroy','Actor':{'ID':cid},'scope':'local','timeNano':str(state['destroyed_at'][cid])}) for cid in state.get('destroyed',[]))
        if os.getenv('STUB_CONSUME_EVENTS'):
            key=next(x for x in args if x.startswith('container='))
            count=state.setdefault('event_reads',{}).get(key,0)
            state['event_reads'][key]=count+1
            if count: out=''
elif args[:1] == ['exec']:
    out='dump'
elif args[:1] == ['run']:
    if args[-1]=='migrate':
        if os.getenv('STUB_LATE_TASK'):
            late=state.get('late_live_cid')
            state['late_running_at_ddl']=bool(late and state['containers'].get(late,{}).get('State',{}).get('Running'))
        state['floor']=int(os.getenv('STUB_TARGET_CONTRACT', '1'))
        state['epoch']=None
        state['phase']='blocked'
        if os.getenv('STUB_MIGRATE_FAIL'): code=1
    elif 'app.deployment_contract' in args:
        action=args[-1]
        if action=='capability':
            identity=args[args.index('python')-1]
            out=json.dumps({'contract':0 if identity==b or os.getenv('STUB_TARGET_OLD') else 1 if identity==c else int(os.getenv('STUB_TARGET_CONTRACT', '1'))})
        elif action=='status':
            out=json.dumps({'minimum_contract':state['floor'],'phase':state.get('phase','blocked'),'epoch':state['epoch']})
        else:
            doc=json.loads(sys.stdin.read())
            if action=='begin':
                state['epoch']=doc['epoch']
                state['phase']='blocked'
            elif action=='attest':
                if doc['epoch']!=state['epoch']: code=1
                else:
                    state['attested']=doc['evidence']
                    state['phase']='verified'
            out=json.dumps({'ok':True})
state_path.write_text(json.dumps(state))
if out: sys.stdout.write(out if args[:1] == ['exec'] else out + '\n')
if err: sys.stderr.write(err)
raise SystemExit(code)
"""

DOMAIN = "planos.example.test"
# O que os scripts leem do ambiente: o do desenvolvedor não pode vazar para os testes.
SCRIPT_VARIABLES = (
    "STACK",
    "DOMAIN",
    "IMAGE",
    "ROLLBACK",
    "BACKUP_DIR",
    "BACKUP_KEEP_DAYS",
    "RATE_LIMIT_AVERAGE",
    "RATE_LIMIT_BURST",
    "API_MEMORY_LIMIT",
    "API_MEMORY_RESERVATION",
    "WORKER_MEMORY_LIMIT",
    "WORKER_MEMORY_RESERVATION",
    "DB_MEMORY_LIMIT",
    "DB_MEMORY_RESERVATION",
    "FORWARDED_ALLOW_IPS",
)


class Staging:
    """Os scripts e o docker de mentira, copiados uma vez por processo; cada teste recebe um *hard
    link* deles, e não uma cópia.

    No macOS, a primeira execução de um arquivo que acabou de ser criado leva de 200 a 600 ms (a
    verificação de segurança do sistema), e as seguintes, uns 5 ms; um hard link do arquivo já
    verificado não paga nada. Com uma cópia por teste, cada um pagava duas vezes (o deploy.sh e o
    docker), uns 0,5 s: um sexto da suíte serial. No Linux não muda nada. Os testes só leem e executam
    os arquivos (nenhum os escreve), então dividir o inode é seguro."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.files: dict[Path, Path] = {}

    def copy(self, source: Path) -> Path:
        if source not in self.files:
            target = self.folder / f"{len(self.files)}-{source.name}"
            shutil.copy2(source, target)
            self.files[source] = target
        return self.files[source]

    def docker(self) -> Path:
        stub = SWARM_DIR / "docker"  # a chave do docker de mentira: o nome que nenhum arquivo tem
        if stub not in self.files:
            target = self.folder / "docker"
            target.write_text(FAKE_DOCKER)
            target.chmod(0o755)
            self.files[stub] = target
        return self.files[stub]

    @staticmethod
    def put(shared: Path, target: Path) -> None:
        try:
            os.link(shared, target)
        except OSError:  # outro sistema de arquivos, ou um que não liga arquivos
            shutil.copy2(shared, target)


class Server:
    """Uma pasta de configuração (stack.yaml, deploy.sh, app.env, db.env) e o docker de mentira."""

    def __init__(self, root: Path, staging: Staging) -> None:
        self.staging = staging
        self.folder = root / "swarm"
        self.folder.mkdir()
        for name in ("stack.yaml", "deploy.sh", "rollout.py"):
            self.install(name)
        (self.folder / "db.env").write_text("POSTGRES_PASSWORD=exemplo\n")
        self.write_app_env()
        bin_dir = root / "bin"
        bin_dir.mkdir()
        staging.put(staging.docker(), bin_dir / "docker")
        self.bin_dir = bin_dir
        self.calls_file = root / "calls.log"
        self.calls_file.touch()
        self.settings_file = Path(f"{self.calls_file}.settings")

    def install(self, name: str) -> None:
        """Põe na pasta de configuração o script (ou o stack.yaml) do repositório."""
        self.staging.put(self.staging.copy(SWARM_DIR / name), self.folder / name)

    def write_app_env(self, *lines: str) -> None:
        base = (f"PUBLIC_BASE_URL=https://{DOMAIN}", *lines)
        (self.folder / "app.env").write_text("\n".join(base) + "\n")

    def run(self, script: str, **env: str) -> subprocess.CompletedProcess[str]:
        """Roda o script da pasta com o docker de mentira; `env` soma ao ambiente, que não leva STACK etc."""
        inherited = {
            name: value for name, value in os.environ.items() if name not in SCRIPT_VARIABLES
        }
        environment = {
            **inherited,
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}",
            "STUB_CALLS": str(self.calls_file),
            **env,
        }
        return subprocess.run(
            [str(self.folder / script)],
            check=False,
            cwd=self.folder,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )

    @property
    def calls(self) -> list[str]:
        return self.calls_file.read_text().splitlines()

    @property
    def deploy_environment(self) -> dict[str, str]:
        """As variáveis do stack.yaml que chegaram ao `docker stack deploy`, pelo ambiente dele."""
        if not self.settings_file.exists():
            return {}
        return dict(line.split("=", 1) for line in self.settings_file.read_text().splitlines())

    @property
    def settings(self) -> dict[str, str]:
        """Os ajustes do app.env (limite de envios, memória): tudo menos o FORWARDED_ALLOW_IPS."""
        return {k: v for k, v in self.deploy_environment.items() if k != "FORWARDED_ALLOW_IPS"}

    @property
    def forwarded_allow_ips(self) -> str | None:
        return self.deploy_environment.get("FORWARDED_ALLOW_IPS")

    def asked_for_the_traefik_network(self) -> bool:
        return any(
            call.startswith("network inspect") and "traefik-public" in call for call in self.calls
        )

    def touched_the_swarm(self) -> bool:
        """Se o script chegou a rodar migrations ou o deploy."""
        return any(call.startswith(("run ", "stack deploy")) for call in self.calls)


@pytest.fixture(scope="session")
def staging(tmp_path_factory: pytest.TempPathFactory) -> Staging:
    return Staging(tmp_path_factory.mktemp("swarm-scripts"))


@pytest.fixture
def server(tmp_path: Path, staging: Staging) -> Server:
    return Server(tmp_path, staging)


def stack_deploy_call(server: Server) -> str:
    return next(call for call in server.calls if call.startswith("stack deploy"))


def migrate_call(server: Server) -> str:
    return next(
        call for call in server.calls if call.startswith("run ") and call.endswith(" migrate")
    )


def deploy(server: Server, **env: str) -> subprocess.CompletedProcess[str]:
    return server.run("deploy.sh", IMAGE="gerador-planos:abc123", **env)


def test_a_folder_without_stack_name_deploys_the_default_stack(server):
    result = deploy(server)

    assert result.returncode == 0, result.stderr
    assert stack_deploy_call(server).endswith(" gerador-planos")
    assert f"STACK=gerador-planos DOMAIN={DOMAIN}" in server.calls
    assert "--network gerador-planos_internal" in migrate_call(server)
    assert "--name gerador-planos-migrate" in migrate_call(server)


def test_the_stack_name_comes_from_the_app_env_of_the_folder(server):
    server.write_app_env("STACK_NAME=leadkit-test")

    result = deploy(server)

    assert result.returncode == 0, result.stderr
    assert stack_deploy_call(server).endswith(" leadkit-test")
    assert f"STACK=leadkit-test DOMAIN={DOMAIN}" in server.calls
    assert "--network leadkit-test_internal" in migrate_call(server)
    assert "--name leadkit-test-migrate" in migrate_call(server)
    assert "leadkit-test no ar com gerador-planos:abc123" in result.stdout


@pytest.mark.parametrize("line", ["STACK_NAME=", "STACK_NAME=\r"], ids=["empty", "carriage-return"])
def test_a_blank_stack_name_means_the_default_stack(server, line):
    server.write_app_env(line)

    result = deploy(server)

    assert result.returncode == 0, result.stderr
    assert stack_deploy_call(server).endswith(" gerador-planos")


def test_the_stack_name_with_windows_line_endings_is_read_whole(server):
    server.write_app_env("STACK_NAME=leadkit-test\r")

    result = deploy(server)

    assert result.returncode == 0, result.stderr
    assert stack_deploy_call(server).endswith(" leadkit-test")


def test_the_same_stack_in_the_environment_is_accepted(server):
    server.write_app_env("STACK_NAME=leadkit-test")

    result = deploy(server, STACK="leadkit-test")

    assert result.returncode == 0, result.stderr
    assert stack_deploy_call(server).endswith(" leadkit-test")


@pytest.mark.parametrize(
    ("in_folder", "in_environment"),
    [
        ("STACK_NAME=leadkit-test", "gerador-planos"),
        ("STACK_NAME=gerador-planos", "leadkit-test"),
        ("", "leadkit-test"),
    ],
    ids=[
        "dev-folder-with-default-stack",
        "default-folder-with-dev-stack",
        "no-name-with-dev-stack",
    ],
)
def test_a_stack_in_the_environment_that_differs_from_the_folder_stops_before_touching_anything(
    server, in_folder, in_environment
):
    server.write_app_env(in_folder)

    result = deploy(server, STACK=in_environment)

    assert result.returncode == 1
    assert f"STACK={in_environment} difere do STACK_NAME do app.env" in result.stderr
    assert "nada foi mexido" in result.stderr
    assert not server.touched_the_swarm()


@pytest.mark.parametrize("name", ["Planos Dev", "planos_dev", "-planos", "planos.dev", "a" * 41])
def test_an_invalid_stack_name_in_the_folder_stops_before_touching_anything(server, name):
    server.write_app_env(f"STACK_NAME={name}")

    result = deploy(server)

    assert result.returncode == 1
    assert "STACK_NAME" in result.stderr
    assert not server.touched_the_swarm()


def test_a_live_router_for_another_domain_stops_before_touching_anything(server):
    # A pasta errada: a configuração de outra stack, com o domínio dela, sobre uma stack no ar.
    result = deploy(server, STUB_NETWORK="1", STUB_LIVE_RULE="Host(`producao.example.test`)")

    assert result.returncode == 1
    assert "producao.example.test" in result.stderr
    assert DOMAIN in result.stderr
    assert "gerador-planos" in result.stderr
    assert "nada foi mexido" in result.stderr
    assert not server.touched_the_swarm()


def test_the_live_router_is_asked_of_the_stack_of_the_folder(server):
    server.write_app_env("STACK_NAME=leadkit-test")

    deploy(server, STUB_NETWORK="1", STUB_LIVE_RULE=f"Host(`{DOMAIN}`)")

    asked = [call for call in server.calls if "Spec.Labels" in call]
    assert len(asked) == 1
    assert "traefik.http.routers.leadkit-test.rule" in asked[0]
    assert asked[0].endswith(" leadkit-test_api")


def test_a_live_router_for_the_same_domain_lets_the_deploy_go_on(server):
    result = deploy(server, STUB_NETWORK="1", STUB_LIVE_RULE=f"Host(`{DOMAIN}`)")

    assert result.returncode == 0, result.stderr
    assert stack_deploy_call(server).endswith(" gerador-planos")
    assert "--network gerador-planos_internal" in migrate_call(server)


def test_without_settings_in_the_app_env_the_stack_file_defaults_apply(server):
    result = deploy(server)

    assert result.returncode == 0, result.stderr
    assert server.settings == {}


def test_the_rate_limit_of_the_app_env_reaches_the_stack_deploy(server):
    server.write_app_env("RATE_LIMIT_AVERAGE=30", "RATE_LIMIT_BURST=60")

    result = deploy(server)

    assert result.returncode == 0, result.stderr
    assert server.settings == {"RATE_LIMIT_AVERAGE": "30", "RATE_LIMIT_BURST": "60"}


def test_a_setting_with_windows_line_endings_is_read_whole(server):
    server.write_app_env("RATE_LIMIT_AVERAGE=30\r")

    result = deploy(server)

    assert result.returncode == 0, result.stderr
    assert server.settings == {"RATE_LIMIT_AVERAGE": "30"}


def test_a_blank_setting_in_the_app_env_means_the_default(server):
    server.write_app_env("RATE_LIMIT_AVERAGE=", "RATE_LIMIT_BURST=")

    result = deploy(server)

    assert result.returncode == 0, result.stderr
    assert server.settings == {}


def test_a_setting_only_in_the_environment_is_accepted(server):
    result = deploy(server, RATE_LIMIT_BURST="40")

    assert result.returncode == 0, result.stderr
    assert server.settings == {"RATE_LIMIT_BURST": "40"}


def test_the_same_setting_in_the_environment_is_accepted(server):
    server.write_app_env("RATE_LIMIT_BURST=40")

    result = deploy(server, RATE_LIMIT_BURST="40")

    assert result.returncode == 0, result.stderr
    assert server.settings == {"RATE_LIMIT_BURST": "40"}


def test_a_setting_in_the_environment_that_differs_from_the_folder_stops_before_touching_anything(
    server,
):
    server.write_app_env("RATE_LIMIT_BURST=40")

    result = deploy(server, RATE_LIMIT_BURST="5")

    assert result.returncode == 1
    assert "RATE_LIMIT_BURST=5 difere do app.env (40)" in result.stderr
    assert "nada foi mexido" in result.stderr
    assert not server.touched_the_swarm()


@pytest.mark.parametrize("name", ["RATE_LIMIT_AVERAGE", "RATE_LIMIT_BURST"])
@pytest.mark.parametrize("value", ["abc", "0", "-5", "1.5", "10 20", "1000000000", "10/min"])
def test_an_invalid_rate_limit_stops_before_touching_anything(server, name, value):
    # Um valor que o Traefik não entende descarta o middleware, e o router dos envios sai do ar.
    server.write_app_env(f"{name}={value}")

    result = deploy(server)

    assert result.returncode == 1
    assert name in result.stderr
    assert "não serve" in result.stderr
    assert not server.touched_the_swarm()


MEMORY_NAMES = [
    "API_MEMORY_LIMIT",
    "API_MEMORY_RESERVATION",
    "WORKER_MEMORY_LIMIT",
    "WORKER_MEMORY_RESERVATION",
    "DB_MEMORY_LIMIT",
    "DB_MEMORY_RESERVATION",
]


def test_the_memory_values_of_the_app_env_reach_the_stack_deploy(server):
    values = {
        "API_MEMORY_LIMIT": "768M",
        "WORKER_MEMORY_LIMIT": "2G",
        "DB_MEMORY_RESERVATION": "512m",
    }
    server.write_app_env(*(f"{name}={value}" for name, value in values.items()))

    result = deploy(server)

    assert result.returncode == 0, result.stderr
    assert server.settings == values


@pytest.mark.parametrize("value", ["512M", "1G", "1536m", "2g", "256MB", "1GB", "536870912", "64k"])
def test_a_memory_size_docker_understands_is_accepted(server, value):
    server.write_app_env(f"API_MEMORY_LIMIT={value}")

    result = deploy(server)

    assert result.returncode == 0, result.stderr
    assert server.settings == {"API_MEMORY_LIMIT": value}


@pytest.mark.parametrize("name", MEMORY_NAMES)
@pytest.mark.parametrize("value", ["abc", "0", "0M", "-1G", "1.5G", "512 M", "G", "512MiB", "1T"])
def test_an_invalid_memory_size_stops_before_touching_anything(server, name, value):
    # Um tamanho que o Docker não lê derruba o `docker stack deploy` no meio, depois das migrations.
    server.write_app_env(f"{name}={value}")

    result = deploy(server)

    assert result.returncode == 1
    assert name in result.stderr
    assert "não serve" in result.stderr
    assert not server.touched_the_swarm()


def test_a_memory_size_in_the_environment_that_differs_from_the_folder_stops_before_touching_anything(
    server,
):
    server.write_app_env("DB_MEMORY_LIMIT=2G")

    result = deploy(server, DB_MEMORY_LIMIT="1G")

    assert result.returncode == 1
    assert "DB_MEMORY_LIMIT=1G difere do app.env (2G)" in result.stderr
    assert not server.touched_the_swarm()


def test_the_api_trusts_the_subnet_of_the_traefik_network_by_default(server):
    result = deploy(server)

    assert result.returncode == 0, result.stderr
    assert server.forwarded_allow_ips == "10.0.1.0/24"
    assert "FORWARDED_ALLOW_IPS=10.0.1.0/24" in result.stdout


def test_every_subnet_of_the_traefik_network_is_trusted(server):
    result = deploy(server, STUB_TRAEFIK_SUBNETS="10.0.1.0/24,fd00:aaaa::/64")

    assert result.returncode == 0, result.stderr
    assert server.forwarded_allow_ips == "10.0.1.0/24,fd00:aaaa::/64"


def test_the_subnet_is_read_from_the_traefik_network_with_a_go_template(server):
    deploy(server)

    asked = next(call for call in server.calls if "traefik-public" in call)
    assert asked.startswith("network inspect traefik-public --format ")
    assert ".IPAM.Config" in asked
    assert ".Subnet" in asked


@pytest.mark.parametrize("value", ["*", "10.9.0.0/16", "10.9.0.0/16,172.20.0.5"])
def test_a_forwarded_allow_ips_in_the_app_env_wins_and_skips_the_network_lookup(server, value):
    server.write_app_env(f"FORWARDED_ALLOW_IPS={value}")

    result = deploy(server)

    assert result.returncode == 0, result.stderr
    assert server.forwarded_allow_ips == value
    assert not server.asked_for_the_traefik_network()


def test_a_blank_forwarded_allow_ips_in_the_app_env_means_the_traefik_network(server):
    server.write_app_env("FORWARDED_ALLOW_IPS=")

    result = deploy(server)

    assert result.returncode == 0, result.stderr
    assert server.forwarded_allow_ips == "10.0.1.0/24"


def test_without_the_traefik_network_the_deploy_stops_before_touching_anything(server):
    result = deploy(server, STUB_NO_TRAEFIK_NETWORK="1")

    assert result.returncode == 1
    assert "traefik-public" in result.stderr
    assert "FORWARDED_ALLOW_IPS" in result.stderr
    assert "nada foi mexido" in result.stderr
    assert not server.touched_the_swarm()


def test_a_network_without_a_subnet_stops_before_touching_anything(server):
    result = deploy(server, STUB_TRAEFIK_SUBNETS="")

    assert result.returncode == 1
    assert "sub-rede" in result.stderr
    assert not server.touched_the_swarm()


def test_a_forwarded_allow_ips_in_the_environment_that_differs_from_the_folder_stops(server):
    server.write_app_env("FORWARDED_ALLOW_IPS=10.9.0.0/16")

    result = deploy(server, FORWARDED_ALLOW_IPS="*")

    assert result.returncode == 1
    assert "FORWARDED_ALLOW_IPS=* difere do app.env (10.9.0.0/16)" in result.stderr
    assert not server.touched_the_swarm()


def test_a_forwarded_allow_ips_with_spaces_stops_before_touching_anything(server):
    server.write_app_env("FORWARDED_ALLOW_IPS=10.9.0.0/16 172.20.0.5")

    result = deploy(server)

    assert result.returncode == 1
    assert "FORWARDED_ALLOW_IPS" in result.stderr
    assert "não serve" in result.stderr
    assert not server.touched_the_swarm()


def backup(server: Server, **env: str) -> subprocess.CompletedProcess[str]:
    server.install("backup.sh")
    return server.run("backup.sh", BACKUP_DIR=str(server.folder / "dumps"), **env)


def test_the_backup_dumps_the_database_of_the_default_stack(server):
    result = backup(server, STUB_DB_SERVICE="gerador-planos_db")

    assert result.returncode == 0, result.stderr
    dumps = list((server.folder / "dumps").glob("mcc-*.dump"))
    assert [dump.read_text() for dump in dumps] == ["dump"]


def test_the_backup_dumps_the_database_of_the_stack_it_is_told(server):
    result = backup(server, STACK="leadkit-test", STUB_DB_SERVICE="leadkit-test_db")

    assert result.returncode == 0, result.stderr
    assert [dump.read_text() for dump in (server.folder / "dumps").glob("mcc-*.dump")] == ["dump"]


def test_the_backup_of_a_stack_without_database_fails_naming_the_service(server):
    # Só a stack padrão existe: o backup de outro nome não pode cair no banco dela.
    result = backup(server, STACK="leadkit-test", STUB_DB_SERVICE="gerador-planos_db")

    assert result.returncode == 1
    assert "leadkit-test_db" in result.stderr
    assert not list((server.folder / "dumps").glob("mcc-*.dump"))


def test_the_backup_refuses_an_invalid_stack_name(server):
    result = backup(server, STACK="../outro", STUB_DB_SERVICE="gerador-planos_db")

    assert result.returncode == 1
    assert "STACK" in result.stderr


def test_first_adoption_stops_both_old_services_before_migration(server):
    result = deploy(server, STUB_NETWORK="1", STUB_CONTRACT_FLOOR="0")
    assert result.returncode == 0, result.stderr
    scale = next(i for i, call in enumerate(server.calls) if call.startswith("service scale "))
    migration = next(
        i
        for i, call in enumerate(server.calls)
        if call.startswith("run ") and call.endswith(" migrate")
    )
    assert scale < migration
    assert "gerador-planos_api=0" in server.calls[scale]
    assert "gerador-planos_worker=0" in server.calls[scale]


@pytest.mark.parametrize("reaped", ["container", "task"])
def test_first_adoption_proves_exit_when_swarm_reaps_stopped_containers(server, reaped):
    result = deploy(server, STUB_NETWORK="1", STUB_CONTRACT_FLOOR="0", STUB_REAP=reaped)
    assert result.returncode == 0, result.stderr
    assert rollout_state(server)["phase"] == "verified"
    assert any(call.startswith("events ") for call in server.calls)
    retired = rollout_state(server)["attested"]["services"]["api"]["retired"]
    assert retired and all(not row["running"] for row in retired)


@pytest.mark.parametrize("boundary", ["wait", "inspect"])
@pytest.mark.parametrize("outcome", ["removed", "terminal"])
def test_first_adoption_refreshes_inventory_when_container_disappears_during_observation(
    server, boundary, outcome
):
    result = deploy(
        server,
        STUB_NETWORK="1",
        STUB_CONTRACT_FLOOR="0",
        STUB_REAP_RACE=boundary,
        STUB_RACE_OUTCOME=outcome,
        STUB_WAIT_FAULT="error",
    )
    assert result.returncode == 0, result.stderr
    state = rollout_state(server)
    assert state["removed_during_observation"]
    assert state["phase"] == "verified"
    assert any(call.startswith("events ") for call in server.calls)


def test_second_quiesce_collection_refuses_late_running_task_before_ddl(server):
    result = deploy(
        server, STUB_NETWORK="1", STUB_CONTRACT_FLOOR="0", STUB_REAP_RACE="wait", STUB_LATE_TASK="1"
    )
    state = rollout_state(server)
    assert state["missing_api_inspects"] >= 2
    assert not any(
        call.startswith("run ") and call.endswith(" migrate") for call in server.calls
    ), {"late_running_at_ddl": state.get("late_running_at_ddl")}
    assert result.returncode == 1
    assert state["containers"][state["late_live_cid"]]["State"]["Running"]
    assert state["floor"] == 0 and "attested" not in state
    assert "Traceback" not in result.stdout + result.stderr


@pytest.mark.parametrize("boundary", ["wait", "inspect"])
@pytest.mark.parametrize(
    "fault",
    [
        {"STUB_RACE_OUTCOME": "active"},
        {"STUB_RACE_OUTCOME": "extra"},
        {"STUB_REFRESH_FAIL": "1"},
        {"STUB_REFRESH_INSPECT_FAIL": "1", "STUB_RACE_OUTCOME": "terminal"},
        {"STUB_REFRESH_LOCAL_FAIL": "1"},
    ],
)
def test_disappearance_race_with_active_or_unobserved_inventory_keeps_gate_closed(
    server, boundary, fault
):
    result = deploy(
        server, STUB_NETWORK="1", STUB_CONTRACT_FLOOR="0", STUB_REAP_RACE=boundary, **fault
    )
    assert result.returncode == 1
    assert rollout_state(server)["removed_during_observation"]
    assert not any(call.startswith("run ") and call.endswith(" migrate") for call in server.calls)
    assert "attested" not in rollout_state(server)
    assert "Traceback" not in result.stdout + result.stderr


def test_first_adoption_refuses_changed_image_for_previously_observed_container(server):
    result = deploy(
        server, STUB_NETWORK="1", STUB_CONTRACT_FLOOR="0", STUB_OBSERVED_IMAGE_DRIFT="1"
    )
    assert result.returncode == 1
    assert not any(call.startswith("run ") and call.endswith(" migrate") for call in server.calls)
    assert "attested" not in rollout_state(server)


@pytest.mark.parametrize("partial", ["omitted", "duplicate", "wrong", "requested_duplicate"])
def test_disappearance_race_refuses_partial_task_inventory_even_with_zero_exit(server, partial):
    result = deploy(
        server,
        STUB_NETWORK="1",
        STUB_CONTRACT_FLOOR="0",
        STUB_REAP_RACE="wait",
        STUB_RACE_OUTCOME="terminal",
        STUB_REFRESH_PARTIAL=partial,
    )
    assert result.returncode == 1
    assert not any(call.startswith("run ") and call.endswith(" migrate") for call in server.calls)
    assert "attested" not in rollout_state(server)


def test_first_adoption_keeps_positive_proof_after_event_history_rotates(server):
    result = deploy(
        server, STUB_NETWORK="1", STUB_CONTRACT_FLOOR="0", STUB_REAP="task", STUB_CONSUME_EVENTS="1"
    )
    assert result.returncode == 0, result.stderr
    assert rollout_state(server)["phase"] == "verified"


@pytest.mark.parametrize(
    "fault",
    [
        {"STUB_NO_DESTROY": "1"},
        {"STUB_EVENTS_FAIL": "1"},
        {"STUB_LOCAL_LIST_FAIL": "1"},
        {"STUB_REAP_ACTIVE": "1"},
        {"STUB_REAP_REMOTE": "1"},
        {"STUB_REAP": "visible"},
        {"STUB_INITIAL_INSPECT_FAIL": "1"},
        {"STUB_NODE_CHANGE": "1"},
        {"STUB_NODE_LOST": "1"},
        *(
            {"STUB_EVENT_FAULT": value}
            for value in ("id", "action", "type", "json", "old", "scope", "future", "time_type")
        ),
    ],
)
def test_removed_container_with_incomplete_exit_proof_keeps_gate_closed(server, fault):
    result = deploy(
        server, STUB_NETWORK="1", STUB_CONTRACT_FLOOR="0", **{"STUB_REAP": "container", **fault}
    )
    assert result.returncode == 1
    assert not any(call.startswith("run ") and call.endswith(" migrate") for call in server.calls)
    assert "attested" not in rollout_state(server)
    assert "SYNTHETIC_PRIVATE_ERROR" not in result.stdout + result.stderr


def rollout_state(server):
    import json

    return json.loads(Path(str(server.calls_file) + ".state").read_text())


@pytest.mark.parametrize(
    "fault", ["STUB_OLD_RUNNING", "STUB_MISSING_CONTAINER", "STUB_REMOTE_NODE", "STUB_NODE_DOWN"]
)
def test_unobserved_or_live_old_processes_stop_bootstrap_before_ddl(server, fault):
    result = deploy(server, STUB_NETWORK="1", STUB_CONTRACT_FLOOR="0", **{fault: "1"})
    assert result.returncode == 1
    assert not any(call.startswith("run ") and call.endswith(" migrate") for call in server.calls)
    assert "attested" not in rollout_state(server)


@pytest.mark.parametrize(
    "fault", ["STUB_MIGRATE_FAIL", "STUB_STACK_FAIL", "STUB_AUTO_ROLLBACK", "STUB_FORCE_FAIL"]
)
def test_failed_initial_adoption_never_restores_old_active_replicas(server, fault):
    result = deploy(server, STUB_NETWORK="1", STUB_CONTRACT_FLOOR="0", **{fault: "1"})
    assert result.returncode == 1
    state = rollout_state(server)
    assert state["floor"] == 1
    assert state.get("phase", "blocked") == "blocked"
    assert "attested" not in state
    for service in state["services"].values():
        spec = service["Spec"]
        if spec["TaskTemplate"]["ContainerSpec"]["Image"] == "leadkit:old":
            assert spec["Mode"]["Replicated"]["Replicas"] == 0
        previous = service.get("PreviousSpec")
        if previous and previous["TaskTemplate"]["ContainerSpec"]["Image"] == "leadkit:old":
            assert previous["Mode"]["Replicated"]["Replicas"] == 0


@pytest.mark.parametrize("fault", ["STUB_PREVIOUS_OLD", "STUB_MISSING_REPLICA", "STUB_SPEC_DRIFT"])
def test_incomplete_fleet_never_reaches_operational_attestation(server, fault):
    result = deploy(server, STUB_NETWORK="1", **{fault: "1"})
    assert result.returncode == 1
    assert rollout_state(server).get("phase") == "blocked"
    assert "attested" not in rollout_state(server)


def test_old_target_is_refused_before_mutation_or_migration(server):
    result = deploy(server, STUB_NETWORK="1", STUB_TARGET_OLD="1", ROLLBACK="1")
    assert result.returncode == 1
    assert not any(
        call.startswith(("stack deploy", "service scale", "service update", "image tag"))
        for call in server.calls
    )
    assert not any(call.endswith((" migrate", " begin", " attest")) for call in server.calls)


def test_compatible_update_and_rollback_keep_rolling_and_preserve_floor(server):
    for extra in ({}, {"ROLLBACK": "1"}):
        result = deploy(server, STUB_NETWORK="1", **extra)
        assert result.returncode == 0, result.stderr
        assert rollout_state(server)["phase"] == "verified"
        assert rollout_state(server)["floor"] == 1
    assert not any(call.startswith("service scale ") for call in server.calls)
    assert "sem migrations" in result.stdout


def test_restart_resumes_quiescent_bootstrap_after_committed_migration(server):
    failed = deploy(server, STUB_NETWORK="1", STUB_CONTRACT_FLOOR="0", STUB_STACK_FAIL="1")
    assert failed.returncode == 1
    state = rollout_state(server)
    assert state["floor"] == 1
    assert all(s["Spec"]["Mode"]["Replicated"]["Replicas"] == 0 for s in state["services"].values())
    result = deploy(server, STUB_NETWORK="1")
    assert result.returncode == 0, result.stderr
    assert rollout_state(server)["phase"] == "verified"


def test_rollout_correlates_full_ids_when_docker_ps_truncates_by_default(server):
    result = deploy(server, STUB_NETWORK="1")
    assert result.returncode == 0, result.stderr
    state = rollout_state(server)
    assert state["phase"] == "verified"
    services = state["attested"]["services"]
    assert all(
        len(task["container_id"]) == 64
        for service in services.values()
        for task in service["tasks"]
    )


def test_contract_upgrade_quiesces_before_ddl_and_attests_composed_return_path(server):
    result = deploy(server, STUB_NETWORK="1", STUB_TARGET_CONTRACT="2")
    assert result.returncode == 0, result.stderr
    calls = server.calls
    scale = next(i for i, call in enumerate(calls) if call.startswith("service scale "))
    migration = next(i for i, call in enumerate(calls) if call.endswith(" migrate"))
    assert scale < migration
    begins = [i for i, call in enumerate(calls) if call.endswith(" begin")]
    assert begins[0] < scale < migration < begins[-1]
    state = rollout_state(server)
    assert state["floor"] == 2 and state["phase"] == "verified"
    for service in state["attested"]["services"].values():
        assert service["contract"] == service["previous_contract"] == 2


def test_rollback_cannot_upgrade_contract_floor(server):
    result = deploy(server, STUB_NETWORK="1", STUB_TARGET_CONTRACT="2", ROLLBACK="1")
    assert result.returncode == 1
    assert not any(
        call.startswith(("image tag", "service scale", "stack deploy", "service update"))
        for call in server.calls
    )
    assert not any(call.endswith((" migrate", " begin", " attest")) for call in server.calls)


def test_contract_two_rolling_update_preserves_floor(server):
    result = deploy(server, STUB_NETWORK="1", STUB_CONTRACT_FLOOR="2", STUB_TARGET_CONTRACT="2")
    assert result.returncode == 0, result.stderr
    assert not any(call.startswith("service scale ") for call in server.calls)
    assert rollout_state(server)["floor"] == 2


@pytest.mark.parametrize("fault", ["STUB_STACK_FAIL", "STUB_AUTO_ROLLBACK"])
def test_upgrade_failure_after_ddl_keeps_old_return_paused_and_resumes(server, fault):
    failed = deploy(server, STUB_NETWORK="1", STUB_TARGET_CONTRACT="2", **{fault: "1"})
    assert failed.returncode == 1
    state = rollout_state(server)
    assert state["floor"] == 2 and state["phase"] == "blocked"
    for service in state["services"].values():
        for spec in (service["Spec"], service.get("PreviousSpec")):
            if spec and spec["TaskTemplate"]["ContainerSpec"]["Image"] == "leadkit:one":
                assert spec["Mode"]["Replicated"]["Replicas"] == 0
    result = deploy(server, STUB_NETWORK="1", STUB_TARGET_CONTRACT="2")
    assert result.returncode == 0, result.stderr
    assert rollout_state(server)["floor"] == 2
    assert rollout_state(server)["phase"] == "verified"


@pytest.mark.parametrize("rollback", ["0", "1"])
def test_contract_one_target_is_refused_at_floor_two_before_mutation(server, rollback):
    result = deploy(server, STUB_NETWORK="1", STUB_CONTRACT_FLOOR="2", ROLLBACK=rollback)
    assert result.returncode == 1
    assert not any(
        call.startswith(("image tag", "service scale", "stack deploy", "service update"))
        for call in server.calls
    )
    assert not any(call.endswith((" migrate", " begin", " attest")) for call in server.calls)


def test_paused_old_return_requires_quiescence_even_after_floor_commit(server):
    result = deploy(
        server,
        STUB_NETWORK="1",
        STUB_CONTRACT_FLOOR="2",
        STUB_INITIAL_CONTRACT="1",
        STUB_TARGET_CONTRACT="2",
    )
    assert result.returncode == 0, result.stderr
    assert any(call.startswith("service scale ") for call in server.calls)


def test_contract_two_recovery_normalizes_return_consumed_by_automatic_rollback(server):
    environment = {"STUB_NETWORK": "1", "STUB_CONTRACT_FLOOR": "2", "STUB_TARGET_CONTRACT": "2"}
    failed = deploy(server, **environment, STUB_AUTO_ROLLBACK="1")
    assert failed.returncode == 1
    state = rollout_state(server)
    assert state["floor"] == 2 and state["phase"] == "blocked"
    assert all("PreviousSpec" not in service for service in state["services"].values())
    checkpoint = len(server.calls)
    recovered = deploy(server, **environment)
    assert recovered.returncode == 0, recovered.stderr
    calls = server.calls[checkpoint:]
    assert next(i for i, call in enumerate(calls) if call.startswith("service scale ")) < next(
        i for i, call in enumerate(calls) if call.endswith(" migrate")
    )
    state = rollout_state(server)
    assert state["floor"] == 2 and state["phase"] == "verified"
    for service in state["attested"]["services"].values():
        assert service["contract"] == service["previous_contract"] == 2
        assert service["retired"] and all(not row["running"] for row in service["retired"])


def test_bootstrap_preserves_inspected_exit_after_removal_without_destroy_event(server):
    result = deploy(
        server,
        STUB_NETWORK="1",
        STUB_CONTRACT_FLOOR="0",
        STUB_TARGET_CONTRACT="2",
        STUB_REAP_AFTER_STOP="1",
        STUB_NO_DESTROY="1",
    )
    assert result.returncode == 0, result.stderr
    state = rollout_state(server)
    assert state["floor"] == 2 and state["phase"] == "verified"
    for service in state["attested"]["services"].values():
        assert service["contract"] == service["previous_contract"] == 2
        assert service["retired"] and all(not row["running"] for row in service["retired"])


@pytest.mark.parametrize("reap", ["", "1"])
def test_bootstrap_waits_for_terminal_task_after_positive_process_exit(server, reap):
    result = deploy(
        server,
        STUB_NETWORK="1",
        STUB_CONTRACT_FLOOR="0",
        STUB_TASK_LAG="1",
        STUB_REAP_AFTER_STOP=reap,
        STUB_RACE_OUTCOME="active",
        STUB_NO_DESTROY="1",
    )
    assert result.returncode == 0, result.stderr
    state = rollout_state(server)
    assert state["phase"] == "verified"
    assert state["waited"] and all(count >= 3 for count in state["waited"].values())


@pytest.mark.parametrize("no_destroy", ["", "1"])
def test_bootstrap_waits_for_terminal_task_after_destroy_during_wait(server, no_destroy):
    result = deploy(
        server,
        STUB_NETWORK="1",
        STUB_CONTRACT_FLOOR="0",
        STUB_TASK_LAG="1",
        STUB_REAP_RACE="wait",
        STUB_RACE_OUTCOME="active",
        STUB_NO_DESTROY=no_destroy,
        STUB_WAIT_FAULT="error",
    )
    assert result.returncode == (1 if no_destroy else 0), result.stderr
    if no_destroy:
        assert not any(call.endswith((" migrate", " attest")) for call in server.calls)
    else:
        state = rollout_state(server)
        assert state["phase"] == "verified"
        assert state["waited"] and all(count >= 3 for count in state["waited"].values())


@pytest.mark.parametrize("exit_code", ["0", "137", "255"])
def test_positive_wait_preserves_exit_after_removal_without_destroy(server, exit_code):
    result = deploy(
        server,
        STUB_NETWORK="1",
        STUB_CONTRACT_FLOOR="0",
        STUB_REAP_RACE="wait",
        STUB_RACE_OUTCOME="terminal",
        STUB_NO_DESTROY="1",
        STUB_WAIT_CODE=exit_code,
    )
    assert result.returncode == 0, result.stderr
    assert rollout_state(server)["phase"] == "verified"


@pytest.mark.parametrize(
    "fault", ["error", "no_such", "empty", "invalid", "multiple", "negative", "range", "stderr"]
)
def test_invalid_wait_cannot_authorize_removed_container_without_other_proof(server, fault):
    result = deploy(
        server,
        STUB_NETWORK="1",
        STUB_CONTRACT_FLOOR="0",
        STUB_REAP_RACE="wait",
        STUB_RACE_OUTCOME="terminal",
        STUB_NO_DESTROY="1",
        STUB_WAIT_FAULT=fault,
    )
    assert result.returncode == 1
    assert not any(call.endswith((" migrate", " attest")) for call in server.calls)
    assert "SYNTHETIC_PRIVATE_ERROR" not in result.stdout + result.stderr


@pytest.mark.parametrize("fault", ["missing", "invalid", "future"])
def test_wait_requires_known_execution_start_before_call(server, fault):
    result = deploy(
        server,
        STUB_NETWORK="1",
        STUB_CONTRACT_FLOOR="0",
        STUB_REAP_RACE="wait",
        STUB_RACE_OUTCOME="terminal",
        STUB_NO_DESTROY="1",
        STUB_RUNNING_START_FAULT=fault,
    )
    assert result.returncode == 1
    assert not any(call.startswith("container wait") for call in server.calls)
    assert not any(call.endswith((" migrate", " attest")) for call in server.calls)
    assert "SYNTHETIC_PRIVATE_ERROR" not in result.stdout + result.stderr


def test_wait_timeout_does_not_preserve_exit_proof(rollout_client, server, monkeypatch):
    from deploy.swarm.rollout import RolloutError

    monkeypatch.setenv("STUB_REAP_RACE", "wait")
    monkeypatch.delenv("STUB_REAP_AFTER_STOP")
    original = subprocess.run

    def docker_run(args, **kwargs):
        if args[:3] == ["docker", "container", "wait"]:
            raise subprocess.TimeoutExpired(args, 660)
        return original(args, **kwargs)

    monkeypatch.setattr("deploy.swarm.rollout.subprocess.run", docker_run)
    with pytest.raises(RolloutError, match="sem conclusão"):
        rollout_client.execute()
    assert not rollout_client.exit_proofs
    assert not any(call.endswith((" migrate", " attest")) for call in server.calls)


@pytest.mark.parametrize("floor", ["0", "1"])
def test_mandatory_pause_does_not_resolve_missing_return_before_quiescence(server, floor):
    result = deploy(
        server,
        STUB_NETWORK="1",
        STUB_CONTRACT_FLOOR=floor,
        STUB_TARGET_CONTRACT="2",
        STUB_INITIAL_PREVIOUS_MISSING="1",
        STUB_OLD_IMAGE_UNRESOLVABLE="1",
    )
    assert result.returncode == 0, result.stderr
    state = rollout_state(server)
    assert state["floor"] == 2 and state["phase"] == "verified"
    for service in state["attested"]["services"].values():
        assert service["contract"] == service["previous_contract"] == 2
    scale = next(i for i, call in enumerate(server.calls) if call.startswith("service scale "))
    migration = next(i for i, call in enumerate(server.calls) if call.endswith(" migrate"))
    assert scale < migration


@pytest.mark.parametrize(
    "fault",
    [
        "id",
        "image",
        "exit_missing",
        "exit_type",
        "exit_range",
        "finished_missing",
        "finished_zero",
        "finished_future",
        "finished_invalid",
        "started_missing",
        "started_zero",
        "started_invalid",
        "started_future",
        "started_after_finish",
        "started_after_finish_ns",
    ],
)
def test_invalid_inspected_exit_blocks_ddl_without_destroy(server, fault):
    result = deploy(
        server,
        STUB_NETWORK="1",
        STUB_CONTRACT_FLOOR="0",
        STUB_REAP_AFTER_STOP="1",
        STUB_NO_DESTROY="1",
        STUB_STOP_FAULT=fault,
    )
    assert result.returncode == 1
    assert not any(call.endswith((" migrate", " attest")) for call in server.calls)
    assert "SYNTHETIC_PRIVATE_ERROR" not in result.stdout + result.stderr


@pytest.fixture
def rollout_client(server, monkeypatch):
    from deploy.swarm.rollout import Rollout

    for key, value in {
        "PATH": f"{server.bin_dir}{os.pathsep}{os.environ['PATH']}",
        "STUB_CALLS": str(server.calls_file),
        "STUB_NETWORK": "1",
        "STUB_CONTRACT_FLOOR": "0",
        "STUB_NO_DESTROY": "1",
        "STUB_REAP_AFTER_STOP": "1",
        "IMAGE": "gerador-planos:abc123",
    }.items():
        monkeypatch.setenv(key, value)
    rollout = Rollout(server.folder, "gerador-planos:abc123", "gerador-planos", False)
    return rollout


def test_inspected_exit_records_execution_start(rollout_client):
    rollout = rollout_client
    rollout.node = "local-node"
    rollout.observe("api")
    rollout.docker("service", "scale", "--detach=false", "gerador-planos_api=0")
    row = rollout.observe("api")[0]
    proof = rollout.exit_proofs[row["container_id"]]
    assert proof["started_at"] == "2026-01-01T00:00:00Z"


@pytest.mark.parametrize(
    "started_at,finished_at",
    [
        ("2025-01-01T00:00:00Z", "2025-02-01T00:00:00Z"),
        ("2025-01-01T00:00:00.000000001Z", "2025-01-01T00:00:00.000000001Z"),
    ],
)
def test_historical_inspected_exit_keeps_execution_window(
    rollout_client, server, monkeypatch, started_at, finished_at
):
    import json

    monkeypatch.delenv("STUB_REAP_AFTER_STOP")
    rollout = rollout_client
    rollout.node = "local-node"
    cid = rollout.observe("api")[0]["container_id"]
    rollout.docker("service", "scale", "--detach=false", "gerador-planos_api=0")
    state = rollout_state(server)
    state["containers"][cid]["State"].update(StartedAt=started_at, FinishedAt=finished_at)
    Path(str(server.calls_file) + ".state").write_text(json.dumps(state))
    assert not rollout.observe("api")[0]["running"]
    proof = rollout.exit_proofs[cid]
    assert (proof["started_at"], proof["finished_at"]) == (started_at, finished_at)


@pytest.mark.parametrize("proof_kind", ["inspect", "wait"])
def test_identical_stopped_snapshots_preserve_seeded_exit_proof(
    rollout_client, monkeypatch, proof_kind
):
    import json

    from deploy.swarm.rollout import Rollout

    monkeypatch.delenv("STUB_REAP_AFTER_STOP")
    if proof_kind == "wait":
        monkeypatch.setenv("STUB_REAP_RACE", "wait")
        monkeypatch.setenv("STUB_RACE_OUTCOME", "terminal")
    rollout = rollout_client
    rollout.node = "local-node"
    rollout.observe("api")
    rollout.docker("service", "scale", "--detach=false", "gerador-planos_api=0")
    rows = rollout.observe("api", wait=proof_kind == "wait")
    snapshot = json.loads(json.dumps({"processes": rows, "exit_proofs": rollout.exit_proofs}))
    if proof_kind == "wait":
        proof = snapshot["exit_proofs"][rows[0]["container_id"]]
        assert proof["kind"] == "wait" and proof["cli_exit_code"] == proof["exit_code"] == 0
        assert "finished_at" not in proof
    # O consumidor conserva o mapa e semeia outra leitora, sem schema fechado da prova.
    reader = Rollout(rollout.folder, rollout.image, rollout.stack, False)
    reader.node = rollout.node
    reader.observed_since = rollout.observed_since
    reader.exit_proofs = dict(snapshot["exit_proofs"])
    for row in rows:
        reader.observed_images[row["container_id"]] = row["image"]
        reader.tracked["api"][row["id"]] = {
            "ID": row["id"],
            "NodeID": row["node_id"],
            "Status": {
                "State": row["state"],
                "ContainerStatus": {"ContainerID": row["container_id"]},
            },
        }
    assert {"processes": reader.observe("api"), "exit_proofs": reader.exit_proofs} == snapshot


def test_new_stopped_execution_replaces_previous_exit_proof(rollout_client, server, monkeypatch):
    import json

    monkeypatch.delenv("STUB_REAP_AFTER_STOP")
    rollout = rollout_client
    rollout.node = "local-node"
    rollout.observe("api")
    rollout.docker("service", "scale", "--detach=false", "gerador-planos_api=0")
    row = rollout.observe("api")[0]
    cid = row["container_id"]
    previous = dict(rollout.exit_proofs[cid])
    state = rollout_state(server)
    state["containers"][cid]["State"]["StartedAt"] = "2026-02-01T00:00:00Z"
    Path(str(server.calls_file) + ".state").write_text(json.dumps(state))
    rollout.observe("api")
    proof = rollout.exit_proofs[cid]
    assert proof["started_at"] == "2026-02-01T00:00:00Z"
    assert proof != previous


@pytest.mark.parametrize("fault", ["started_future", "id", "changed_image"])
def test_contradictory_inspection_invalidates_exit_proof_before_removal(
    rollout_client, server, monkeypatch, fault
):
    import json

    from deploy.swarm.rollout import RolloutError

    monkeypatch.delenv("STUB_REAP_AFTER_STOP")
    rollout = rollout_client
    rollout.node = "local-node"
    rollout.observe("api")
    rollout.docker("service", "scale", "--detach=false", "gerador-planos_api=0")
    cid = rollout.observe("api")[0]["container_id"]
    if fault == "changed_image":
        state = rollout_state(server)
        state["containers"][cid]["Image"] = "sha256:" + "f" * 64
        Path(str(server.calls_file) + ".state").write_text(json.dumps(state))
    else:
        monkeypatch.setenv("STUB_STOP_FAULT", fault)
    with pytest.raises(RolloutError):
        rollout.observe("api")
    monkeypatch.delenv("STUB_STOP_FAULT", raising=False)
    state = rollout_state(server)
    del state["containers"][cid]
    Path(str(server.calls_file) + ".state").write_text(json.dumps(state))
    with pytest.raises(RolloutError, match="Falta prova local"):
        rollout.observe("api")


@pytest.mark.parametrize(
    "field,value",
    [
        ("container_id", "f" * 64),
        ("image", "sha256:" + "f" * 64),
        ("node_id", "other-node"),
        ("task_id", "other-task"),
        ("since", 0),
        ("observed_at", 0),
        ("exit_code", True),
        ("started_at", "invalid"),
        ("finished_at", "invalid"),
        ("extra", 1),
        ("cli_exit_code", 1),
        ("wait_started_at", 0),
    ],
)
@pytest.mark.parametrize("proof_kind", ["inspect", "wait"])
def test_altered_inspected_proof_cannot_authorize_removed_container(
    rollout_client,
    monkeypatch,
    field,
    value,
    proof_kind,
):
    from deploy.swarm.rollout import RolloutError

    rollout = rollout_client
    if proof_kind == "wait":
        monkeypatch.delenv("STUB_REAP_AFTER_STOP")
        monkeypatch.setenv("STUB_REAP_RACE", "wait")
        monkeypatch.setenv("STUB_RACE_OUTCOME", "terminal")
    rollout.node = "local-node"
    rollout.observe("api")
    rollout.docker("service", "scale", "--detach=false", "gerador-planos_api=0")
    rows = rollout.observe("api", wait=proof_kind == "wait")
    cid = rows[0]["container_id"]
    rollout.exit_proofs[cid][field] = value
    with pytest.raises(RolloutError, match="prova de saída"):
        rollout.observe("api")


@pytest.mark.parametrize("reap", ["", "wait", "wait_without_destroy"])
def test_terminal_task_convergence_has_deadline_before_ddl(
    rollout_client, monkeypatch, server, reap
):
    from deploy.swarm.rollout import RolloutError

    monkeypatch.setenv("STUB_TASK_LAG", "forever")
    monkeypatch.setenv("STUB_REAP_RACE", "wait" if reap else "")
    monkeypatch.setenv("STUB_RACE_OUTCOME", "active")
    monkeypatch.delenv("STUB_REAP_AFTER_STOP")
    monkeypatch.delenv("STUB_NO_DESTROY")
    if reap == "wait":
        monkeypatch.setenv("STUB_WAIT_FAULT", "error")
    elif reap == "wait_without_destroy":
        monkeypatch.setenv("STUB_NO_DESTROY", "1")
    clock = [0.0]

    def advance(seconds):
        clock[0] += 10

    # Só o helper usa o relógio falso; subprocess precisa do sleep real no polling de wait.
    monkeypatch.setattr(
        "deploy.swarm.rollout.time",
        SimpleNamespace(monotonic=lambda: clock[0], sleep=advance, time_ns=time.time_ns),
    )
    with pytest.raises(RolloutError, match="estado terminal no prazo"):
        rollout_client.execute()
    assert clock[0] == 30
    assert not any(call.endswith((" migrate", " attest")) for call in server.calls)


def test_process_grace_period_does_not_consume_task_convergence_deadline(
    rollout_client,
    monkeypatch,
    server,
):
    monkeypatch.setenv("STUB_TASK_LAG", "1")
    monkeypatch.delenv("STUB_REAP_AFTER_STOP")
    clock = [0.0]
    run = subprocess.run

    def docker_run(args, **kwargs):
        if args[:3] == ["docker", "container", "wait"]:
            clock[0] += 60
        return run(args, **kwargs)

    monkeypatch.setattr("deploy.swarm.rollout.subprocess.run", docker_run)
    monkeypatch.setattr(
        "deploy.swarm.rollout.time",
        SimpleNamespace(
            monotonic=lambda: clock[0], sleep=lambda seconds: None, time_ns=time.time_ns
        ),
    )
    rollout_client.execute()
    assert rollout_state(server)["phase"] == "verified"


@pytest.mark.parametrize("fault", ["STUB_NODE_CHANGE", "STUB_NODE_LOST", "STUB_LOCAL_LIST_FAIL"])
def test_inspected_exit_does_not_bypass_uncertain_daemon_or_inventory(server, fault):
    result = deploy(
        server,
        STUB_NETWORK="1",
        STUB_CONTRACT_FLOOR="0",
        STUB_REAP_AFTER_STOP="1",
        STUB_NO_DESTROY="1",
        **{fault: "1"},
    )
    assert result.returncode == 1
    assert not any(call.endswith((" migrate", " attest")) for call in server.calls)


@pytest.mark.parametrize("proof_kind", ["inspect", "wait"])
def test_observed_restart_invalidates_exit_proof_before_subsequent_removal(
    rollout_client,
    server,
    monkeypatch,
    proof_kind,
):
    import json

    from deploy.swarm.rollout import RolloutError

    monkeypatch.setenv("STUB_RACE_OUTCOME", "terminal")
    rollout = rollout_client
    if proof_kind == "wait":
        monkeypatch.delenv("STUB_REAP_AFTER_STOP")
        monkeypatch.setenv("STUB_REAP_RACE", "wait")
    rollout.node = "local-node"
    rollout.observe("api")
    rollout.docker("service", "scale", "--detach=false", "gerador-planos_api=0")
    row = rollout.observe("api", wait=proof_kind == "wait")[0]
    cid = row["container_id"]
    state_path = Path(str(server.calls_file) + ".state")
    state = rollout_state(server)
    state["containers"][cid] = {"Image": row["image"], "State": {"Running": True}}
    state_path.write_text(json.dumps(state))
    assert rollout.observe("api")[0]["running"]
    state = rollout_state(server)
    del state["containers"][cid]
    state_path.write_text(json.dumps(state))
    with pytest.raises(RolloutError, match="Falta prova local"):
        rollout.observe("api")
