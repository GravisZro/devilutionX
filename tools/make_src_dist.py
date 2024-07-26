#!/usr/bin/env python

"""
Makes a tarball suitable for distros.

It contains the following:

1. The repo source code.

2. An additional `dist` directory with:

   1. `FetchContent` dependencies that currently must be vendored.
       These are stripped from especially heavy bloat.

   2. `devilutionx.mpq`.
      While this file can be generated by the build system, it requires
      the `smpq` host dependency which may be missing in some distributions.

   3. `CMakeLists.txt` - a file with the cmake flags containing the version,
      the path to `devilutionx.mpq` and the `FetchContent` dependency paths.

      This file is automatically used by the build system if present.

The only stdout output of this script is the path to the generated tarball.
"""

import argparse
import logging
import pathlib
import re
import shutil
import subprocess
import sys

# We only package the dependencies that are:
# 1. Uncommon in package managers (sdl_audiolib and simpleini).
# 2. Require devilutionx forks (all others).
_DEPS = ['asio', 'libmpq', 'libsmackerdec',
         'libzt', 'sdl_audiolib', 'simpleini', 'unordered_dense']
_ALWAYS_VENDORED_DEPS = ['asio', 'libmpq', 'libsmackerdec', 'libzt']

# These dependencies are not vendored by default.
# Run with `--fully_vendored` to include them.
_DEPS_NOT_VENDORED_BY_DEFAULT = ['googletest', 'benchmark', 'sdl2', 'sdl_image',
                                 'libpng', 'libfmt', 'bzip2', 'libsodium']

_ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent
_BUILD_DIR = _ROOT_DIR.joinpath('build-src-dist')
_ARCHIVE_DIR = _BUILD_DIR.joinpath('archive')

_LOGGER = logging.getLogger()
_LOGGER.setLevel(logging.INFO)
_LOGGER.addHandler(logging.StreamHandler(sys.stderr))


class Version():
	def __init__(self, prefix: str, commit_sha: str):
		self.prefix = prefix
		self.commit_sha = commit_sha
		self.str = f'{prefix}-{commit_sha}' if '-' in prefix else prefix

	def __str__(self) -> str:
		return self.str


class Paths():
	def __init__(self, version: Version, fully_vendored: bool):
		self.archive_top_level_dir_name = 'devilutionx-src'
		if fully_vendored:
			self.archive_top_level_dir_name += '-full'
		self.archive_top_level_dir_name += f'-{version}'
		self.archive_top_level_dir = _ARCHIVE_DIR.joinpath(
			self.archive_top_level_dir_name)
		self.dist_dir = self.archive_top_level_dir.joinpath('dist')


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument('--fully_vendored', default=False, action='store_true')
	args = parser.parse_args()

	configure_args = [f'-S{_ROOT_DIR}',
                   f'-B{_BUILD_DIR}', '-DBUILD_ASSETS_MPQ=ON']
	for dep in sorted(set(_DEPS) - set(_ALWAYS_VENDORED_DEPS)):
		configure_args.append(f'-DDEVILUTIONX_SYSTEM_{dep.upper()}=OFF')
	if args.fully_vendored:
		for dep in _DEPS_NOT_VENDORED_BY_DEFAULT:
			configure_args.append(f'-DDEVILUTIONX_SYSTEM_{dep.upper()}=OFF')
		configure_args.append('-DDISCORD_INTEGRATION=ON')
	cmake(*configure_args)
	cmake('--build', _BUILD_DIR, '--target', 'devilutionx_mpq')

	if _ARCHIVE_DIR.exists():
		shutil.rmtree(_ARCHIVE_DIR)

	version = get_version()
	paths = Paths(version, args.fully_vendored)

	_LOGGER.info(f'Copying repo files...')
	for src_bytes in git('ls-files', '-z').rstrip(b'\0').split(b'\0'):
		src = src_bytes.decode()
		dst_path = paths.archive_top_level_dir.joinpath(src)
		dst_path.parent.mkdir(parents=True, exist_ok=True)
		if re.search(r'(^|/)\.gitkeep$', src):
			continue
		shutil.copy2(_ROOT_DIR.joinpath(src), dst_path, follow_symlinks=False)

	_LOGGER.info(f'Copying devilutionx.mpq...')
	paths.dist_dir.mkdir(parents=True)
	shutil.copy(_BUILD_DIR.joinpath('devilutionx.mpq'), paths.dist_dir)

	for dep in _DEPS + (_DEPS_NOT_VENDORED_BY_DEFAULT if args.fully_vendored else []):
		_LOGGER.info(f'Copying {dep}...')
		shutil.copytree(
			src=_BUILD_DIR.joinpath('_deps', f'{dep}-src'),
			dst=paths.dist_dir.joinpath(f'{dep}-src'),
			ignore=ignore_dep_src)

	write_dist_cmakelists(paths, version, args.fully_vendored)
	print(make_archive(paths, args.fully_vendored))


