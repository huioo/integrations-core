# (C) Datadog, Inc. 2010-2017
# All rights reserved
# Licensed under Simplified BSD License (see LICENSE)
from __future__ import print_function, unicode_literals
import os
import re
import sys
import json
from collections import namedtuple
from datetime import datetime

from six.moves.urllib.request import urlopen
from six import StringIO
from invoke import task
from invoke.exceptions import Exit
from packaging import version

from .constants import ROOT, GITHUB_API_URL, AGENT_BASED_INTEGRATIONS
from .utils import get_version_string, get_release_tag_string

# match something like `(#1234)` and return `1234` in a group
PR_REG = re.compile(r'\(\#(\d+)\)')

CHANGELOG_LABEL_PREFIX = 'changelog/'
CHANGELOG_TYPE_NONE = 'no-changelog'
CHANGELOG_TYPES = [
    'Added',
    'Changed',
    'Deprecated',
    'Fixed',
    'Removed',
    'Security',
]

ChangelogEntry = namedtuple('ChangelogEntry', 'number, title, url, author, author_url, is_contributor')


def parse_pr_numbers(git_log_lines):
    """
    Parse PR numbers from commit messages. At GitHub those have the format:

        `here is the message (#1234)`

    being `1234` the PR number.
    """
    prs = []
    for line in git_log_lines:
        match = re.search(PR_REG, line)
        if match:
            prs.append(match.group(1))
    return prs


def is_contributor(payload):
    """
    If the PR comes from a fork, we can safely assumed it's from an
    external contributor.
    """
    return payload.get('head', {}).get('repo', {}).get('fork') is True


@task(help={
    'target': "The check to compile the changelog for",
    'dry-run': "Runs the task without actually doing anything",
})
def update_changelog(ctx, target, new_version, dry_run=False):
    """
    Update the changelog for the given check with the changes
    since the current release.

    Example invocation:
        inv update-changelog redisdb 3.1.1
    """
    # sanity check on the target
    if target not in AGENT_BASED_INTEGRATIONS:
        raise Exit("Provided target is not an Agent-based Integration")

    # sanity check on the version provided
    p_version = version.parse(new_version)
    p_current = version.parse(get_version_string(target))
    if p_version <= p_current:
        raise Exit("Current version is {}, can't bump to {}".format(p_current, p_version))
    print("Current version of check {}: {}, bumping to: {}".format(target, p_current, p_version))

    do_update_changelog(ctx, target, str(p_current), new_version, dry_run)


def do_update_changelog(ctx, target, cur_version, new_version, dry_run=False):
    """
    Actually perform the operations needed to update the changelog, this
    method is supposed to be used by other tasks and not directly.
    """
    # get the name of the current release tag
    target_tag = get_release_tag_string(target, cur_version)

    # get the diff from HEAD
    target_path = os.path.join(ROOT, target)
    cmd = 'git log --pretty=%s {}... {}'.format(target_tag, target_path)
    diff_lines = ctx.run(cmd, hide='out').stdout.split('\n')

    # for each PR get the title, we'll use it to populate the changelog
    endpoint = GITHUB_API_URL + '/repos/DataDog/integrations-core/pulls/{}'
    pr_numbers = parse_pr_numbers(diff_lines)
    print("Found {} PRs merged since tag: {}".format(len(pr_numbers), target_tag))

    entries = []
    for pr_num in pr_numbers:
        try:
            response = urlopen(endpoint.format(pr_num))
        except Exception as e:
            sys.stderr.write("Unable to fetch info for PR #{}\n: {}".format(pr_num, e))
            continue

        payload = json.loads(response.read())
        changelog_labels = []
        for l in payload.get('labels', []):
            name = l.get('name')
            if name.startswith(CHANGELOG_LABEL_PREFIX):
                # only add the name, e.g. for `changelog/Added` it's just `Added`
                changelog_labels.append(name.split(CHANGELOG_LABEL_PREFIX)[1])

        if not changelog_labels:
            raise Exit("No valid changelog labels found attached to PR #{}, please add one".format(pr_num))
        elif len(changelog_labels) > 1:
            raise Exit("Multiple changelog labels found attached to PR #{}, please use only one".format(pr_num))

        changelog_type = changelog_labels[0]
        if changelog_type == CHANGELOG_TYPE_NONE:
            # No changelog entry for this PR
            print("Skipping PR #{} from changelog".format(pr_num))
            continue

        author = payload.get('user', {}).get('login')
        author_url = payload.get('user', {}).get('html_url')
        title = '[{}] {}'.format(changelog_type, payload.get('title'))

        entry = ChangelogEntry(pr_num, title, payload.get('html_url'),
                               author, author_url, is_contributor(payload))

        entries.append(entry)

    # store the new changelog in memory
    new_entry = StringIO()

    # the header contains version and date
    header = "### {} / {}\n".format(new_version, datetime.now().strftime("%Y-%m-%d"))
    new_entry.write(header)

    # one bullet point for each PR
    new_entry.write("\n")
    for entry in entries:
        thanknote = ""
        if entry.is_contributor:
            thanknote = " Thanks [{}]({}).".format(entry.author, entry.author_url)
        new_entry.write("* {}. See [#{}]({}).{}\n".format(entry.title, entry.number, entry.url, thanknote))
    new_entry.write("\n")

    # read the old contents
    changelog_path = os.path.join(ROOT, target, "CHANGELOG.md")
    with open(changelog_path, 'r') as f:
        old = f.readlines()

    # write the new changelog in memory
    changelog = StringIO()

    # preserve the title
    changelog.write("".join(old[:2]))

    # prepend the new changelog to the old contents
    # make the command idempotent
    if header not in old:
        changelog.write(new_entry.getvalue())

    # append the rest of the old changelog
    changelog.write("".join(old[2:]))

    # print on the standard out in case of a dry run
    if dry_run:
        print(changelog.getvalue())
        sys.exit(0)

    # overwrite the old changelog
    with open(changelog_path, 'w') as f:
        f.write(changelog.getvalue())
