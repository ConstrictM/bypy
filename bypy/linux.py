#!/usr/bin/env python
# vim:fileencoding=utf-8
# License: GPLv3 Copyright: 2019, Kovid Goyal <kovid at kovidgoyal.net>

import os
import pwd
import shlex
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from functools import partial
from urllib.request import urlopen

from .conf import parse_conf_file
from .constants import base_dir
from .utils import call, print_cmd, single_instance

DEFAULT_BASE_IMAGE = (
    'https://partner-images.canonical.com/core/'
    'xenial/current/ubuntu-xenial-core-cloudimg-{}-root.tar.gz'
)

arch = '64'
img_path = sw_dir = sources_dir = img_store_path = None
conf = {}


def initialize_env():
    global img_path, img_store_path, sw_dir, sources_dir
    sources_dir = os.path.join(base_dir(), 'b', 'sources-cache')
    os.makedirs(sources_dir, exist_ok=True)
    output_dir = os.path.join(base_dir(), 'b', 'linux', arch)
    os.makedirs(output_dir, exist_ok=True)
    img_path = os.path.abspath(
        os.path.realpath(os.path.join(output_dir, 'chroot')))
    img_store_path = img_path + '.img'
    sw_dir = os.path.join(output_dir, 'sw')
    os.makedirs(sw_dir, exist_ok=True)
    conf.update(parse_conf_file(os.path.join(base_dir(), 'linux.conf')))


def mount_image():
    if not hasattr(mount_image, 'mounted'):
        call('sudo', 'mount', img_store_path, img_path)
    mount_image.mounted = True


def unmount_image():
    if hasattr(mount_image, 'mounted'):
        call('sudo', 'umount', img_path)
        del mount_image.mounted


def cached_download(url):
    bn = os.path.basename(url)
    local = os.path.join('/tmp', bn)
    if not os.path.exists(local):
        print('Downloading', url, '...')
        data = urlopen(url).read()
        with open(local, 'wb') as f:
            f.write(data)
    return local


def copy_terminfo():
    raw = subprocess.check_output(['infocmp']).decode('utf-8').splitlines()[0]
    path = raw.partition(':')[2].strip()
    if path and os.path.exists(path):
        bdir = os.path.basename(os.path.dirname(path))
        dest = os.path.join(img_path, 'usr/share/terminfo', bdir)
        call('sudo', 'mkdir', '-p', dest, echo=False)
        call('sudo', 'cp', '-a', path, dest, echo=False)


def chroot(cmd, as_root=True, for_install=False):
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    print_cmd(['in-chroot'] + cmd)
    user = pwd.getpwuid(os.geteuid()).pw_name
    env = {
        'PATH': '/sbin:/usr/sbin:/usr/local/bin:/bin:/usr/bin',
        'HOME': '/root' if as_root else '/home/' + user,
        'USER': 'root' if as_root else user,
        'TERM': os.environ.get('TERM', 'xterm-256color'),
        'BYPY_ARCH': f'{arch}-bit',
    }
    if for_install:
        env['DEBIAN_FRONTEND'] = 'noninteractive'
    us = [] if as_root else ['--userspec={}:{}'.format(
        os.geteuid(), os.getegid())]
    as_arch = ['linux{}'.format(arch), '--']
    env_cmd = ['env']
    for k, v in env.items():
        env_cmd += [f'{k}={v}']
    cmd = ['sudo', 'chroot'] + us + [img_path] + as_arch + env_cmd + list(cmd)
    copy_terminfo()
    call('sudo', 'cp', '/etc/resolv.conf',
         os.path.join(img_path, 'etc'), echo=False)
    ret = subprocess.Popen(cmd, env=env).wait()
    if ret != 0:
        raise SystemExit(ret)


