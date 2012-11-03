# -*- coding: utf-8 -*-
"""

    @author: Fabio Erculiani <lxnay@sabayon.org>
    @contact: lxnay@sabayon.org
    @copyright: Fabio Erculiani
    @license: GPL-2

    B{Entropy Command Line Client}.

"""
import sys
import argparse

from entropy.i18n import _, ngettext
from entropy.output import darkred, blue, brown, darkgreen, purple

from solo.commands.descriptor import SoloCommandDescriptor
from solo.commands.command import SoloCommand
from solo.utils import print_table, print_package_info

import entropy.dep

class SoloMatch(SoloCommand):
    """
    Main Solo Match command.
    """

    NAME = "match"
    ALIASES = ["m"]
    ALLOW_UNPRIVILEGED = True

    INTRODUCTION = """\
Match package names.
"""
    SEE_ALSO = ""

    def __init__(self, args):
        SoloCommand.__init__(self, args)
        self._quiet = False
        self._verbose = False
        self._installed = False
        self._available = False
        self._multimatch = False
        self._multirepo = False
        self._showrepo = False
        self._showdesc = False
        self._showslot = False
        self._packages = []

    def man(self):
        """
        Overridden from SoloCommand.
        """
        return self._man()

    def bashcomp(self, last_arg):
        """
        Overridden from SoloCommand.
        """
        args = [
            "--quiet", "--verbose",
            "--installed", "--available",
            "--multimatch", "--multirepo",
            "--showrepo", "--showslot"]
        args.sort()
        return self._bashcomp(sys.stdout, last_arg, args)

    def _get_parser(self):
        """
        Overridden from SoloCommand.
        """
        descriptor = SoloCommandDescriptor.obtain_descriptor(
            SoloMatch.NAME)
        parser = argparse.ArgumentParser(
            description=descriptor.get_description(),
            formatter_class=argparse.RawDescriptionHelpFormatter,
            prog="%s %s" % (sys.argv[0], SoloMatch.NAME))

        parser.add_argument("string", nargs='+',
                            metavar="<string>", help=_("match keyword"))

        parser.add_argument("--quiet", "-q", action="store_true",
                            default=self._quiet,
                            help=_('quiet output, for scripting purposes'))

        parser.add_argument("--verbose", "-v", action="store_true",
                            default=self._verbose,
                            help=_('verbose output'))

        group = parser.add_mutually_exclusive_group()
        group.add_argument("--installed", action="store_true",
                           default=self._installed,
                           help=_('match among installed packages only'))

        group.add_argument("--available", action="store_true",
                           default=self._available,
                           help=_('match among available packages only'))

        parser.add_argument("--multimatch", action="store_true",
                            default=self._multimatch,
                            help=_('return all the matches, '
                                   'not just the best'))
        parser.add_argument(
            "--multirepo", action="store_true",
            default=self._multirepo,
            help=_('return matches found in every repository'))

        # only if --quiet
        parser.add_argument(
            "--showrepo", action="store_true",
            default=self._showrepo,
            help=_('print repository information (w/--quiet)'))
        parser.add_argument(
            "--showdesc", action="store_true",
            default=self._showdesc,
            help=_('print description too (w/--quiet)'))
        parser.add_argument(
            "--showslot", action="store_true",
            default=self._showslot,
            help=_('print ":<slot>" next to package names (w/--quiet)'))

        return parser

    def parse(self):
        """
        Parse command.
        """
        parser = self._get_parser()
        try:
            nsargs = parser.parse_args(self._args)
        except IOError as err:
            sys.stderr.write("%s\n" % (err,))
            return parser.print_help, []

        self._quiet = nsargs.quiet
        self._verbose = nsargs.verbose
        self._installed = nsargs.installed
        self._available = nsargs.available
        self._packages = nsargs.string
        self._multimatch = nsargs.multimatch
        self._multirepo = nsargs.multirepo
        self._showrepo = nsargs.showrepo
        self._showdesc = nsargs.showdesc
        self._showslot = nsargs.showslot

        return self._call_unlocked, [self.match]

    def _match_string(self, entropy_client, string):
        """
        Match method, returns search results.
        """
        matches = []
        inst_repo = entropy_client.installed_repository()
        inst_repo_id = inst_repo.repository_id()

        if self._installed:
            inst_pkg_id, inst_rc = inst_repo.atomMatch(
                string, multiMatch = self._multimatch)
            if inst_rc != 0:
                match = (-1, 1)
            else:
                if self._multimatch:
                    match = ([(x, inst_repo_id) for x in inst_pkg_id], 0)
                else:
                    match = (inst_pkg_id, inst_repo_id)
        else:
            match = entropy_client.atom_match(
                string, multi_match = self._multimatch,
                multi_repo = self._multirepo,
                mask_filter = False)

        if match[1] != 1:
            if not self._multimatch:
                if self._multirepo:
                    matches += match[0]
                else:
                    matches += [match]
            else:
                matches += match[0]

        key_sorter = lambda x: \
            entropy_client.open_repository(x[1]).retrieveAtom(x[0])
        return sorted(matches, key=key_sorter)

    def match(self, entropy_client):
        """
        Solo Match command.
        """
        if not self._quiet:
            entropy_client.output(
                "%s..." % (darkgreen(_("Matching")),),
                header=darkred(" @@ "))

        matches_found = 0
        for string in self._packages:
            results = self._match(
                entropy_client, string)
            matches_found += len(results)

        if not self._quiet:
            toc = []
            toc.append(("%s:" % (blue(_("Keywords")),),
                purple(', '.join(self._packages))))
            toc.append(("%s:" % (blue(_("Found")),), "%s %s" % (
                matches_found,
                brown(ngettext("entry", "entries", matches_found)),)))
            print_table(entropy_client, toc)

        if not matches_found:
            return 1
        return 0

    def _match(self, entropy_client, string):
        """
        Solo Search string command.
        """
        results = self._match_string(
            entropy_client, string)

        inst_repo = entropy_client.installed_repository()
        inst_repo_class = inst_repo.__class__
        for pkg_id, pkg_repo in results:
            dbconn = entropy_client.open_repository(pkg_repo)
            from_client = isinstance(dbconn, inst_repo_class)
            print_package_info(
                pkg_id, entropy_client, dbconn,
                show_repo_if_quiet = self._showrepo,
                show_desc_if_quiet = self._showdesc,
                show_slot_if_quiet = self._showslot,
                extended = self._verbose,
                installed_search = from_client,
                quiet = self._quiet)

        return results


SoloCommandDescriptor.register(
    SoloCommandDescriptor(
        SoloMatch,
        SoloMatch.NAME,
        _("match packages in repositories"))
    )
