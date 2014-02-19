# -*- coding: utf-8 -*-
# Copyright 2014 Jan-Philip Gehrcke. See LICENSE file for details.
#

from __future__ import unicode_literals

"""Accept or reject files/items based on time categorization.

Feature / TODO brainstorm:
    - symlink support (elaborate specifics)
"""

EXTENDED_HELP = """

timegaps accepts or rejects file system entries based on modification time
categorization. Its input is a set of paths and a set of classification rules.
In the default mode, the output is to two sets of paths, the rejected and the
accepted ones. Details are described below.


Input:
    ITEMs:
        By default, an ITEM value is interpreted as a path to a file system
        entry. By default, the timestamp corresponding to this item (which is
        used for item filtering) is the modification time as reported by
        stat(). Optionally, this timestamp may also be parsed from the basename
        of the path. When interpreted as paths, all ITEM values must point to
        valid file system entries. In a different mode of operation, ITEM
        values are treated as simple strings w/o path validation, in which case
        a timestamp must be parsable from the string itself.
    RULES:
        These rules define the amount of items to be accepted for certain time
        categories. All other items become rejected. Supported time categories
        and the RULES string formatting specification are given in the
        program's normal help text. The exact method of classification is
        explained below.


Output:
        By default, the program writes the rejected items to stdout, whereas
        single items are separated by newline characters. Alternatively, the
        accepted items can be written out instead of the rejected ones. The
        item separator may be set to the NUL character. Log output and error
        messages are written to stderr.


Actions:
        Certain actions such as removal or renaming (moving) can be performed
        on items based on their classification. By default, no actions are
        performed. If not further specified, activated actions are performed on
        rejected items only.


Classification method:
        Each item provided as input becomes classified as either accepted or
        rejected, based on its corresponding timestamp and according to the
        time filter rules given by the user. For the basic meaning of the
        filter rules, consider this RULES string example:

            hours12,days5,weeks4

        It translates to the following <category>:<maxcount> pairs:

            hours: 12
            days:   5
            weeks:  4

        Based on the reference time, which by default is the program's startup
        time, the program calculates the age of all ITEMs. According to
        the example rules, the program tries to identify and accept one item
        from each of the last 12 hours, one item from each of the last 5 days,
        and one item from each of the last 4 weeks.

        More specifically, according to the <hours> rule above, the program
        accepts the *newest* item in each of 12 sub-categories: the newest item
        being 1 h old, the newest item being 2 h old, ..., and the newest item
        being 12 h old, yielding at most 12 accepted items from the <hours>
        time category: zero or one for each of the sub-categories.

        An hour is a time unit, as are all time categories except for the
        <recent> category (explained further below). An item is considered
        X [timeunits] old if it is older than X [timeunits], but younger than
        X+1 [timeunits]. For instance, if an item being 45 days old should be
        sub-categorized within the 'months' category, it would be considered
        1 month old, because it is older than 30 days (1 month) and younger
        than 60 days (2 months). Internally, all time category units are
        treated as linear in time (see below for time category specification).

        The example rules above can accept at most 12 + 5 + 4 accepted items.
        If there are multiple items fitting into a certain sub-category (e.g.
        <3-days>), then the newest of these is accepted. If there is no item
        fitting into a certain sub-category, then this sub-category simply does
        not yield an item. Considering the example rules above, only 11 items
        are accepted from the <hours> category if the program does not find an
        item for the <5-hours> sub-category, but at least one item for the
        other <hours> sub-categories.

        Younger time categories have higher priority than older ones. This is
        only relevant when, according to the rules, two <category>:<maxcount>
        pairs overlap. Example rules:

            days:  10
            weeks:  1

        An item fitting into one of the <7/8/9/10-days> sub-categories would
        also fit the <1-weeks> sub-category. This is a rules overlap. In this
        case, the <X-days> sub-categories will be populated first, since <days>
        is the younger category than <weeks>. If there is an 11 days old item
        in the input, it will populate the <1-week> sub-category, because it is
        the newest 1-week-old item *not consumed by a younger category*.


        Time categories and their meaning:

            hours:  60 minutes (    3600 seconds)
            days:   24 hours   (   86400 seconds)
            weeks:   7 days    (  604800 seconds)
            months: 30 days    ( 2592000 seconds)
            years: 365 days    (31536000 seconds)

        The special category <recent> keeps track of all items younger than
        1 hour. It it not further sub-categorized. If specified in the rules,
        the <maxcount> newest items from this category re accepted.


Exit status:
    TODO
"""

