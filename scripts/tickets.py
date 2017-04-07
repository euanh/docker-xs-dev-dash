#!/usr/bin/env python

import re
import sys
import time
import argparse
import getpass
import logging
from jira import JIRAError
from jira.client import JIRA

from common import db_write
from common import add_common_parser_args

DB_URI = "http://localhost:8086/write?db=inforad"

JIRA_ENDPOINT = "https://issues.citrite.net"

COUNT_QUERIES = {
    "dc_inbox": 47168,  # "R3 Dash: DC Inbox",
    "CA,priority=Blocker":  47165,  # "R3 Dash: CA Blocker"
    "CA,priority=Critical": 47166,  # "R3 Dash: CA Critical"
    "CA,priority=Major": 47167,  # "R3 Dash: CA Major"
    "CA,priority=MinorAndTrivial": 47876,  # "R3 Dash: CA Minor and Trivial"
    "CA,priority=Non-bug": 48477,  # "R3 Dash: CA Non-Bug",
    "CA,workflow=Blocked": 52664,  # "R3 Dash: Blocked CA"
    "SCTX": 47170,  # "R3 Dash: SCTX"
    "XOP": 47169,  # "R3 Dash: XOP"
    "PAR": 47171,  # "R3 Dash: PAR"
    "Hotlist": 47531,  # "R3 Dash Hotlist"
    "Staging": 48797,  # "R3 Dash Staging"
    "FalconMustFix": 57062,  # "R3 Falcon Bugs"
}

QRF_DB_KEY = "CA,priority=QRF"
QRF_JIRA_FILTER = 47875  # "R3 Dash: Unresolved CA"

BACKLOG_DEPTH_DB_KEY = "backlog_depth"
BACKLOG_DEPTH_JIRA_FILTER = 50374  # "R3 Dash: Groomed Backlog"

SPRINT_BURNDOWN_DB_KEY = "sprint_burndown"
SPRINT_BURNDOWN_JIRA_FILTER = 50375  # "R3 Dash: Sprint Burndown"

SPRINT_VELOCITY_DB_KEY = "sprint_velocity"
SPRINT_BOARD_ID = 70
SPRINT_REGEX = r"^xs-ring3\s.+"

KEY_FIELD = "key"
DRV_FIELD = "customfield_18131"
STORY_POINTS_FIELD = "customfield_11332"


def retrieve_issues(jira, filter_id, fields, limit=None):
    try:
        max_results = False if limit is None else limit
        if isinstance(fields, list):
            fields = ",".join(fields)
        the_filter = jira.filter(filter_id)
        return jira.search_issues(getattr(the_filter, 'jql'),
                                  maxResults=max_results, fields=fields)
    except JIRAError as exn:
        sys.stderr.write("error: Connection to JIRA failed: %s\n" % exn)
        exit(3)


def retrieve_issue_count(jira, jira_filter):
    issues = retrieve_issues(jira, jira_filter, fields=[KEY_FIELD], limit=1)
    return issues.total


def retrieve_sum_of_field(jira, jira_filter, field):
    issues = retrieve_issues(jira, jira_filter, fields=[field])
    fields = [getattr(issue.fields, field) for issue in issues]
    return sum([f for f in fields if f is not None])


def retrieve_qrf(jira):
    qrf = retrieve_sum_of_field(jira, QRF_JIRA_FILTER, DRV_FIELD)
    return round(qrf, 3)


def retrieve_backlog_depth(jira):
    bkl_depth = retrieve_sum_of_field(jira, BACKLOG_DEPTH_JIRA_FILTER,
                                      STORY_POINTS_FIELD)
    return round(bkl_depth, 2)


def retrieve_sprint_burndown(jira):
    return retrieve_sum_of_field(jira, SPRINT_BURNDOWN_JIRA_FILTER,
                                 STORY_POINTS_FIELD)


def retrieve_sprint_velocity(jira, board_id, sprint_regex=None, window=3):
    try:
        sprints = jira.sprints(board_id)
        if sprint_regex:
            sprints = [s for s in sprints if re.match(sprint_regex, s.name)]
        completed = [s for s in sprints if s.state == 'CLOSED']

        def id_of_sprint(sprint):
            return sprint.id

        latest_completed = sorted(completed, key=id_of_sprint,
                                  reverse=True)[0:window]

        vels = []
        for spr in latest_completed:
            try:
                vels.append(jira.completedIssuesEstimateSum(board_id, spr.id))
            except KeyError:
                continue

        avg_v = float(sum(vels)) / len(vels) if vels else float('nan')
        return round(avg_v, 1)
    except JIRAError as exn:
        if exn.status_code == 403:
            sys.stderr.write("warn: Auth error in retrieve_sprint_velocity\n")
            return "AUTH_ERROR"


def parse_args_or_exit(argv=None):
    parser = argparse.ArgumentParser(
        description='Get number of open ring3 tickets and add to dashboard DB')
    parser.add_argument('-u', '--user',
                        help='Authenticate with JIRA using this user')
    add_common_parser_args(parser)
    return parser.parse_args(argv)


def jira_login(endpoint, user=None):
    try:
        basic_auth = None
        if user:
            if sys.stdin.isatty():
                password = getpass.getpass(stream=sys.stderr)
            else:
                password = sys.stdin.readline().rstrip()
            basic_auth = (user, password)
        try:
            jira = JIRA({'server': endpoint}, basic_auth=basic_auth)
        except JIRAError:
            sys.stderr.write("warn: Autentication to JIRA failed," +
                             " continuing unauthenticated\n")
            jira = JIRA({'server': endpoint})
        # pylint: disable=protected-access
        if "JSESSIONID" in jira._session.cookies:
            # drop basic auth if we have a cookie (for performance)
            jira._session.auth = None
        return jira
    except JIRAError as exn:
        sys.stderr.write("Connection to JIRA failed: %s: %s\n" %
                         (exn.response.status_code, exn.response.reason))
        exit(4)


def main():
    args = parse_args_or_exit(sys.argv[1:])
    logging.captureWarnings(True)
    jira = jira_login(JIRA_ENDPOINT, args.user)
    values = {}
    for (db_key, jira_filter) in COUNT_QUERIES.iteritems():
        values[db_key] = retrieve_issue_count(jira, jira_filter)
    values[QRF_DB_KEY] = retrieve_qrf(jira)
    values[BACKLOG_DEPTH_DB_KEY] = retrieve_backlog_depth(jira)
    values[SPRINT_BURNDOWN_DB_KEY] = retrieve_sprint_burndown(jira)
    values[SPRINT_VELOCITY_DB_KEY] =\
        retrieve_sprint_velocity(jira, SPRINT_BOARD_ID, SPRINT_REGEX, 3)

    if args.dry_run:
        print "---\nRetrieved the following values: %s" % values
        exit(0)
    # use same timestamp for all database writes for consistent key
    tstamp = int(time.time()) * 10 ** 9
    for (key, value) in values.iteritems():
        db_write(DB_URI, key, value, tstamp)

if __name__ == "__main__":
    main()
