"""
    pyznap.pyzfs
    ~~~~~~~~~~~~~~

    Python ZFS bindings, forked from https://bitbucket.org/stevedrake/weir/.

    :copyright: (c) 2015-2019 by Stephen Drake, Yannick Boetzel.
    :license: GPLv3, see LICENSE for more details.
"""


import sys
import logging
import subprocess as sp
from shlex import quote
from .process import check_output, DatasetNotFoundError, DatasetBusyError
from .utils import exists


SHELL = ['sh', '-c']

# Use mbuffer if installed on the system
if exists('mbuffer'):
    MBUFFER = lambda mem: ['mbuffer', '-q', '-s', '128K', '-m', '{:d}M'.format(mem)]
else:
    MBUFFER = None

# Use pv if installed on the system
if exists('pv'):
    PV = lambda size: ['pv', '-f', '-w', '100', '-s', str(size)]
else:
    PV = None

class OperationNotAllowedError(Exception):
    pass

def find(path=None, ssh=None, max_depth=None, types=[]):
    """Lists filesystems and snapshots for a given path"""
    cmd = ['zfs', 'list']

    cmd.append('-H')

    if max_depth is None:
        cmd.append('-r')
    elif max_depth >= 0:
        cmd.append('-d')
        cmd.append(str(max_depth))
    else:
        raise TypeError('max_depth must be a non-negative int or None')

    if types:
        cmd.append('-t')
        cmd.append(','.join(types))

    cmd.append('-o')
    cmd.append('name,type')

    if path:
        cmd.append(path)

    out = check_output(cmd, ssh=ssh)

    return [open(name, ssh=ssh, type=type) for name, type in out]


def findprops(path=None, ssh=None, max_depth=None, props=['all'], sources=[], types=[]):
    """Lists all properties of a given filesystem"""
    cmd = ['zfs', 'get']

    cmd.append('-H')
    cmd.append('-p')

    if max_depth is None:
        cmd.append('-r')
    elif max_depth >= 0:
        cmd.append('-d')
        cmd.append(str(max_depth))
    else:
        raise TypeError('max_depth must be a non-negative int or None')

    if types:
        cmd.append('-t')
        cmd.append(','.join(types))

    if sources:
        cmd.append('-s')
        cmd.append(','.join(sources))

    cmd.append(','.join(props))

    if path:
        cmd.append(path)

    out = check_output(cmd, ssh=ssh)

    names = set(map(lambda x: x[0], out))

    # return [dict(name=n, property=p, value=v, source=s) for n, p, v, s in out]
    return {name: {i[1]: (i[2], i[3]) for i in out if i[0] == name} for name in names}


# Factory function for dataset objects
def open(name, ssh=None, type=None):
    """Opens a volume, filesystem or snapshot"""
    if type is None:
        type = findprops(name, ssh=ssh, max_depth=0, props=['type'])[name]['type'][0]

    if type == 'volume':
        return ZFSVolume(name, ssh)

    if type == 'filesystem':
        return ZFSFilesystem(name, ssh)

    if type == 'snapshot':
        return ZFSSnapshot(name, ssh)

    raise ValueError('invalid dataset type %s' % type)


def roots(ssh=None):
    return find(ssh=ssh, max_depth=0)

# note: force means create missing parent filesystems
def create(name, ssh=None, type='filesystem', sparse=True, volsize=None, volblocksize='8k', recordsize='128k', props={}, force_parent=False):
    cmd = ['zfs', 'create']

    if type == 'volume':
        cmd.append('-V')
        cmd.append(str(volsize))

        if sparse:
            cmd.append('-s')

    elif type != 'filesystem':
        raise ValueError('invalid type %s' % type)

    if force_parent:
        cmd.append('-p')

    for prop, value in props.items():
        cmd.append('-o')
        cmd.append(prop + '=' + str(value))

    if type == 'volume':
        if volblocksize:
            if not 'volblocksize' in props:
                cmd.append('-o')
                cmd.append('volblocksize' + '=' + str(volblocksize))
    else:
        if recordsize:
            if not 'recordsize' in props:
                cmd.append('-o')
                cmd.append('recordsize' + '=' + str(recordsize))

    cmd.append(name)

    check_output(cmd, ssh=ssh)

    if type == 'volume':
        return ZFSVolume(name, ssh=ssh)
    else:
        return ZFSFilesystem(name, ssh=ssh)