import os
import sys
import argparse
import logging
import re
import time
from timegaps import TimeFilter, FileSystemEntry, __version__

WINDOWS = sys.platform == "win32"

# Global for options, to be populated by argparse from cmdline arguments.
options = None


def main():
    parse_options()
    if options.verbose == 1:
        log.setLevel(logging.INFO)
    elif options.verbose == 2:
        log.setLevel(logging.DEBUG)

    # Be explicit about output encoding.
    # Also see http://stackoverflow.com/a/4374457/145400
    if sys.stdout.encoding is None:
        err(("Don't know which encoding to use when writing data to stdout. "
            "Please set environment variable PYTHONIOENCODING. "
            "Example: export PYTHONIOENCODING=UTF-8."))

    log.debug("Options namespace:\n%s", options)

    # Validate options (logic not tested automatically by `argparse`).

    # If the user misses to provide either RULES or an ITEM, it is not catched
    # by argparse (0 ITEMs is allowed when --stdin is set). Validate RULES and
    # ITEMs here in the order as consumed by argparse (first RULES, then ITEMS).
    # Doing it the other way round could produce confusing error messages.
    # Parse RULES argument.
    # TODO: Py3
    # sys.stdout.encoding is either derived from LC_CTYPE (set on your typical
    # Unix system) or from environment variable PYTHONIOENCODING, which is good
    # for overriding and making guarantees, e.g. on Windows.
    rules_unicode = options.rules
    if not isinstance(rules_unicode, unicode):
        rules_unicode = options.rules.decode(sys.stdout.encoding)
    log.debug("Decode rules string.")
    try:
        rules = parse_rules_from_cmdline(rules_unicode)
        log.info("Using rules: %s", rules)
    except ValueError as e:
        err("Error while parsing rules: '%s'." % e)
    if not options.stdin:
        if len(options.items) == 0:
            err("At least one ITEM must be provided (if --stdin not set).")
    else:
        if len(options.items) > 0:
            err("No ITEM must be provided when --stdin is set.")

    # Determine reference time and set up `TimeFilter` instance. Do this as
    # early as possible: might raise error.
    if options.reference_time is not None:
        log.debug("Parse reference time from cmdline.")
        raise NotImplemented
    else:
        log.debug("Get reference time: now.")
        reference_time = time.time()
    log.info("Using reference time %s." % reference_time)
    timefilter = TimeFilter(rules, reference_time)

    if options.move is not None:
        if not os.path.isdir(options.move):
            err("--move target not a directory: '%s'" % options.move)

    # Item input section.
    log.info("Start collecting items.")
    items = prepare_input()
    log.info("Start filtering %s items.", len(items))

    # Classification section.
    accepted, rejected = timefilter.filter(items)
    rejected = list(rejected)
    log.info("Number of accepted items: %s", len(accepted))
    log.info("Number of rejected items: %s", len(rejected))
    log.debug("Accepted items:\n%s" % "\n".join("%s" % a for a in accepted))
    log.debug("Rejected items:\n%s" % "\n".join("%s" % r for r in rejected))

    actonitems = rejected if not options.accepted else accepted

    # Item output section.
    # The `text` attribute of items is a unicode object
    for ai in actonitems:
        # sys.stdout.encoding is not always the right thing:
        # http://drj11.wordpress.com/2007/05/14/python-how-is-sysstdoutencoding-chosen/
        sys.stdout.write(("%s\n" % ai.text).encode(sys.stdout.encoding))


