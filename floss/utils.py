# Copyright (C) 2017 Mandiant, Inc. All Rights Reserved.
import re
import time
import inspect
import contextlib
from typing import Set, Iterable
from collections import OrderedDict

import tqdm
import tabulate
import viv_utils
import envi.archs
from envi import Emulator

import floss.strings
import floss.logging_

from .const import MEGABYTE, MAX_STRING_LENGTH
from .results import StaticString
from .identify import is_thunk_function

STACK_MEM_NAME = "[stack]"

logger = floss.logging_.getLogger(__name__)


def make_emulator(vw) -> Emulator:
    """
    create an emulator using consistent settings.
    """
    emu = vw.getEmulator(logwrite=True, taintbyte=b"\xFE")
    remove_stack_memory(emu)
    emu.initStackMemory(stacksize=int(0.5 * MEGABYTE))
    emu.setStackCounter(emu.getStackCounter() - int(0.25 * MEGABYTE))
    # do not short circuit rep prefix
    try:
        emu.setEmuOpt("i386:repmax", 256)  # 0 == no limit on rep prefix
    except Exception:
        # TODO remove once vivisect#465 is included in release (v1.0.6)
        emu.setEmuOpt("i386:reponce", False)
    return emu


def remove_stack_memory(emu: Emulator):
    # TODO this is a hack while vivisect's initStackMemory() has a bug
    memory_snap = emu.getMemorySnap()
    for i in range((len(memory_snap) - 1), -1, -1):
        (_, _, info, _) = memory_snap[i]
        if info[3] == STACK_MEM_NAME:
            del memory_snap[i]
            emu.setMemorySnap(memory_snap)
            emu.stack_map_base = None
            return
    raise ValueError("`STACK_MEM_NAME` not in memory map")


def getPointerSize(vw):
    if isinstance(vw.arch, envi.archs.amd64.Amd64Module):
        return 8
    elif isinstance(vw.arch, envi.archs.i386.i386Module):
        return 4
    else:
        raise NotImplementedError("unexpected architecture: %s" % (vw.arch.__class__.__name__))


def get_vivisect_meta_info(vw, selected_functions, decoding_function_features):
    info = OrderedDict()
    entry_points = vw.getEntryPoints()
    basename = None
    if entry_points:
        basename = vw.getFileByVa(entry_points[0])

    # "blob" is the filename for shellcode
    if basename and basename != "blob":
        version = vw.getFileMeta(basename, "Version")
        md5sum = vw.getFileMeta(basename, "md5sum")
        baseva = hex(vw.getFileMeta(basename, "imagebase"))
    else:
        version = "N/A"
        md5sum = "N/A"
        baseva = "N/A"

    info["version"] = version
    info["MD5 Sum"] = md5sum
    info["format"] = vw.getMeta("Format")
    info["architecture"] = vw.getMeta("Architecture")
    info["platform"] = vw.getMeta("Platform")
    disc = vw.getDiscoveredInfo()[0]
    undisc = vw.getDiscoveredInfo()[1]
    info["percentage of discovered executable surface area"] = "%.1f%% (%s / %s)" % (
        disc * 100.0 / (disc + undisc),
        disc,
        disc + undisc,
    )
    info["base VA"] = baseva
    info["entry point(s)"] = ", ".join(map(hex, entry_points))
    info["number of imports"] = len(vw.getImports())
    info["number of exports"] = len(vw.getExports())
    info["number of functions"] = len(vw.getFunctions())

    if selected_functions:
        meta = []
        for fva in selected_functions:
            if is_thunk_function(vw, fva) or viv_utils.flirt.is_library_function(vw, fva):
                continue

            xrefs_to = len(vw.getXrefsTo(fva))
            num_args = len(vw.getFunctionArgs(fva))
            function_meta = vw.getFunctionMetaDict(fva)
            instr_count = function_meta.get("InstructionCount")
            block_count = function_meta.get("BlockCount")
            size = function_meta.get("Size")
            score = round(decoding_function_features.get(fva, {}).get("score", 0), 3)
            meta.append((hex(fva), score, xrefs_to, num_args, size, block_count, instr_count))
        info["selected functions' info"] = "\n%s" % tabulate.tabulate(
            meta, headers=["fva", "score", "#xrefs", "#args", "size", "#blocks", "#instructions"]
        )

    return info