def receive(name, stdin, ssh=None, ssh_source=None, append_name=False, append_path=False,
            force=False, nomount=False, stream_size=0, raw=False):
    """Returns Popen instance for zfs receive"""
    logger = logging.getLogger(__name__)

    # use minimal mbuffer size of 1 and maximal size of 512 (256 over ssh)
    mbuff_size = min(max(stream_size // 1024**2, 1), 256 if (ssh_source or ssh) else 512)

    # choose shell (sh or ssh) and mbuffer command on local / remote
    if ssh:
        shell = ssh.cmd
        mbuffer = ssh.mbuffer
    else:
        shell = SHELL
        mbuffer = MBUFFER

    # only compress if send is over ssh
    if ssh_source and ssh:
        decompress = ssh_source.decompress if ssh_source.decompress == ssh.decompress else None
    elif ssh_source or ssh:
        decompress = ssh_source.decompress if ssh_source else ssh.decompress
    else:
        decompress = None

    # construct zfs receive command
    cmd = ['zfs', 'receive']

    # cmd.append('-v')

    if append_name:
        cmd.append('-e')
    elif append_path:
        cmd.append('-d')

    if force:
        cmd.append('-F')
    if nomount:
        cmd.append('-u')

    cmd.append(quote(name)) # use shlex to quote the name

    # add additional commands
    if decompress and not raw: # disable compression for raw send
        logger.debug("Using compression on dest: '{:s}'...".format(' '.join(decompress)))
        cmd = decompress + ['|'] + cmd
    # only use mbuffer at recv if send is over ssh
    if (ssh_source or ssh) and mbuffer and stream_size >= 1024**2: # don't use mbuffer if stream size is too small
        logger.debug("Using mbuffer on dest: '{:s}'...".format(' '.join(mbuffer(mbuff_size))))
        cmd = mbuffer(mbuff_size) + ['|'] + cmd

    # execute command with shell (sh or ssh)
    cmd = shell + [' '.join(cmd)]

    return sp.Popen(cmd, stdin=stdin, stderr=sp.PIPE) # zfs receive process


class ZFSDataset(object):
    def __init__(self, name, ssh=None):
        self.name = name
        self.ssh = ssh

    def __str__(self):
        return '{:s}@{:s}:{:s}'.format(self.ssh.user, self.ssh.host, self.name) if self.ssh else self.name

    def __repr__(self):
        name = self.__str__()
        return '{0}({1!r})#{2}'.format(self.__class__.__name__, name, self.guid())

    def __eq__(self, value):
        return self.guid() == value.guid()

    def parent(self):
        parent_name, _, _ = self.name.rpartition('/')
        return open(parent_name, ssh=self.ssh) if parent_name else None

    def guid(self):
        return self.getpropval('guid')

    def filesystems(self, max_depth=1):
        return find(self.name, ssh=self.ssh, max_depth=max_depth, types=['filesystem'])[1:]

    def volumes(self, max_depth=1):
        return find(self.name, ssh=self.ssh, max_depth=max_depth, types=['volume'])

    def snapshots(self, max_depth=1):
        return find(self.name, ssh=self.ssh, max_depth=max_depth, types=['snapshot'])

    def children(self, max_depth=1):
        return find(self.name, ssh=self.ssh, max_depth=max_depth, types=['all'])[1:]

    def clones(self, max_depth=1):
        clones = []
        for clone in [value['clones'][0] for (key, value) in findprops(self.name, ssh=self.ssh, props=['clones'], max_depth=max_depth).items()]:
            if not (clone == '' or clone == '-'):
                clones.extend(clone.split(','))
        
        return [open(clone, ssh=self.ssh) for clone in clones]

    def dependents(self):
        raise NotImplementedError()

    def destroy(self, defer=False, children=False, dependents=False, force_unmount=False):
        cmd = ['zfs', 'destroy']

        cmd.append('-v')

        if isinstance(self, ZFSSnapshot):
            if defer:
                cmd.append('-d')
        else:
            if force_unmount:
                cmd.append('-f')

        if children:
            cmd.append('-r')

        if dependents:
            cmd.append('-R')

        cmd.append(self.name)

        check_output(cmd, ssh=self.ssh)

    def snapshot(self, snapname, recursive=False, props={}):
        cmd = ['zfs', 'snapshot']

        if recursive:
            cmd.append('-r')

        for prop, value in props.items():
            cmd.append('-o')
            cmd.append(prop + '=' + str(value))

        name = self.name + '@' + snapname
        cmd.append(name)

        check_output(cmd, ssh=self.ssh)
        return ZFSSnapshot(name, ssh=self.ssh)

    # TODO: split force to allow -f, -r and -R to be specified individually
    def rollback(self, snapname, destroy_newer=False, destroy_clones=False, force_unmount=False):
        cmd = ['zfs', 'rollback']
        
        if destroy_newer:
            cmd.append('-r')
        if destroy_clones:
            cmd.append('-R')
        if force_unmount:
            cmd.append('-f')

        snap = str(snapname).split('@')
        if len(snap) == 1:
            snap.insert(0, self.name)
        
        cmd.append('@'.join(snap))

        check_output(cmd, ssh=self.ssh)

    def origin(self):
        origin = self.getpropval('origin')
        if origin:
            return open(origin, ssh=self.ssh)

    def promote(self):
        if self.origin() == None:
            return

        cmd = ['zfs', 'promote']
        cmd.append(self.name)

        check_output(cmd, ssh=self.ssh)

    # TODO: split force to allow -f and -p to be specified individually
    def rename(self, name, recursive=False, force_parent=False, force_unmount=False):
        cmd = ['zfs', 'rename']

        if not isinstance(self, ZFSSnapshot):
            if force_parent:
                cmd.append('-p')

        if force_unmount:
            cmd.append('-f')

        cmd.append(self.name)

        target = ''
        if not isinstance(self, ZFSSnapshot):
            target = name
        else:
            target = self.name.split('@')[0] + '@' + name

        cmd.append(target)

        check_output(cmd, ssh=self.ssh)
        return open(target, ssh=self.ssh)

    def getprops(self):
        return findprops(self.name, ssh=self.ssh, max_depth=0)[self.name]

    def getprop(self, prop):
        return findprops(self.name, ssh=self.ssh, max_depth=0, props=[prop])[self.name].get(prop, None)

    def getpropval(self, prop, default=None):
        value = self.getprop(prop)[0]
        return default if value == '-' else value

    def setprop(self, prop, value):
        cmd = ['zfs', 'set']

        cmd.append(prop + '=' + str(value))
        cmd.append(self.name)

        check_output(cmd, ssh=self.ssh)

    def delprop(self, prop, recursive=False):
        cmd = ['zfs', 'inherit']

        if recursive:
            cmd.append('-r')

        cmd.append(prop)
        cmd.append(self.name)

        check_output(cmd, ssh=self.ssh)

class ZFSVolume(ZFSDataset):
    def filesystems(self, *args, **kwargs):
        raise OperationNotAllowedError()

    def volumes(self, *args, **kwargs):
        raise OperationNotAllowedError()

class ZFSFilesystem(ZFSDataset):
    def mount(self, *args, **kwargs):
        raise NotImplementedError()

    def unmount(self, *args, **kwargs):
        raise NotImplementedError()

class ZFSSnapshot(ZFSDataset):
    def snapname(self):
        snapname = self.name.split('@')[-1]
        return snapname

    def parent(self):
        parent_path = self.name.split('@')[0]
        return open(name=parent_path, ssh=self.ssh)

    def snapshots(self, *args, **kwargs):
        raise OperationNotAllowedError()

    def snapshot(self, *args, **kwargs):
        raise OperationNotAllowedError()

    # note: force means create missing parent filesystems
    def clone(self, name, props={}, force_parent=False):
        cmd = ['zfs', 'clone']

        if force_parent:
            cmd.append('-p')
        
        for prop, value in props.items():
            cmd.append('-o')
            cmd.append(prop + '=' + str(value))

        cmd.append(self.name)
        cmd.append(name)

        check_output(cmd, ssh=self.ssh)

    def clones(self):
        clones = self.getpropval('clones').split(',')

        if clones == '':
            return []
        else:
            return [open(name=clone, ssh=self.ssh) for clone in clones]

    def send(self, ssh_dest=None, base=None, intermediates=False, replicate=False,
             properties=False, deduplicate=False, raw=False):
        logger = logging.getLogger(__name__)

        # get the size of the snapshot to send
        stream_size = self.stream_size(base=base)
        # use minimal mbuffer size of 1 and maximal size of 512 (256 over ssh)
        mbuff_size = min(max(stream_size // 1024**2, 1), 256 if (self.ssh or ssh_dest) else 512)

        # choose shell (sh or ssh) and mbuffer, pv commands on local / remote
        if self.ssh:
            shell = self.ssh.cmd
            mbuffer, pv = self.ssh.mbuffer, self.ssh.pv
        else:
            shell = SHELL
            mbuffer, pv = MBUFFER, PV

        # only compress if send is over ssh
        if self.ssh and ssh_dest:
            compress = self.ssh.compress if self.ssh.compress == ssh_dest.compress else None
        elif self.ssh or ssh_dest:
            compress = self.ssh.compress if self.ssh else ssh_dest.compress
        else:
            compress = None

        # construct zfs send command
        cmd = ['zfs', 'send']

        # cmd.append('-v')
        # cmd.append('-P')

        if replicate:
            cmd.append('-R')
        if properties:
            cmd.append('-p')
        if deduplicate:
            cmd.append('-D')
        if raw:
            logger.debug("Using raw zfs send...")
            cmd.append('-w')

        if base is not None:
            if intermediates:
                cmd.append('-I')
            else:
                cmd.append('-i')
            cmd.append(quote(base.name)) # use shlex to quote the name

        cmd.append(quote(self.name)) # use shlex to quote the name

        # add additional commands
        if mbuffer and stream_size >= 1024**2: # don't use mbuffer if stream size is too small
            logger.debug("Using mbuffer on source: '{:s}'...".format(' '.join(mbuffer(mbuff_size))))
            cmd += ['|'] + mbuffer(mbuff_size)

        if pv and stream_size >= 1024**2: # don't use pv if stream size is too small
            pv_cmd = pv(stream_size)
            if not sys.stdout.isatty():
                pv_cmd += ['-D', '60', '-i', '60'] # if stdout is redirected, only update pv every 60s
            logger.debug("Using pv on source: '{:s}'...".format(' '.join(pv_cmd)))
            cmd += ['|'] + pv_cmd

        if compress and not raw: # disable compression for raw send
            logger.debug("Using compression on source: '{:s}'...".format(' '.join(compress)))
            cmd += ['|'] + compress

        # execute command with shell (sh or ssh)
        cmd = shell + [' '.join(cmd)]

        return sp.Popen(cmd, stdout=sp.PIPE, stderr=sp.PIPE) # return zfs send process


    def stream_size(self, base=None):
        # cache stream sizes
        if not hasattr(self, 'stream_cache'):
            self.stream_cache = {}
        elif str(base) in self.stream_cache:
            return self.stream_cache[str(base)]
        else:
            self.stream_cache[str(base)] = 0

        cmd = ['zfs', 'send', '-nvP']

        if base is not None:
            cmd.append('-I')
            cmd.append(base.name)

        cmd.append(self.name)

        try:
            out = check_output(cmd, ssh=self.ssh)
        except (DatasetNotFoundError, DatasetBusyError, sp.CalledProcessError):
            return 0

        try:
            out = out[-1][-1]
            size = int(out.split(' ')[-1])
            self.stream_cache[str(base)] = size
            return size
        except (IndexError, ValueError):
            return 0

    def hold(self, tag, recursive=False):
        cmd = ['zfs', 'hold']

        if recursive:
            cmd.append('-r')

        cmd.append(tag)
        cmd.append(self.name)

        check_output(cmd, ssh=self.ssh)

    def holds(self):
        cmd = ['zfs', 'holds']

        cmd.append('-H')

        cmd.append(self.name)

        out = check_output(cmd, ssh=self.ssh)

        # return hold tag names only
        return [hold[1] for hold in out]

    def release(self, tag, recursive=False):
        cmd = ['zfs', 'release']

        if recursive:
            cmd.append('-r')

        cmd.append(tag)
        cmd.append(self.name)

        check_output(cmd, ssh=self.ssh)