def write_in_chroot(path, data):
    path = path.lstrip('/')
    p = subprocess.Popen([
        'sudo', 'tee', os.path.join(img_path, path)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
    if not isinstance(data, bytes):
        data = data.encode('utf-8')
    p.communicate(data)
    if p.wait() != 0:
        raise SystemExit(p.returncode)


@contextmanager
def mounts_needed_for_install():
    mounts = []
    for dev in ('random', 'urandom'):
        mounts.append(os.path.join(img_path, f'dev/{dev}'))
        call('sudo', 'touch', mounts[-1])
        call('sudo', 'mount', '--bind', f'/dev/{dev}', mounts[-1])
    try:
        yield
    finally:
        for x in mounts:
            call('sudo', 'umount', '-l', x)


def install_modern_python(image_name):
    return [
        'add-apt-repository ppa:deadsnakes/ppa -y',
        'apt-get update',
        'apt-get install -y python3.9 python3.9-venv',
        ['sh', '-c', 'ln -sf `which python3.9` `which python3`'],
        'python3 -m ensurepip --upgrade --default-pip',
    ]


def install_modern_cmake(image_name):
    kitware = '/usr/share/keyrings/kitware-archive-keyring.gpg'
    return [
        ['sh', '-c',
         'curl https://apt.kitware.com/keys/kitware-archive-latest.asc |'
         f' gpg --dearmor - > {kitware}'],
        ['sh', '-c', f"echo 'deb [signed-by={kitware}]'"
            f' https://apt.kitware.com/ubuntu/ {image_name} main'
            ' > /etc/apt/sources.list.d/kitware.list'],
        'apt-get update',
        f'rm {kitware}',
        'apt-get install -y kitware-archive-keyring',
        'apt-get install -y cmake',
    ]


def _build_container(url=DEFAULT_BASE_IMAGE):
    user = pwd.getpwuid(os.geteuid()).pw_name
    archive = cached_download(url.format('amd64' if arch == '64' else 'i386'))
    image_name = url.split('/')[-1].split('-')[1]
    if os.path.exists(img_path):
        call('sudo', 'rm', '-rf', img_path, echo=False)
    if os.path.exists(img_store_path):
        os.remove(img_store_path)
    os.makedirs(img_path)
    call('truncate', '-s', '2G', img_store_path)
    call('mkfs.ext4', img_store_path)
    mount_image()
    call('sudo tar -C "{}" -xpf "{}"'.format(img_path, archive), echo=False)
    if os.getegid() != 100:
        chroot('groupadd -f -g {} {}'.format(os.getegid(), 'crusers'))
    chroot(
        'useradd --home-dir=/home/{user} --create-home'
        ' --uid={uid} --gid={gid} {user}'.format(
            user=user, uid=os.geteuid(), gid=os.getegid())
    )
    # Prevent services from starting
    write_in_chroot('/usr/sbin/policy-rc.d', '#!/bin/sh\nexit 101')
    chroot('chmod +x /usr/sbin/policy-rc.d')
    # prevent upstart scripts from running during install/update
    chroot('dpkg-divert --local --rename --add /sbin/initctl')
    chroot('cp -a /usr/sbin/policy-rc.d /sbin/initctl')
    chroot('''sed -i 's/^exit.*/exit 0/' /sbin/initctl''')
    # remove apt-cache translations for fast "apt-get update"
    write_in_chroot(
        '/etc/apt/apt.conf.d/chroot-no-languages',
        'Acquire::Languages "none";'
    )
    deps = conf['deps']
    if isinstance(deps, (list, tuple)):
        deps = ' '.join(deps)
    deps_cmd = 'apt-get install -y ' + deps

    extra_cmds = []
    if image_name in ('xenial', 'bionic'):
        extra_cmds += install_modern_python(image_name)
        extra_cmds += install_modern_cmake(image_name)
    else:
        extra_cmds.append('apt-get install -y python-is-python3 python3-pip')
        extra_cmds.append('apt-get install -y cmake')

    tzdata_cmds = [
        f'''sh -c "echo '{x}' | debconf-set-selections"''' for x in (
            'tzdata tzdata/Areas select Asia',
            'tzdata tzdata/Zones/Asia select Kolkata'
        )] + ['debconf-show tzdata']

    with mounts_needed_for_install():
        for cmd in tzdata_cmds + [
            'apt-get update',
            # bloody only way to get tzdata to install non-interactively is to
            # pipe the expected responses to it
            """sh -c 'echo "6\\n44" | apt-get install -y tzdata'""",
            # Basic build environment
            'apt-get install -y build-essential software-properties-common'
            ' nasm chrpath zsh git uuid-dev libmount-dev apt-transport-https'
            ' dh-autoreconf gperf',
        ] + extra_cmds + [
            'python3 -m pip install ninja',
            'python3 -m pip install meson',
            deps_cmd,
            # Cleanup
            'apt-get clean',
            'chsh -s /bin/zsh ' + user,
        ]:
            if cmd:
                if callable(cmd):
                    cmd()
                else:
                    chroot(cmd, for_install=True)


def build_container():
    url = conf['image']
    try:
        _build_container(url=url)
    except Exception:
        failed_img_path = img_store_path + '.failed'
        if os.path.exists(failed_img_path):
            os.remove(failed_img_path)
        os.rename(img_store_path, failed_img_path)
        raise


def check_for_image(tag):
    return os.path.exists(img_store_path)


def get_mounts():
    ans = {}
    lines = open('/proc/self/mountinfo', 'rb').read().decode(
            'utf-8').splitlines()
    for line in lines:
        parts = line.split()
        src, dest = parts[3:5]
        ans[os.path.abspath(os.path.realpath(dest))] = src
    return ans


def mount_all(tdir):
    scall = partial(call, echo=False)
    current_mounts = get_mounts()
    base = os.path.dirname(os.path.abspath(__file__))

    def mount(src, dest, readonly=False):
        dest = os.path.join(img_path, dest.lstrip('/'))
        if dest not in current_mounts:
            scall('sudo', 'mkdir', '-p', dest)
            scall('sudo', 'mount', '--bind', src, dest)
            if readonly:
                scall('sudo', 'mount', '-o', 'remount,ro,bind', dest)

    mount(tdir, '/tmp')
    mount(sw_dir, '/sw')
    mount(os.getcwd(), '/src', readonly=True)
    mount(sources_dir, '/sources')
    mount(os.path.dirname(base), '/bypy', readonly=True)
    mount('/dev', '/dev')
    scall('sudo', 'mount', '-t', 'proc', 'proc',
          os.path.join(img_path, 'proc'))
    scall('sudo', 'mount', '-t', 'sysfs', 'sys', os.path.join(img_path, 'sys'))
    scall('sudo', 'chmod', 'a+w', os.path.join(img_path, 'dev/shm'))
    scall('sudo', 'mount', '--bind', '/dev/shm',
          os.path.join(img_path, 'dev/shm'))


def umount_all():
    found = True
    while found:
        found = False
        for mp in sorted(get_mounts(), key=len, reverse=True):
            if mp.startswith(img_path) and '/chroot/src/' not in mp:
                call('sudo', 'umount', '-l', mp, echo=False)
                found = True
                break
    del mount_image.mounted


def run(args):
    # dont use /tmp since it could be RAM mounted and therefore
    # too small
    with tempfile.TemporaryDirectory(prefix='tmp-', dir='bypy/b') as tdir:
        zshrc = os.path.realpath(os.path.expanduser('~/.zshrc'))
        dest = os.path.join(
            img_path, 'home', pwd.getpwuid(os.geteuid()).pw_name, '.zshrc')
        if os.path.exists(zshrc):
            shutil.copy2(zshrc, dest)
        else:
            open(dest, 'wb').close()
        shi = os.path.expanduser('~/work/kitty/shell-integration/kitty.zsh')
        if os.path.exists(shi):
            shutil.copy2(shi, os.path.dirname(dest))
        try:
            mount_all(tdir)
            cmd = ['python3', '/bypy', 'main'] + args
            os.environ.pop('LANG', None)
            for k in tuple(os.environ):
                if k.startswith('LC') or k.startswith('XAUTH'):
                    del os.environ[k]
            chroot(cmd, as_root=False)
        finally:
            umount_all()


def singleinstance():
    name = f'bypy-{arch}-singleinstance-{os.getcwd()}'
    return single_instance(name)


def main(args=tuple(sys.argv)):
    global arch
    args = list(args)
    if len(args) > 1 and args[1] in ('64', '32'):
        arch = args[1]
        del args[1]
    if not singleinstance():
        raise SystemExit('Another instance of the linux container is running')
    initialize_env()
    try:
        if len(args) > 1:
            if args[1] == 'shutdown':
                raise SystemExit(0)
            if args[1] == 'container':
                build_container()
                return
        if not check_for_image(arch):
            build_container()
        else:
            mount_image()
        run(args)
    finally:
        unmount_image()


if __name__ == '__main__':
    main()