def prepare_input():
    """Return a list of objects that can be classified by a `TimeFilter`
    instance.
    """
    # When reading from stdin, take a different path than `options.items`.
    # Regarding stdin decoding: http://stackoverflow.com/a/16549381/145400
    #
    if options.time_from_string is not None:
        # TODO: change mode to pure string parsing, w/o item-wise file system
        # interaction
        raise NotImplemented
        # return list_of_items_from_strings
    # File system mode.
    log.info("Validate paths and extract time information.")
    fses = []
    for path in options.items:
        log.debug("Type of path: %s", type(path))

        # On the one hand, a unicode-aware Python program should only use
        # unicode type strings internally. On the other hand, when it comes
        # to file system interaction, bytestrings are the more portable choice
        # on Unix-like systems.
        # See https://wiki.python.org/moin/Python3UnicodeDecodeError:
        # "A robust program will have to use only the bytes type to make sure
        # that it can open / copy / remove any file or directory."
        #
        # On Python 3, which automatically populates argv with unicode objects,
        # we could therefore re-encode towards bytestrings. See:
        # http://stackoverflow.com/a/7077803/145400
        # http://bugs.python.org/issue8514
        # https://github.com/oscarbenjamin/opster/commit/61f693a2c553944394ba286baed20abc31958f03
        # On the other hand,
        # there is http://www.python.org/dev/peps/pep-0383/ which describes how
        # surrogate encoding is used by Python 3 for auto-correcting issues
        # related to wrongly decoded arguments (the encoding assumption upon
        # decoding might have been wrong).
        # Also, interesting in this respect:
        # http://stackoverflow.com/a/846931/145400

        # Definite choice for Python 2 and Unix:
        # keep paths as byte strings.
        modtime = None
        if options.time_from_basename:
            modtime = time_from_basename(path)
        try:
            fses.append(FileSystemEntry(path, modtime))
        except OSError:
            err("Cannot access '%s'." % path)
    log.debug("Created %s items from file system entries.", len(fses))
    return fses


def time_from_basename(path):
    """Parse `path`, extract time from basename, according to format string
    in `options.time_from_basename`. Treat time string as local time.

    Return non-localized Unix timestamp.
    """
    # When extracting time from path (basename), use path and format string as
    # unicode objects.
    #
    # On Python 3, argv comes as unicode objects (possibly with surrogate
    # chars).
    #
    # On Python 2, both the format string and the path
    # come as byte strings from sys.argv. By default, attempt to decode
    # both using sys.getfilesystemencoding(), as the best possible
    # guess. Or let the user override via --encoding-args



    #
    raise NotImplemented
    # use options.time_from_basename for parsing string.


def parse_rules_from_cmdline(s):
    """Parse strings such as 'hours12,days5,weeks4' into rules dictionary.
    """
    assert isinstance(s, unicode) # TODO: Py3
    tokens = s.split(",")
    # never happens: http://docs.python.org/2/library/stdtypes.html#str.split
    #if not tokens:
    #    raise ValueError("Error extracting rules from string '%s'" % s)
    rules = {}
    for t in tokens:
        log.debug("Analyze token '%s'", t)
        if not t:
            raise ValueError("Token is empty")
        match = re.search(r'([a-z]+)([0-9]+)', t)
        if match:
            catid = match.group(1)
            timecount = match.group(2)
            if catid not in TimeFilter.valid_categories:
                raise ValueError("Time category '%s' invalid" % catid)
            rules[catid] = int(timecount)
            log.debug("Stored rule: %s: %s" % (catid, timecount))
            continue
        raise ValueError("Invalid token <%s>" % t)
    return rules


def err(s):
    """Log error message `s` in logging's error category and exit with code 1.
    """
    log.error(s)
    log.info("Exit with code 1.")
    sys.exit(1)


