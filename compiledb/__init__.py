#
#   compiledb: Tool for generating LLVM Compilation Database
#   files for make-based build systems.
#
#   Copyright (c) 2017 Nick Diego Yamane <nick.diego@gmail.com>
#
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation; either version 2 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# ex: ts=2 sw=4 et filetype=python

import json
import logging
import os
import sys
from types import ModuleType

from compiledb.parser import Error, parse_build_log

orjson: ModuleType | None
try:
    import orjson as _orjson
except ImportError:  # pragma: no cover - exercised when speedups extra is absent.
    orjson = None
else:
    orjson = _orjson

logger = logging.getLogger(__name__)


def __is_stdout(pfile):
    try:
        return pfile.name == sys.stdout.name or isinstance(pfile.name, int)
    except AttributeError:
        return pfile == sys.stdout


def basename(stream):
    if __is_stdout(stream):
        return "<stdout>"
    else:
        return os.path.basename(stream.name)


def generate_json_compdb(instream=None, proj_dir=os.getcwd(), exclude_files=None, add_predefined_macros=False,
                         use_full_path=False, command_style=False):
    if exclude_files is None:
        exclude_files = []
    if not os.path.isdir(proj_dir):
        raise Error(f"Project dir '{proj_dir}' does not exists!")

    logger.info(f"## Processing build commands from {basename(instream)}")
    result = parse_build_log(instream, proj_dir, exclude_files, add_predefined_macros=add_predefined_macros,
                             use_full_path=use_full_path, command_style=command_style)
    return result


def write_json_compdb(compdb, outstream, force=False, pretty_output=True) -> None:
    logger.info(f"## Writing compilation database with {len(compdb)} entries to {basename(outstream)}")

    # We could truncate after reading, but here is easier to understand
    if not __is_stdout(outstream):
        outstream.seek(0)
        outstream.truncate()
    if orjson is not None:
        option = orjson.OPT_APPEND_NEWLINE
        if pretty_output:
            option |= orjson.OPT_INDENT_2
        outstream.write(orjson.dumps(compdb, option=option).decode("utf-8"))
    else:
        json.dump(compdb, outstream, indent=pretty_output)
        outstream.write(os.linesep)


def load_json_compdb(outstream):
    try:
        if __is_stdout(outstream):
            return []

        # Read from beggining of file
        outstream.seek(0)
        compdb = orjson.loads(outstream.read()) if orjson is not None else json.load(outstream)
        logger.info(f"## Loaded compilation database with {len(compdb)} entries from {basename(outstream)}")
        return compdb
    except Exception as e:
        logger.debug(f"## Failed to read previous {basename(outstream)}: {e}")
        return []


def merge_compdb(compdb, new_compdb, check_files=True):
    def gen_key(entry):
        if 'directory' in entry:
            return os.path.join(entry['directory'], entry['file'])
        return entry['directory']

    def check_file(path):
        return True if not check_files else os.path.exists(path)

    orig = {gen_key(c): c for c in compdb if 'file' in c}
    new = {gen_key(c): c for c in new_compdb if 'file' in c}
    orig.update(new)
    return [v for k, v in orig.items() if check_file(k)]


def generate(infile, outfile, build_dir, exclude_files, overwrite=False, strict=False,
             add_predefined_macros=False, use_full_path=False, command_style=False) -> bool | None:
    try:
        r = generate_json_compdb(infile, proj_dir=build_dir, exclude_files=exclude_files,
                                 add_predefined_macros=add_predefined_macros, use_full_path=use_full_path,
                                 command_style=command_style)
        compdb = [] if overwrite else load_json_compdb(outfile)
        compdb = merge_compdb(compdb, r.compdb, strict)
        write_json_compdb(compdb, outfile)
        logger.info("## Done.")
        return True
    except Error as e:
        logger.error(e)
        return False
