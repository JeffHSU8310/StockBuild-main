# -*- coding: utf-8 -*-
"""以官方 CPython 相容工具鏈 configure/build/test StockBuild native module。"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


NATIVE_DIR = Path(__file__).resolve().parent
DEFAULT_BUILD_DIR = NATIVE_DIR / 'build'


def _version_key(value):
    parts = []
    for item in str(value).split('.'):
        try:
            parts.append(int(item))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _vsdevcmd(require_asan=False):
    root = Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'))
    vswhere = root / 'Microsoft Visual Studio' / 'Installer' / 'vswhere.exe'
    if vswhere.exists():
        command = [str(vswhere), '-products', '*', '-format', 'json', '-utf8']
        if require_asan:
            command.extend(['-requires', 'Microsoft.VisualStudio.Component.VC.ASAN'])
        try:
            payload = subprocess.run(
                command, check=True, capture_output=True).stdout.decode('utf-8-sig')
            installations = json.loads(payload)
        except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
            installations = []
        candidates = []
        for item in installations:
            devcmd = Path(item.get('installationPath', '')) / 'Common7' / 'Tools' / 'VsDevCmd.bat'
            if item.get('isLaunchable') and devcmd.exists():
                candidates.append((_version_key(item.get('installationVersion')), devcmd))
        if candidates:
            return max(candidates, key=lambda value: value[0])[1]

    candidates = sorted(
        root.glob(r'Microsoft Visual Studio/*/BuildTools/Common7/Tools/VsDevCmd.bat'),
        reverse=True)
    if require_asan:
        candidates = [path for path in candidates if '2022' in path.parts]
    return candidates[0] if candidates else None


def _run(command, cwd=None, shell=False):
    subprocess.run(command, cwd=cwd, check=True, shell=shell)


def _asan_runtime(devcmd=None):
    roots = []
    if devcmd is not None:
        roots.append(devcmd.parents[2] / 'VC' / 'Tools' / 'MSVC')
    tools_dir = os.environ.get('VCToolsInstallDir')
    if tools_dir:
        roots.append(Path(tools_dir).parent)
    for root in roots:
        candidates = sorted(
            root.glob('*/bin/Hostx64/x64/clang_rt.asan_dynamic-x86_64.dll'),
            reverse=True)
        if candidates:
            return candidates[0]
    return None


def _cmake_commands(build_dir, config='Release', sanitizers=False):
    configure = [
        'cmake', '--fresh', '-S', str(NATIVE_DIR), '-B', str(build_dir),
        '-G', 'Ninja', f'-DPython_EXECUTABLE={sys.executable}',
        '-DBUILD_TESTING=ON', '-DSTOCKBUILD_BUILD_PYTHON=ON',
        f'-DCMAKE_BUILD_TYPE={config}',
        f'-DSTOCKBUILD_ENABLE_SANITIZERS={"ON" if sanitizers else "OFF"}',
    ]
    build = ['cmake', '--build', str(build_dir), '--config', config]
    test = ['ctest', '--test-dir', str(build_dir), '-C', config, '--output-on-failure']
    return configure, build, test


def build(build_dir=DEFAULT_BUILD_DIR, config='Release', sanitizers=False):
    build_dir = Path(build_dir).resolve()
    build_dir.mkdir(parents=True, exist_ok=True)
    configure, compile_cmd, test_cmd = _cmake_commands(build_dir, config, sanitizers)

    devcmd = None
    if os.name == 'nt' and shutil.which('cl') is None:
        devcmd = _vsdevcmd(require_asan=sanitizers)
        if devcmd is None:
            detail = '（需含 C++ AddressSanitizer 元件）' if sanitizers else ''
            raise RuntimeError(
                f'找不到 MSVC Build Tools{detail}；官方 Windows Python 不使用 MinGW 跨 ABI 建置')
        joined = ' && '.join(subprocess.list2cmdline(part)
                             for part in (configure, compile_cmd, test_cmd))
        command = f'set "VSLANG=1033" && "{devcmd}" -arch=amd64 -host_arch=amd64 && {joined}'
        # subprocess 的 argv-list 會讓 cmd 重寫第一組引號；shell=True 交給
        # COMSPEC 原樣解析 VsDevCmd 與後面的 && command chain。
        _run(command, cwd=NATIVE_DIR.parent, shell=True)
        compiler = f'MSVC x64 ({devcmd.parents[3].name} VsDevCmd)'
    else:
        _run(configure, cwd=NATIVE_DIR.parent)
        _run(compile_cmd, cwd=NATIVE_DIR.parent)
        _run(test_cmd, cwd=NATIVE_DIR.parent)
        compiler = shutil.which('cl') or shutil.which('c++') or 'unknown'

    suffix = '.pyd' if os.name == 'nt' else '.so'
    modules = sorted(build_dir.rglob(f'_stockbuild_native*{suffix}'))
    if not modules:
        raise RuntimeError(f'建置完成但找不到 _stockbuild_native{suffix}')
    runtime = None
    if os.name == 'nt' and sanitizers:
        runtime = _asan_runtime(devcmd)
        if runtime is None:
            raise RuntimeError('ASan build 完成但找不到 clang_rt.asan_dynamic-x86_64.dll')
        shutil.copy2(runtime, modules[-1].parent / runtime.name)
    return {'module': str(modules[-1]), 'build_dir': str(build_dir),
            'compiler': compiler, 'config': config, 'sanitizers': bool(sanitizers),
            'asan_runtime': str(runtime) if runtime else None}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Build StockBuild ADR-145 native foundation')
    parser.add_argument('--build-dir', default=str(DEFAULT_BUILD_DIR))
    parser.add_argument('--config', default='Release', choices=('Debug', 'Release'))
    parser.add_argument('--sanitizers', action='store_true')
    args = parser.parse_args(argv)
    print(json.dumps(build(args.build_dir, args.config, args.sanitizers),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
