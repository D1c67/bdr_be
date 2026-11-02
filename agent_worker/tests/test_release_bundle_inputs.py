"""A signed release must contain the full pinned Windows dependency closure."""
import zipfile
from pathlib import Path

import pytest

from build_release_bundle import validate_wheels, build_bundle, package_version, ROOT
from .release_helpers import fake_package


def wheel(root, name, version='1.0', tag='py3-none-any', requires=()):
    path=root/f'{name}-{version}-{tag}.whl'
    metadata=f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\nRequires-Python: >=3.12\n'
    metadata+=''.join(f'Requires-Dist: {value}\n' for value in requires)
    with zipfile.ZipFile(path,'w') as zf:
        zf.writestr(f'{name}-{version}.dist-info/METADATA',metadata)
    return path


def test_missing_or_wrong_pin_is_refused(tmp_path):
    wheel(tmp_path,'httpx','0.28.1')
    with pytest.raises(SystemExit,match='cryptography'):
        validate_wheels(tmp_path,['httpx==0.28.1','cryptography==48.0.0'])
    with pytest.raises(SystemExit,match='httpx'):
        validate_wheels(tmp_path,['httpx==0.28.2'])


def test_windows_marker_and_transitive_dependency_are_checked(tmp_path):
    wheel(tmp_path,'parent',requires=['windows-only>=2; sys_platform == "win32"',
                                    'unix-only; sys_platform != "win32"'])
    with pytest.raises(SystemExit,match='windows-only'):
        validate_wheels(tmp_path,['parent==1.0'])
    wheel(tmp_path,'windows_only','2.1',tag='cp312-cp312-win_amd64')
    validate_wheels(tmp_path,['parent==1.0'])


def test_wrong_platform_and_old_python_abi_refused(tmp_path):
    bad=wheel(tmp_path,'binary',tag='cp312-cp312-macosx_11_0_arm64')
    with pytest.raises(SystemExit,match='incompatible'):
        validate_wheels(tmp_path,['binary==1.0'])
    bad.unlink()
    wheel(tmp_path,'binary',tag='cp311-cp311-win_amd64')
    with pytest.raises(SystemExit,match='incompatible'):
        validate_wheels(tmp_path,['binary==1.0'])


def test_abi3_and_extras_dependencies_accepted(tmp_path):
    wheel(tmp_path,'parent',requires=['child[secure]>=1'])
    wheel(tmp_path,'child',requires=['crypto>=1; extra == "secure"'])
    wheel(tmp_path,'crypto',tag='cp39-abi3-win_amd64')
    validate_wheels(tmp_path,['parent==1.0'])


def test_unknown_wheel_and_malformed_archive_refused(tmp_path):
    wheel(tmp_path,'parent')
    extra=wheel(tmp_path,'unexpected')
    with pytest.raises(SystemExit,match='Unexpected wheels'):
        validate_wheels(tmp_path,['parent==1.0'])
    extra.unlink()
    (tmp_path/'broken-1.0-py3-none-any.whl').write_bytes(b'not a wheel')
    with pytest.raises(SystemExit,match='metadata'):
        validate_wheels(tmp_path,['parent==1.0'])


def test_build_output_is_deterministic_for_identical_inputs(tmp_path):
    wheels=tmp_path/'wheels';wheels.mkdir()
    wheel(wheels,'parent')
    package=fake_package(tmp_path,'1.0.1')
    first=build_bundle('1.0.1',tmp_path/'first',wheels,package=package)
    second=build_bundle('1.0.1',tmp_path/'second',wheels,package=package)
    assert first['sha256']==second['sha256']
    assert Path(first['path']).read_bytes()==Path(second['path']).read_bytes()


def test_source_release_version_matches_package_metadata():
    import tomllib
    declared=tomllib.loads((ROOT/'pyproject.toml').read_text())['project']['version']
    assert declared==package_version()=='1.0.6'