def hex(i):
    return "0x%X" % (i)


FP_STRINGS = (
    "R6016",
    "Program: ",
    "Runtime Error!",
    "<program name unknown>",
    "- floating point not loaded",
    "Program: <program name unknown>",
    "- not enough space for thread data",
    # all printable ASCII chars
    " !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~",
)


def extract_strings(buffer: bytes, min_length: int, exclude: Set[str] = None) -> Iterable[StaticString]:
    # TODO do this even earlier?
    # TODO fail on offsets with strip?!
    buffer_stripped = strip_bytes(buffer)
    logger.trace("strip bytes: %s -> %s", buffer, buffer_stripped)

    if len(buffer) < min_length:
        return

    for s in floss.strings.extract_ascii_unicode_strings(buffer_stripped):
        if len(s.string) > MAX_STRING_LENGTH:
            continue

        if s.string in FP_STRINGS:
            continue

        decoded_string = strip_string(s.string)

        if len(decoded_string) < min_length:
            continue

        logger.trace("strip: %s -> %s", s.string, decoded_string)

        if exclude and decoded_string in exclude:
            continue

        yield StaticString(decoded_string, s.offset, s.encoding)


FP_FILTER_REP_BYTES = re.compile(rb"(.)\1{3,}")  # any string containing the same char 4 or more consecutive times
FP_STACK_FILTER_1 = rb"...VA.*\x00\x00\x00\x00"


def strip_bytes(b, enabled=False):
    # TODO check with offsets
    if not enabled:
        return b
    b = re.sub(FP_FILTER_REP_BYTES, b"\x00\x00", b)
    b = re.sub(FP_STACK_FILTER_1, b"\x00\x00", b)
    return b


# remove string prefixes: pVA, VA, 0VA, etc.
FP_FILTER_PREFIXES = re.compile(r"^.?((p|P|0|W4|Q)?VA)(0|7Q|,)?|(0|P)?\\A|\[A|P\]A|@AA|fqd`|(fe){5,}|(p|P)_A")
# remove string suffixes: 0VA, AVA, >VA, etc.
FP_FILTER_SUFFIXES = re.compile(r"([0-9A-G>]VA|@AA|iiVV|j=p@|ids@|iDC@|i4C@|i%1@)$")


def strip_string(s):
    """
    Return string stripped from false positive (FP) pre- or suffixes.
    :param s: input string
    :return: string stripped from FP pre- or suffixes
    """
    for reg in (FP_FILTER_PREFIXES, FP_FILTER_SUFFIXES):
        s = re.sub(reg, "", s)
    return s


@contextlib.contextmanager
def redirecting_print_to_tqdm():
    """
    tqdm (progress bar) expects to have fairly tight control over console output.
    so calls to `print()` will break the progress bar and make things look bad.
    so, this context manager temporarily replaces the `print` implementation
    with one that is compatible with tqdm.
    via: https://stackoverflow.com/a/42424890/87207
    """
    old_print = print

    def new_print(*args, **kwargs):

        # If tqdm.tqdm.write raises error, use builtin print
        try:
            tqdm.tqdm.write(*args, **kwargs)
        except:
            old_print(*args, **kwargs)

    try:
        # Globaly replace print with new_print
        inspect.builtins.print = new_print
        yield
    finally:
        inspect.builtins.print = old_print


@contextlib.contextmanager
def timing(msg):
    t0 = time.time()
    yield
    t1 = time.time()
    logger.trace("perf: %s: %0.2fs", msg, t1 - t0)


def get_runtime_diff(time0):
    return round(time.time() - time0, 2)


def is_all_zeros(buffer: bytes):
    return all([b == 0 for b in buffer])


def get_progress_bar(functions, disable_progress, desc="", unit=""):
    pbar = tqdm.tqdm
    if disable_progress:
        # do not use tqdm to avoid unnecessary side effects when caller intends
        # to disable progress completely
        pbar = lambda s, *args, **kwargs: s
    return pbar(functions, desc=desc, unit=unit)