def parse_options():
    """Set up and parse commandline options using `argparse`.
    """
    class ExtHelpAction(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            print(EXTENDED_HELP)
            sys.exit(0)

    global options
    description = "Accept or reject files/items based on time categorization."
    parser = argparse.ArgumentParser(
        prog="timegaps",
        description=description,
        epilog="Version %s" % __version__,
        add_help=False
        )
    parser.add_argument("-h", "--help", action="help",
        help="Show help message and exit.")
    parser.add_argument("--extended-help", action=ExtHelpAction, nargs=0,
        help="Show extended help and exit.")
    parser.add_argument("--version", action="version",
        version=__version__, help="Show version information and exit.")


    parser.add_argument("rules", action="store",
        metavar="RULES",
        help=("A string defining the filter rules of the form "
            "<category><maxcount>[,<category><maxcount>[, ... ]]. "
            "Example: 'recent5,days12,months5'. "
            "Valid <category> values: %s. Valid <maxcount> values: "
            "positive integers. Default maxcount for unspecified categories: "
            "0." %
            ", ".join(TimeFilter.valid_categories))
        )
    # Require at least one arg if --stdin is not defined. Don't require any
    # arg if --stdin is defined. Overall, allow an arbitrary number, and
    # validate later.
    parser.add_argument("items", metavar="ITEM", action="store", nargs='*',
        help=("Items for filtering. Interpreted as paths to file system "
            "entries by default. Must be omitted in --stdin mode.")
        )

    filehandlegroup = parser.add_mutually_exclusive_group()
    filehandlegroup .add_argument("-d", "--delete", action="store_true",
        help="Attempt to delete rejected paths."
        )
    filehandlegroup.add_argument("-m", "--move", action="store",
        metavar="DIR",
        help="Attempt to move rejected paths to directory DIR.")


    parser.add_argument("-s", "--stdin", action="store_true",
        help=("Read input items from stdin (default separator: newline).")
        )
    parser.add_argument("-0", "--nullsep", action="store_true",
        help=("Input and output item separator is NUL character "
            "instead of newline character.")
        )
    parser.add_argument("-t", "--reference-time", action="store",
        metavar="FMT",
        help=("Parse time from formatstring FMT (cf. documentation of Python's "
            "strptime() at bit.ly/strptime). Use this time as reference time "
            "(default is time of program invocation).")
        )
    parser.add_argument("-S", "--follow-symlinks", action="store_true",
        help=("Retrieve modification time from symlink target, .. "
            "TODO: other implications?")
        )

    timeparsegroup = parser.add_mutually_exclusive_group()
    timeparsegroup.add_argument("--time-from-basename", action="store",
        metavar="FMT",
        help=("Don't extract an item's modification time from inode (which is "
            "the default). Instead, parse time from basename of path according "
            "to formatstring FMT (cf. documentation of Python's strptime() at "
            "bit.ly/strptime).")
        )
    timeparsegroup.add_argument("--time-from-string", action="store",
        metavar="FMT",
        help=("Treat items as strings (don't validate paths) and parse time "
            "from strings using formatstring FMT (cf. bit.ly/strptime).")
        )

    parser.add_argument('-a', '--accepted', action='store_true',
        help=("Output accepted items and perform actions on accepted items "
            "instead of (on) rejected ones.")
        )

    parser.add_argument('-v', '--verbose', action='count', default=0,
        help=("Control verbosity. Can be specified multiple times for "
            "increasing logging level. Levels: error (default), info, debug.")
        )

    options = parser.parse_args()


def time_from_dirname(d):
    # dirs are of type 2013.08.15_20.29.31
    return time.strptime(d, "%Y.%m.%d_%H.%M.%S")


def dirname_from_time(t):
    return time.strftime("%Y.%m.%d_%H.%M.%S", t)


# TODO: Py3 (this hack should not be necessary for 3.3, at least).
if WINDOWS:
    def win32_unicode_argv():
        """Uses shell32.GetCommandLineArgvW to get sys.argv as a list of Unicode
        strings.

        Versions 2.x of Python don't support Unicode in sys.argv on
        Windows, with the underlying Windows API instead replacing multi-byte
        characters with '?'.

        Solution copied from http://stackoverflow.com/a/846931/145400
        """

        from ctypes import POINTER, byref, cdll, c_int, windll
        from ctypes.wintypes import LPCWSTR, LPWSTR

        GetCommandLineW = cdll.kernel32.GetCommandLineW
        GetCommandLineW.argtypes = []
        GetCommandLineW.restype = LPCWSTR

        CommandLineToArgvW = windll.shell32.CommandLineToArgvW
        CommandLineToArgvW.argtypes = [LPCWSTR, POINTER(c_int)]
        CommandLineToArgvW.restype = POINTER(LPWSTR)

        cmd = GetCommandLineW()
        argc = c_int(0)
        argv = CommandLineToArgvW(cmd, byref(argc))
        if argc.value > 0:
            # Remove Python executable and commands if present
            start = argc.value - len(sys.argv)
            return [argv[i] for i in
                    xrange(start, argc.value)]

    # Populate sys.argv with unicode objects.
    sys.argv = win32_unicode_argv()


if __name__ == "__main__":
    log = logging.getLogger()
    log.setLevel(logging.ERROR)
    ch = logging.StreamHandler()
    #fh = RotatingFileHandler(
    #    LOGFILE_PATH,
    #    mode='a',
    #    maxBytes=500*1024,
    #    backupCount=30,
    #    encoding='utf-8')
    formatter = logging.Formatter(
        '%(asctime)s,%(msecs)-6.1f - %(levelname)s: %(message)s',
        datefmt='%H:%M:%S')
    ch.setFormatter(formatter)
    #fh.setFormatter(formatter)
    log.addHandler(ch)
    #log.addHandler(fh)
    main()
