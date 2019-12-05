#!/usr/bin/env python3
#
# flatpak-extra-data-checker: A tool for checking the status of
# the extra data in a Flatpak manifest.
#
# Copyright (C) 2018 Endless Mobile, Inc.
#
# Authors:
#       Joaquim Rocha <jrocha@endlessm.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

import argparse
import contextlib
import logging
import os
import subprocess
import sys

from github import Github

from src.lib.utils import parse_github_url, init_logging
from src.lib.externaldata import ExternalData
from src import checker


log = logging.getLogger(__name__)


@contextlib.contextmanager
def indir(path):
    """
    >>> with indir(path):
    ...    # code executes with 'path' as working directory
    ... # old working directory is restored
    """

    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def print_outdated_external_data(manifest_checker):
    ext_data = manifest_checker.get_outdated_external_data()
    for data in ext_data:
        if data.new_version:
            if data.state == ExternalData.State.ADDED:
                print('ADDED: {}'.format(data.filename))
            elif data.state == ExternalData.State.REMOVED:
                print('REMOVED: {}'.format(data.filename))
            elif data.state == ExternalData.State.VALID:
                print('CHANGE SOON: {}\n'
                      ' Has a new version:'.format(data.filename))
            elif data.state == ExternalData.State.BROKEN:
                print('BROKEN: {}\n'
                      ' Has a new version:'.format(data.filename))
            else:
                print(' A new version is available:')

            print('  URL:     {url}\n'
                  '  SHA256:  {checksum}\n'
                  '  Size:    {size}\n'
                  '  Version: {version}\n'.format(**data.new_version._asdict()))
        elif data.state == ExternalData.State.BROKEN:
            print('BROKEN: {}\n'
                  ' Unreachable URL: {}'.format(data.filename, data.current_version.url))
        print('')

    return len(ext_data) > 0


def check_call(args):
    log.debug("$ %s", " ".join(args))
    subprocess.check_call(args)


def commit_changes(changes):
    log.info("Committing updates")
    if len(changes) > 1:
        subject = "Update {} modules".format(len(changes))
        body = "\n".join(changes)
        message = subject + "\n\n" + body
    else:
        subject = changes[0]
        body = None
        message = subject

    # Moved to detached HEAD
    check_call(("git", "checkout", "HEAD@{0}"))
    check_call(("git", "commit", "-am", message))

    # Find a stable identifier for the contents of the tree, to avoid
    # sending the same PR twice.
    tree = subprocess.check_output(("git", "rev-parse", "HEAD^{tree}"))
    branch = "update-{}".format(tree.decode("ascii")[:7])

    try:
        # Check if the branch already exists
        subprocess.run(
            ("git", "rev-parse", "--verify", branch),
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        # If not, create it
        check_call(("git", "checkout", "-b", branch))
    return subject, body, branch


DISCLAIMER = (
    "<i>(This pull request was automatically generated by "
    "[flathub/flatpak-external-data-checker]"
    "(https://github.com/flathub/flatpak-external-data-checker). "
    "Please contact @barthalion or @wjt if you have any questions or complaints.)</i>"
)


def open_pr(subject, body, branch):
    log.info("Opening pull request for branch %s", branch)
    github_token = os.environ['GITHUB_TOKEN']
    g = Github(github_token)
    user = g.get_user()

    origin_url = subprocess.check_output((
        "git", "remote", "get-url", "origin",
    )).decode("utf-8").strip()
    origin_repo = g.get_repo(parse_github_url(origin_url))

    if origin_repo.permissions.push:
        log.debug("origin repo is writable")
        remote = "origin"
        repo = origin_repo
        fork = None
    else:
        log.debug("origin repo not writable; creating fork")
        fork = user.create_fork(origin_repo)
        remote = user.login
        repo = fork

    remote_url = f"https://{github_token}:x-oauth-basic@github.com/{repo.full_name}"
    if fork:
        check_call(("git", "remote", "add", remote, remote_url))
    else:
        check_call(("git", "remote", "set-url", remote, remote_url))

    base = "master"
    head = "{}:{}".format(repo.owner.login, branch)
    pr_message = ((body or "") + "\n\n" + DISCLAIMER).strip()
    # Include closed PRs – if the maintainer has closed our last PR, we don't want to
    # open another one.
    for pr in origin_repo.get_pulls(state="all", base=base, head=head):
        log.info(
            "Found existing %s PR: %s",
            "merged" if pr.is_merged() else pr.state,
            pr.html_url,
        )
        return

    check_call(("git", "push", "-u", remote, branch))

    pr = origin_repo.create_pull(
        subject,
        pr_message,
        base,
        head,
        maintainer_can_modify=True,
    )
    log.info("Opened pull request %s", pr.html_url)


def main():
    types = ['all'] + list(ExternalData.TYPES)
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", help="Flatpak manifest to check",
                        type=os.path.abspath)
    parser.add_argument('-v', '--verbose', help='Print debug messages',
                        action="store_true")
    parser.add_argument("--update",
                        help="Update manifest(s) to refer to new versions of "
                             "external data - also open PRs for changes unless "
                             "--commit-only is specified",
                        action="store_true")
    parser.add_argument("--commit-only",
                        help="Do not open PRs for updates, only commit changes "
                             "to external data (implies --update)",
                        action="store_true")
    parser.add_argument('--filter-type',
                        help='Only check external data of the given type',
                        choices=types, default='all')
    args = parser.parse_args()

    init_logging(logging.DEBUG if args.verbose else logging.INFO)

    manifest_checker = checker.ManifestChecker(args.manifest)
    filter_type = ExternalData.TYPES.get(args.filter_type)

    manifest_checker.check(filter_type)

    if print_outdated_external_data(manifest_checker):
        if args.update or args.commit_only:
            changes = manifest_checker.update_manifests()
            if changes:
                with indir(os.path.dirname(args.manifest)):
                    subject, body, branch = commit_changes(changes)
                    if not args.commit_only:
                        open_pr(subject, body, branch)
                return

            log.warning("Can't automatically fix any of the above issues")

        exit(1)