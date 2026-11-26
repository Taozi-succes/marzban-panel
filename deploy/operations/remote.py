"""Ephemeral source-side helper. Preview never stops or changes the source app."""
import argparse
from contextlib import contextmanager,redirect_stdout
from datetime import datetime,timezone
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import sys

from common import ToolError,run,write_json
from migrate import export_container,inspect_container


def identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-f0-9]{64}',value):raise ToolError('源容器标识无效')
    return value


def inspect_source(value,docker_command):
    reference=value.get('container','')
    if not isinstance(reference,str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}',reference):raise ToolError('容器名称或标识无效')
    source=inspect_container(reference,docker_command)
    ident=identifier(source['Id'])
    if value.get('expected_id') and ident!=value['expected_id']:raise ToolError('源容器已被替换，拒绝继续迁移')
    return source


def receipt_paths(ident):
    root=Path('/tmp')/('marzban-cutover-'+identifier(ident))
    return Path(str(root)+'.lock'),Path(str(root)+'.json')


@contextmanager
def source_lock(ident):
    path,_=receipt_paths(ident)
    fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        if os.fstat(fd).st_uid!=os.geteuid():raise ToolError('迁移锁所有者不匹配')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ToolError('此源容器已有迁移操作正在运行') from None
        yield
    finally:os.close(fd)


def receipt(value,ident):
    migration_id=value.get('migration_id','')
    if not re.fullmatch(r'[a-f0-9]{32}',migration_id):raise ToolError('迁移会话标识无效')
    _,path=receipt_paths(ident)
    if path.is_symlink():raise ToolError('停机收据路径无效')
    if path.exists() and path.stat().st_uid!=os.geteuid():raise ToolError('停机收据所有者不匹配')
    record=json.loads(path.read_text()) if path.exists() else None
    if record and (record.get('migration_id')!=migration_id or record.get('source_container')!=ident):
        raise ToolError('源容器已有其他迁移会话的停机记录')
    return path,record


def operate(operation,value,docker_command=('docker',)):
    if operation=='containers':
        names=run([*docker_command,'ps','-a','--format','{{.Names}}']).decode().splitlines()
        result=[]
        for name in names:
            source=inspect_container(name,docker_command)
            image=source['Config']['Image'].lower()
            service=(source['Config'].get('Labels') or {}).get('com.docker.compose.service','')
            if ('marzban' in image and 'marzban-node' not in image) or service in ('marzban','panel'):
                result.append({'name':source['Name'].lstrip('/'),'id':identifier(source['Id']),'running':source['State']['Running']})
        return result
    source=inspect_source(value,docker_command);ident=source['Id']
    if operation=='inspect':return {'id':ident,'name':source['Name'].lstrip('/'),'running':source['State']['Running']}
    if operation=='fingerprint':
        output=run([*docker_command,'run','--rm','--network','none','--volumes-from',ident+':ro','-v',str(Path(__file__).parent)+':/operator:ro',
                   '--entrypoint','python',source['Image'],'/operator/exporter.py','fingerprint'])
        return {'fingerprint':output.decode().strip()}
    if operation=='export':
        if not source['State']['Running']:raise ToolError('在线预演要求旧主控仍在运行；不会自动启动旧主控')
        return export_container(ident,'preview',value['addresses'],docker_command)
    with source_lock(ident):
        path,record=receipt(value,ident)
        if operation=='freeze':
            if record is None:
                if not source['State']['Running']:raise ToolError('源主控已停止且没有本次停机收据，请人工核对')
                record={'migration_id':value['migration_id'],'source_container':ident,'receipt_id':secrets.token_hex(16),
                        'created_utc':datetime.now(timezone.utc).isoformat(),'phase':'stopping'}
                write_json(path,record)
            if source['State']['Running']:
                if record['phase']=='stopped':raise ToolError('已停机的旧主控又被启动，拒绝继续')
                run([*docker_command,'stop',ident])
            if inspect_container(ident,docker_command)['State']['Running']:raise ToolError('源主控尚未停止')
            record['phase']='stopped';write_json(path,record)
            return record
        if not record or record['phase']!='stopped' or source['State']['Running']:
            raise ToolError('缺少本次有效停机收据，或旧主控又在运行')
        if operation=='stopped':return record
        if operation=='final':
            payload=export_container(ident,'final',value['addresses'],docker_command,record)
            if inspect_container(ident,docker_command)['State']['Running']:raise ToolError('导出期间旧主控又被启动，拒绝接管')
            return payload
    raise ToolError('不支持的源端迁移操作')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('operation',choices=['containers','inspect','fingerprint','export','freeze','final','stopped']);parser.add_argument('--sudo',action='store_true')
    args=parser.parse_args();value=json.loads(sys.stdin.buffer.read(1024*1024))
    # Binary stdout is exclusively the archive. Docker progress belongs on stderr.
    with redirect_stdout(sys.stderr):result=operate(args.operation,value,('sudo','-n','docker') if args.sudo else ('docker',))
    if isinstance(result,bytes):sys.stdout.buffer.write(result)
    else:print(json.dumps(result))


if __name__=='__main__':
    try:main()
    except (ToolError,OSError,ValueError,KeyError) as error:
        print('MIGRATION_ERROR:'+(str(error) if isinstance(error,ToolError) else '源端读取或操作失败'),file=sys.stderr);sys.exit(1)