def cmake(*cmd_args):
	_LOGGER.info(f'+ cmake {subprocess.list2cmdline(cmd_args)}')
	subprocess.run(['cmake', *cmd_args], cwd=_ROOT_DIR, stdout=subprocess.DEVNULL)


def git(*cmd_args):
	_LOGGER.debug(f'+ git {subprocess.list2cmdline(cmd_args)}')
	return subprocess.run(['git', *cmd_args], cwd=_ROOT_DIR, capture_output=True).stdout


# Ignore files in dependencies that we don't need.
# Examples of some heavy ones:
# 48M libzt-src/ext
# 9.8M asio-src/asio/src/doc
_IGNORE_DEP_DIR_RE = re.compile(
	r'(/|^)\.|(/|^)(tests?|other|vcx?proj|examples?|doxygen|docs?|asio-src/asio/src)(/|$)')
_IGNORE_DEP_FILE_RE = re.compile(
	r'(^\.|Makefile|vcx?proj|example|doxygen|docs?|\.(doxy|cmd|png|html|ico|icns)$)')


def ignore_dep_src(src, names):
	if 'sdl_audiolib' in src:
		# SDL_audiolib currently fails to compile if any of the files are missing.
		# TODO: Fix this in SDL_audiolib by making this optional:
		# https://github.com/realnc/SDL_audiolib/blob/5a700ba556d3a5b5c531c2fa1f45fc0c3214a16b/CMakeLists.txt#L399-L401
		return [name for name in names if src.endswith('/sdl_audiolib-src/3rdparty/fmt')]

	if _IGNORE_DEP_DIR_RE.search(src):
		_LOGGER.debug(f'Excluded directory {src}')
		return names

	def ignore_name(name):
		if _IGNORE_DEP_FILE_RE.search(name) or _IGNORE_DEP_DIR_RE.search(name):
			_LOGGER.debug(f'Excluded file {src}/{name}')
			return True
		return False

	return filter(ignore_name, names)


def get_version() -> Version:
	version_prefix = None
	with open('VERSION', 'r') as f:
		version_prefix = f.read().rstrip()
	git_commit_sha = git('rev-parse', '--short', 'HEAD').rstrip().decode()
	return Version(version_prefix, git_commit_sha)


def write_dist_cmakelists(paths: Paths, version: Version, fully_vendored: bool):
	with open(paths.dist_dir.joinpath('CMakeLists.txt'), 'wb') as f:
		f.write(b'# Generated by tools/make_src_dist.py\n')
		if version.commit_sha:
			f.write(b'set(GIT_COMMIT_HASH "%s" PARENT_SCOPE)\n' % version.commit_sha.encode('utf-8'))

		f.write(b'''
# Pre-generated `devilutionx.mpq` is provided so that distributions do not have to depend on smpq.
set(DEVILUTIONX_MPQ "${CMAKE_CURRENT_SOURCE_DIR}/devilutionx.mpq" PARENT_SCOPE)

# This would ensure that CMake does not attempt to connect to network.
# We do not set this to allow for builds for Windows and Android, which do fetch some
# dependencies even with this source distribution.
# set(FETCHCONTENT_FULLY_DISCONNECTED ON PARENT_SCOPE)

# Set the path to each dependency that must be vendored:
''')
		for dep in _DEPS:
			f.write(b'set(FETCHCONTENT_SOURCE_DIR_%s "${CMAKE_CURRENT_SOURCE_DIR}/%s-src" CACHE STRING "")\n' % (
				dep.upper().encode(), dep.encode()))
		if fully_vendored:
			f.write(b'\n# These dependencies are not usually vendored but this distribution includes them\n')
			f.write(b'set(FETCHCONTENT_SOURCE_DIR_DISCORDSRC "${CMAKE_CURRENT_SOURCE_DIR}/discordsrc-src" CACHE STRING "")\n')
			for dep in _DEPS_NOT_VENDORED_BY_DEFAULT:
				f.write(b'set(FETCHCONTENT_SOURCE_DIR_%s "${CMAKE_CURRENT_SOURCE_DIR}/%s-src" CACHE STRING "")\n' % (
					dep.upper().encode(), dep.encode()))
				f.write(b'''if(NOT DEFINED DEVILUTIONX_SYSTEM_%s)
  set(DEVILUTIONX_SYSTEM_%s OFF CACHE BOOL "")
endif()
''' % (dep.upper().encode(), dep.upper().encode()))


def make_archive(paths: Paths, fully_vendored: bool):
	_LOGGER.info(f'Compressing {_ARCHIVE_DIR}')
	return shutil.make_archive(
            format='xztar',
            logger=_LOGGER,
            base_name=_BUILD_DIR.joinpath(paths.archive_top_level_dir_name),
            root_dir=_ARCHIVE_DIR,
            base_dir=paths.archive_top_level_dir_name)


main()
