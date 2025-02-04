from __future__ import annotations

from static import *
import logging
from datetime import datetime, timedelta
from typing import List
import humanize
import iso8601
import praw
import prawcore
import pytz
import re

from sqlalchemy import *
from sqlalchemy.ext.declarative import declarative_base
from praw.models.listing.generator import ListingGenerator
import queue
from models.reddit_models import SubAuthor, SubmittedPost, \
    TrackedAuthor, TrackedSubreddit, RedditInterface, PostingGroup
from logger import logger
from sqlalchemy import exc
from settings import MAIN_BOT_NAME
from nsfw_monitoring import check_post_nsfw_eligibility



def check_new_submissions(wd: WorkingData, query_limit=800, sub_list='mod', intensity=0):
    subreddit_names = []
    subreddit_names_complete = []
    logger.info(f"main/CNW: pulling new posts!  intensity: {intensity}")

    possible_new_posts = [a for a in wd.ri.reddit_client.subreddit(sub_list).new(limit=query_limit)]

    count = 0
    total = 0
    for post_to_review in possible_new_posts:
        total += 1
        subreddit_name = str(post_to_review.subreddit).lower()

        # If we have seen this post for this subreddit, stop going any further
        if intensity == 0 and subreddit_name in subreddit_names_complete:
            # print(f'done w/ {subreddit_name} @ {total}')
            continue

        # check if we know this post
        previous_post: SubmittedPost = wd.s.query(SubmittedPost).get(post_to_review.id)
        if previous_post:  # seen this post before -> ignore posts from this  sub
            subreddit_names_complete.append(subreddit_name)
            # logger.info(f"seen this post before {subreddit_name} {post_to_review.id}")
            continue
        if not previous_post:  # have not seen this post, add to db
            post = SubmittedPost(post_to_review)
            if post.subreddit_name in wd.nsfw_monitoring_subs:   # do nsfw eligibility check if applicable
                check_post_nsfw_eligibility(wd, post)

            wd.s.add(post)
            count += 1
    logger.info(f'main/CNW: found {count} posts out of {total}')
    wd.s.commit()


def check_spam_submissions(wd: WorkingData, sub_list='mod', intensity=0):
    possible_spam_posts = []
    try:
        possible_spam_posts = [a for a in wd.ri.reddit_client.subreddit(sub_list).mod.spam(only='submissions')]
    except prawcore.exceptions.Forbidden:
        pass
    for post_to_review in possible_spam_posts:
        previous_post: SubmittedPost = wd.s.query(SubmittedPost).get(post_to_review.id)
        if previous_post and intensity == 0:
            break
        if not previous_post:
            post = SubmittedPost(post_to_review)
            if post.banned_by is True:
                post.posted_status = PostedStatus.AUTOMOD_RM
            elif post.banned_by == "AutoModerator":
                post.posted_status = PostedStatus.SPAM_FLT.value
            post.reviewed = True
            sub_list = post.subreddit_name.lower()
            # logger.info("found spam post: '{0}...' http://redd.it/{1} ({2})".format(post.title[0:20], post.id,
            #                                                                         subreddit_name))

            # post.reviewed = True
            wd.s.add(post)
            subreddit_author: SubAuthor = wd.s.query(SubAuthor).get((sub_list, post.author))
            if subreddit_author and subreddit_author.hall_pass >= 1:
                subreddit_author.hall_pass -= 1
                post.api_handle.mod.approve()
                wd.s.add(subreddit_author)
    wd.s.commit()


def check_for_post_exemptions(tr_sub: TrackedSubreddit, recent_post: SubmittedPost, wd=None):  # uses some reddit api
    # check if removed
    if recent_post.counted_status_enum not in (CountedStatus.NEEDS_UPDATE, CountedStatus.NOT_CHKD, CountedStatus.PREV_EXEMPT, CountedStatus.COUNTS, CountedStatus.REVIEWED):
        return CountedStatus(recent_post.counted_status_enum),\
               f"previously exempted {CountedStatus(recent_post.counted_status_enum)}"

    posted_status = recent_post.posted_status
    if posted_status == PostedStatus.UNKNOWN.value \
            or (recent_post.last_checked
                and recent_post.last_checked < datetime.now(pytz.utc).replace(tzinfo=None) - timedelta(hours=3)):
        posted_status = wd.ri.get_posted_status(recent_post, get_removed_info=True)  # uses some reddit api
        recent_post.posted_status = posted_status.value
        recent_post.post_flair = recent_post.api_handle.link_flair_text
        recent_post.author_flair = recent_post.api_handle.author_flair_text
        # recent_post.author_css = recent_post.api_handle.author_css_text
        recent_post.last_checked = datetime.now(pytz.utc)

        wd.s.add(recent_post)
        wd.s.commit()
    else:
        print(f"recently updated, assuming no change to posted status {posted_status}")
    # banned_by = recent_post.get_api_handle().banned_by
    # logger.debug(">>>>exemption status: {}".format(banned_by))

    # These should already be identified - except for author/post flairs? May not know if they were recently updated
    if posted_status == PostedStatus.SPAM_FLT:
        return CountedStatus.SPAMMED_EXMPT, ""
    elif tr_sub.ignore_AutoModerator_removed and posted_status == PostedStatus.AUTOMOD_RM:
        return CountedStatus.AM_RM_EXEMPT, ""
    elif tr_sub.ignore_moderator_removed and posted_status == PostedStatus.FH_RM:
        return CountedStatus.FLAIR_HELPER, ""
    elif tr_sub.ignore_moderator_removed and posted_status == PostedStatus.MOD_RM:
        return CountedStatus.MOD_RM_EXEMPT, ""
    elif tr_sub.exempt_oc and recent_post.is_oc:  # won't change
        return CountedStatus.OC_EXEMPT, ""
    elif tr_sub.exempt_self_posts and recent_post.is_self:  # wont change
        return CountedStatus.SELF_EXEMPT, ""
    elif tr_sub.exempt_link_posts and recent_post.is_self is not True:  # won't change
        return CountedStatus.LINK_EXEMPT, ""
    if tr_sub.exempt_moderator_posts and recent_post.author in tr_sub.subreddit_mods: # may change
        return CountedStatus.MODPOST_EXEMPT, "moderator exempt"
    # check if flair-exempt
    try:
        author_flair = wd.ri.get_submission_api_handle(recent_post).author_flair_text  # Reddit API
        author_css = None
        #author_css = wd.ri.get_submission_api_handle(recent_post).auth  # Reddit API
    except prawcore.exceptions.Forbidden:
        print("can't access flair")
        author_flair = None
    # add CSS class to author_flair
    #if author_flair and wd.ri.get_submission_api_handle(recent_post).author_flair_css_class:  # Reddit API
    #     author_flair = author_flair + wd.ri.get_submission_api_handle(recent_post).author_flair_css_class  # Reddit API

    # Flair keyword exempt
    if tr_sub.author_exempt_flair_keyword and isinstance(tr_sub.author_exempt_flair_keyword, str) \
            and ((author_flair and tr_sub.author_exempt_flair_keyword in author_flair)
            or (author_css and tr_sub.author_not_exempt_flair_keyword in author_css)):
        logger.debug(">>>flair exempt")
        return CountedStatus.FLAIR_EXEMPT, "flair exempt {}".format(author_flair)

    # Not-flair-exempt keyword (Only restrict certain flairs)
    if tr_sub.author_not_exempt_flair_keyword \
            and ((author_flair and tr_sub.author_not_exempt_flair_keyword not in author_flair) or not author_flair):
        return CountedStatus.FLAIR_NOT_EXEMPT, "flair not exempt {}".format(author_flair)

    # check if title keyword exempt:
    if tr_sub.title_exempt_keyword:
        flex_title = recent_post.title.lower()
        if (isinstance(tr_sub.title_exempt_keyword, str)
            and tr_sub.title_exempt_keyword.lower() in flex_title) or \
                (isinstance(tr_sub.title_exempt_keyword, list)
                 and any(x in flex_title for x in [y.lower() for y in tr_sub.title_exempt_keyword])):
            logger.debug(">>>title keyword exempted")
            return CountedStatus.TITLE_KW_EXEMPT, f"title keyword exempt {tr_sub.title_exempt_keyword} -> exemption"

    # title keywords only to restrict:
    if tr_sub.title_not_exempt_keyword:
        link_flair = wd.ri.get_submission_api_handle(recent_post).link_flair_text  # Reddit API
        if link_flair:
            flex_title = recent_post.title.lower() + link_flair
        else:
            flex_title = recent_post.title.lower()
        print(flex_title)
        # example: restriction "Selfies"
        # if there is a restriction and required keyword is not in title -> does not meet restriction criteria, exempt
        if (isinstance(tr_sub.title_not_exempt_keyword, str)
            and tr_sub.title_not_exempt_keyword.lower() not in flex_title) or \
                (isinstance(tr_sub.title_not_exempt_keyword, list)
                 and all(x not in flex_title for x in [y.lower() for y in tr_sub.title_not_exempt_keyword])):
            logger.debug(f">>>meets restriction criteria: {flex_title}, restriction: {tr_sub.title_not_exempt_keyword}")
            return CountedStatus.TITLE_CRITERIA_NOT_MET, f"title does not have {tr_sub.title_not_exempt_keyword} -> exemption"
    return CountedStatus.COUNTS, "no exemptions"



def automated_reviews(wd):
    logger.debug("AR: excluding mod posts...")
    now_date = datetime.now(pytz.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")

    # ignore moderators
    """
    rs = wd.s.update(SubmittedPost)\
        .join(TrackedSubreddit, SubmittedPost.subreddit_name == TrackedSubreddit.subreddit_name)\
        .where(SubmittedPost.counted_status < 1,
                SubmittedPost.reviewed == 0,
                TrackedSubreddit.mod_list.like(func.concat("%", SubmittedPost.author, "%")))\
        .values({SubmittedPost.counted_status: CountedStatus.MODPOST_EXEMPT.value,
                 SubmittedPost.last_reviewed: datetime.now(pytz.utc)})
    """
    rs = wd.s.execute('UPDATE RedditPost t '
                   'INNER JOIN TrackedSubs s ON t.subreddit_name = s.subreddit_name '
                   'SET counted_status = :counted_status, reviewed = 1 '
                   'WHERE t.counted_status < 1 and t.reviewed = 0 and s.mod_list like CONCAT("%", t.author, "%") ',
                   {"counted_status": CountedStatus.MODPOST_EXEMPT.value,
                    "last_reviewed": now_date})
    logger.debug(rs.rowcount)


    logger.debug("AR: excluding self posts...")
    # ignore self posts
    rs = wd.s.execute("UPDATE RedditPost t "
                   "INNER JOIN TrackedSubs s ON t.subreddit_name = s.subreddit_name "
                   "SET counted_status = :counted_status, reviewed = 1, last_reviewed = :last_reviewed "
                   "WHERE t.counted_status < 1 "
                   "AND t.reviewed = 0 and t.is_self is TRUE and s.exempt_self_posts is TRUE",
                   {"counted_status": CountedStatus.SELF_EXEMPT.value,
                    "banned_by": "AutoModerator",
                    "last_reviewed": now_date})
    logger.debug(rs.rowcount)

    """  No one has excluded link posts...
    print("AR: excluding link posts...")
    # ignore link posts
    rs = wd.s.execute("UPDATE RedditPost t "
                   "INNER JOIN TrackedSubs s ON t.subreddit_name = s.subreddit_name "
                   "SET counted_status = :counted_status, reviewed = 1, last_reviewed = :last_reviewed "
                   "WHERE t.counted_status < 1 "
                   "AND t.reviewed = 0 and t.is_self is FALSE and s.exempt_link_posts is TRUE",
                   {"counted_status": CountedStatus.LINK_EXEMPT.value})
    print(rs.rowcount)
    """

    logger.debug("AR excluding OC posts")
    # ignore OC
    rs = wd.s.execute("UPDATE RedditPost t "
                   "INNER JOIN TrackedSubs s ON t.subreddit_name = s.subreddit_name "
                   "SET counted_status = :counted_status, reviewed = 1, last_reviewed = :last_reviewed "
                   "WHERE t.counted_status < 1 "
                   "AND  t.reviewed = 0 and t.is_oc is TRUE and s.exempt_oc is TRUE",
                   {"counted_status": CountedStatus.OC_EXEMPT.value,
                    "last_reviewed": now_date})

    #ignore autoremoved
    logger.debug("AR excluding autoremoved posts...")
    rs = wd.s.execute("UPDATE RedditPost t "
                   "INNER JOIN TrackedSubs s ON t.subreddit_name = s.subreddit_name "
                   "SET counted_status = :counted_status, reviewed = 1, last_reviewed = :last_reviewed "
                   "WHERE t.counted_status < 1 "
                   "AND s.ignore_Automoderator_removed = 1 AND t.posted_status like :posted_status",
                   {"counted_status": CountedStatus.AM_RM_EXEMPT.value,
                    "posted_status": PostedStatus.AUTOMOD_RM.value,
                    "last_reviewed": now_date})
    logger.debug(rs.rowcount)
    logger.debug("AR excluding moderator removed posts...DOES NOT INCLUDE Flair helper??")
    # ignore link posts
    rs = wd.s.execute("UPDATE RedditPost t "
                   "INNER JOIN TrackedSubs s ON t.subreddit_name = s.subreddit_name "
                   "SET counted_status = :counted_status, reviewed = 1, last_reviewed = :last_reviewed "
                   "WHERE t.counted_status < 1 "
                   "AND  s.ignore_moderator_removed = 1 AND t.posted_status like :posted_status",
                   {"counted_status": CountedStatus.MOD_RM_EXEMPT.value,
                    "posted_status": PostedStatus.MOD_RM.value,
                    "last_reviewed": now_date})
    logger.debug(rs.rowcount)

    logger.debug("AR: excluding author flair")
    rs = wd.s.execute('UPDATE RedditPost t '
                   'INNER JOIN TrackedSubs s ON t.subreddit_name = s.subreddit_name '
                   'SET counted_status = :counted_status, reviewed = 1, last_reviewed = :last_reviewed '
                   'WHERE t.reviewed = 0 AND t.counted_status <1 '
                   'AND s.author_exempt_flair_keyword is not NULL and t.author_flair is not NULL '
                   'AND t.author_flair REGEXP s.author_exempt_flair_keyword ',
                   {"counted_status": CountedStatus.FLAIR_EXEMPT.value,
                    "last_reviewed": now_date})
    logger.debug(rs.rowcount)
    logger.debug("AR: author flair inclusion")
    rs = wd.s.execute('UPDATE RedditPost t '
                   'INNER JOIN TrackedSubs s ON t.subreddit_name = s.subreddit_name '
                   'SET counted_status = :counted_status, reviewed = 1, last_reviewed = :last_reviewed  '
                   'WHERE t.reviewed = 0 AND t.counted_status <1 '
                   'AND s.author_not_exempt_flair_keyword is NOT NULL '
                   'AND (t.author_flair is NULL '
                   'OR NOT (t.author_flair REGEXP s.author_not_exempt_flair_keyword)'
                   ')',
                   {"counted_status": CountedStatus.FLAIR_EXEMPT.value,
                    "last_reviewed": now_date})
    logger.debug(rs.rowcount)
    logger.debug("AR: excluding title/post_flair")
    rs = wd.s.execute('UPDATE RedditPost t '
                   'INNER JOIN TrackedSubs s ON t.subreddit_name = s.subreddit_name '
                   'SET counted_status = :counted_status, reviewed = 1 , last_reviewed = :last_reviewed  '
                   'WHERE t.reviewed = 0 AND t.counted_status <1 '
                   'AND s.title_exempt_keyword is not NULL '
                   'AND CONCAT(t.title, COALESCE(t.post_flair)) REGEXP s.author_exempt_flair_keyword ',
                   {"counted_status": CountedStatus.TITLE_KW_EXEMPT.value,
                    "last_reviewed": now_date})
    logger.debug(rs.rowcount)
    logger.debug("AR: inclusion title/post flair - reversed")
    rs = wd.s.execute('UPDATE RedditPost t '
                   'INNER JOIN TrackedSubs s ON t.subreddit_name = s.subreddit_name '
                   'SET counted_status = :counted_status, reviewed = 1, last_reviewed = :last_reviewed  '
                   'WHERE t.reviewed = 0 AND t.counted_status <1 '
                   'AND s.title_not_exempt_keyword  is NOT NULL '
                   'AND NOT (CONCAT(t.title, COALESCE(t.post_flair)) REGEXP s.title_not_exempt_keyword)',
                   {"counted_status": CountedStatus.TITLE_KW_EXEMPT.value,
                    "last_reviewed": now_date})
    logger.debug(rs.rowcount)



    """
    logger.info(f"finding blacklist violations")
    rs = wd.s.execute('UPDATE RedditPost p '
                   'INNER JOIN SubAuthor a ON p.author = a.author_name AND p.subreddit_name == a.subreddit_name '
                   'SET counted_status = :counted_status, reviewed = 1 '
                   'WHERE p.reviewed = 0 AND p.counted_status <1 '
                   'AND p.time_utc < a.next_eligible '
                   'AND p.time_utc > utc_timestamp() - INTERVAL 24 HOUR',
                   {"counted_status": CountedStatus.BLKLIST_NEED_REMOVE})
    print(rs.rowcount)
    """

    tick = datetime.now()
    logger.info(f"identifying blacklist violations")
    tuples = (wd.s.query(SubmittedPost, SubAuthor)).select_from(SubmittedPost).join(SubAuthor, and_(
        SubAuthor.author_name == SubmittedPost.author,
        SubAuthor.subreddit_name == SubmittedPost.subreddit_name)). \
        filter(SubmittedPost.reviewed.is_(False),
               SubmittedPost.time_utc < SubAuthor.next_eligible,
               SubmittedPost.time_utc > tick.replace(tzinfo=None) - timedelta(hours=24)
               ).all()

    for j, tuple1 in enumerate(tuples):
        op, subreddit_author = tuple1
        assert (isinstance(op, SubmittedPost))
        assert (isinstance(subreddit_author, SubAuthor))
        logger.info(f"checking post for softblacklist: {j} {op.author} {op.title}")
        tr_sub: TrackedSubreddit = get_subreddit_by_name(wd, op.subreddit_name, update_if_due=False)
        if not tr_sub:
            logger.info(f"Could not find subreddit {op.subreddit_name}")
            continue
        if tr_sub.active_status_enum not in (SubStatus.ACTIVE, SubStatus.NO_BAN_ACCESS):
            logger.info(f"active status not applicable {op.subreddit_name}")
            continue
        last_valid_post: SubmittedPost = wd.s.query(SubmittedPost).get(
            subreddit_author.last_valid_post) if subreddit_author.last_valid_post is not None else None
        if tr_sub.comment:
            op.reply_comment = make_comment(tr_sub, op, [last_valid_post, ],
                                            tr_sub.comment, distinguish=tr_sub.distinguish, approve=tr_sub.approve,
                                            lock_thread=tr_sub.lock_thread, stickied=tr_sub.comment_stickied,
                                            next_eligibility=subreddit_author.next_eligible, blacklist=True, wd=wd,
                                            do_actual_comment=False)
        else:
            logger.info(f"Making comment is not set")
        op.counted_status_enum = CountedStatus.BLKLIST_NEED_REMOVE
        op.reviewed = True
        logger.info(f"added to blacklist: {j} {op.author} {op.title} {op.counted_status_enum}")
        wd.s.add(op)
    wd.s.commit()


def do_reddit_actions(wd):
    # assert(isinstance(wd.todoq, queue.Queue))
    # assert(isinstance(wd.doneq, queue.Queue))

    print("do status updates")
    to_update = wd.s.query(SubmittedPost)\
        .filter(SubmittedPost.counted_status_enum == CountedStatus.NEEDS_UPDATE)\
        .filter(SubmittedPost.time_utc > datetime.now(pytz.utc).replace(tzinfo=None) - timedelta(hours=48))
    for op in to_update:
        assert(isinstance(op, SubmittedPost))
        op.posted_status = wd.ri.get_posted_status(op).value
        op.last_checked = datetime.now(pytz.utc)
        wd.s.add(op)
    wd.s.commit()

    print("do removals...")
    to_remove = wd.s.query(SubmittedPost)\
        .filter(or_(SubmittedPost.counted_status_enum == CountedStatus.BLKLIST_NEED_REMOVE,
                    SubmittedPost.counted_status_enum == CountedStatus.NEED_REMOVE))\
        .filter(SubmittedPost.time_utc > datetime.now(pytz.utc).replace(tzinfo=None) - timedelta(hours=24))
    # print(f"blacklist removals {to_remove.rowcount}")
    for op in to_remove:
        # logger.info(f'removing post {op.author} {op.title} {op.subreddit_name}')
        tr_sub = get_subreddit_by_name(wd, op.subreddit_name)
        try:
            wd.ri.get_submission_api_handle(op).mod.remove()
            logger.info(f'remove successful!: {op.subreddit_name} {op.author} {op.title}')
            new_counted_status = CountedStatus.REMOVED \
                if op.counted_status_enum == CountedStatus.NEED_REMOVE else CountedStatus.BLKLIST
            if op.reply_comment:
                wd.ri.reply(op, op.reply_comment, distinguish=tr_sub.distinguish, approve=tr_sub.approve,
                            lock_thread=tr_sub.lock_thread)
            op.counted_status_enum = new_counted_status
            op.reply_comment = None
        except prawcore.exceptions.Forbidden as e:
            logger.warning(f'No permission to remove post {op.author} {op.title} {op.subreddit_name} {str(e)}')
            op.counted_status_enum = CountedStatus.REMOVE_FAILED
            wd.sub_dict[op.subreddit_name].active_status_enum = SubStatus.NO_REMOVE_ACCESS
            wd.s.add(wd.sub_dict[op.subreddit_name])
            print('test')
        except (praw.exceptions.APIException,  prawcore.exceptions.ServerError) as e:
            logger.warning(f'something went wrong in removing post {op.author} {op.title} {op.subreddit_name} {str(e)}')
            op.counted_status_enum = CountedStatus.REMOVE_FAILED


        wd.s.add(op)
    wd.s.commit()

def look_for_rule_violations4(wd):
    # automated_reviews(wd)
    last_reviews = datetime.now(pytz.utc)
    # do_reddit_actions(wd)

    # query all groups:
    #   posts: active status >3, counted_status <2, within  post interval
    #   group: count > max count per interval; most recent within last 72 hours, **added_time -since last checked?**
    #
    # go through previously marked
    #  check if complete?
    #  rule out grace periods, hall passes
    #
    # go through groups:
    #    if any of them need posted_status updates, mark for update, then skip
    #

    # Memory of recent posts
    posts_checked = {}


    # Find most recently checked date
    if wd.most_recent_review is None:
        most_recent_post = wd.s.query(SubmittedPost).filter(SubmittedPost.review_debug.like("ma:%")) \
            .order_by(SubmittedPost.date_added.asc()).one()
        wd.most_recent_review = most_recent_post.date_added

    """UPDATE table2
    INNER JOIN (SELECT MIN(IFNULL(table1.views,0)) counted
    FROM table1
    GROUP BY table1.id
    HAVING counted>0  ,
    ) x ON x.id = table2.id
    SET table2.number = x.counted"""

    """
    UPDATE RedditPost p 
    INNER JOIN s.max_count_per_interval, s.min_post_interval_mins 
    FROM TrackedSubs s 
    GROUP BY p.author, p.subreddit_name 
    HAVING COUNT(p.author) > s.max_count_per_interval
    """
    more_accurate_statement = "SELECT MAX(t.id), " \
                              "GROUP_CONCAT(t.id ORDER BY t.id) as debug, " \
                              "GROUP_CONCAT(t.reviewed ORDER BY t.id), " \
                              "t.author, " \
                              "t.subreddit_name, " \
                              "GROUP_CONCAT(t.counted_status ORDER BY t.id), " \
                              "COUNT(t.author), " \
                              "MAX(t.time_utc) as most_recent, " \
                              "t.reviewed, " \
                              "t.flagged_duplicate, " \
                              "s.is_nsfw, " \
                              "s.max_count_per_interval, " \
                              "s.min_post_interval_mins/60, " \
                              "s.active_status " \
                              "FROM RedditPost t INNER JOIN TrackedSubs s ON t.subreddit_name = s.subreddit_name " \
                              "WHERE s.active_status >3 and counted_status <2 "\
                              "AND t.time_utc > utc_timestamp() - INTERVAL s.min_post_interval_mins MINUTE  " \
                              "GROUP BY t.author, t.subreddit_name " \
                              "HAVING COUNT(t.author) > s.max_count_per_interval " \
                              "AND most_recent > utc_timestamp() - INTERVAL 72 HOUR AND MAX(added_time) > :look_back  " \
                              "ORDER BY most_recent desc ;"

    # First check which ones need to be checked
    posts_to_check = wd.s.query(SubmittedPost)\
        .filter(SubmittedPost.counted_status_enum == CountedStatus.NEEDS_UPDATE,
                SubmittedPost.time_utc > datetime.now(pytz.utc) - timedelta(hours=48))\
        .order_by(SubmittedPost.time_utc.asc()).all()
    for post in posts_to_check:
        prior_post_ids = post.review_debug.replace("ma:","").split(",")
        # review previous posts
        for pp_id in prior_post_ids:
            # get the objects
            if pp_id in posts_checked:
                pp = posts_checked[pp_id]
            else:
                pp = wd.s.query(SubmittedPost).get(pp_id)
                posts_checked[pp_id]=pp


def look_for_rule_violations3(wd):

    # need to rule out easy ones - moderators, etc.
    automated_reviews(wd)

    # posts that are deleted/removed
    # exempted posts:
    #   permanent: moderator post, title keywords, self/oc
    #   non permanent: author/post flair, moderator
    # grace period
    # hall pass
    # sub not active
    #


    # get "leftover" posts that were not checked
    logger.debug(f"LRWT: querying recent post(s)")
    posting_groups = []
    most_recent_identified = None
    look_back_hrs = 48

    posts_to_verify = wd.s.query(SubmittedPost) \
        .join(TrackedSubreddit, TrackedSubreddit.subreddit_name == SubmittedPost.subreddit_name, isouter=False) \
        .filter(SubmittedPost.reviewed == 0,
                SubmittedPost.counted_status_enum.in_((CountedStatus.NEEDS_UPDATE, CountedStatus.NOT_CHKD)),
                SubmittedPost.review_debug.like("ma:%"),
                SubmittedPost.time_utc > datetime.now() - timedelta(hours=look_back_hrs),
                TrackedSubreddit.active_status_enum.in_((SubStatus.ACTIVE,SubStatus.NO_BAN_ACCESS))
                ).order_by(SubmittedPost.added_time.desc()).all()

    for post in posts_to_verify:
        if not most_recent_identified:
            most_recent_identified = post
        assert isinstance(post, SubmittedPost)
        post_ids = post.review_debug.replace("ma:", "").split(',')

        posts = []
        for post_id in post_ids:
            posts.append(wd.s.query(SubmittedPost).get(post_id))
        posting_groups.append(
            PostingGroup(post.id, author_name=post.author, subreddit_name=post.subreddit_name, posts=posts))
    logger.debug(f"# of leftover posts from before: {len(posting_groups)}")

    logger.debug(f"leftover posts from before: {len(posting_groups)}")

    if not most_recent_identified:
        most_recent_identified: SubmittedPost | None = wd.s.query(SubmittedPost) \
            .filter(SubmittedPost.review_debug.like("ma:%")) \
            .order_by(SubmittedPost.added_time).first()

    # AND (most_recent > MAX(t.last_checked) or max(t.last_checked) is NULL)
    more_accurate_statement = """
    SELECT 
        MAX(t.id), 
        GROUP_CONCAT(t.id ORDER BY t.id), 
        GROUP_CONCAT(t.reviewed ORDER BY t.id), 
        t.author, 
        t.subreddit_name, 
        GROUP_CONCAT(t.counted_status ORDER BY t.id), 
        COUNT(t.author), 
        MAX(t.time_utc) as most_recent, 
        t.reviewed, 
        t.flagged_duplicate, 
        s.is_nsfw, 
        s.max_count_per_interval, 
        s.min_post_interval_mins/60, 
        s.active_status_enum FROM RedditPost t 
        INNER JOIN TrackedSubs s 
            ON t.subreddit_name = s.subreddit_name 
        WHERE 
            s.active_status_enum in ('ACTIVE', 'NO_BAN_ACCESS') 
            and counted_status_enum  in ('NEEDS_UPDATE', 'NOT_CHKD', 'PREV_EXEMPT', 'COUNTS')
            AND t.time_utc > utc_timestamp() - INTERVAL s.min_post_interval_mins MINUTE  
        GROUP BY 
            t.author, t.subreddit_name 
            HAVING COUNT(t.author) > s.max_count_per_interval 
            AND most_recent > utc_timestamp() - INTERVAL 72 HOUR 
            AND MAX(added_time) > :look_back  
        ORDER BY most_recent desc ;"""

    # more_accurate_statement.replace("[date]")
    search_back = 48
    more_accurate_statement = more_accurate_statement.replace('72', str(search_back))

    tick = datetime.now()
    last_date = most_recent_identified.added_time.isoformat() \
        if most_recent_identified and most_recent_identified.added_time else "2022-06-30 00:00:00"
    logger.debug(f"doing more accurate {datetime.now()} last date:{last_date}")
    # last_date = "2022-06-30 00:00:00"  # REMOVE THIS!!!!!!!!!!!!!!!!!!!!!!!
    rs = wd.s.execute(more_accurate_statement, {"look_back": last_date})
    logger.debug(f"query took this long {datetime.now() - tick}")

    for row in rs:
        logger.debug(",".join((row[0], row[1], row[2], row[3], row[4], row[5])))
        post_ids = row[1].replace("ma:", "").split(',')
        posts = []
        for post_id in post_ids:
            # print(f"\t{post_id}")
            posts.append(wd.s.query(SubmittedPost).get(post_id))

        last_post = posts[-1]
        assert isinstance(last_post, SubmittedPost)
        if not last_post.review_debug:
            last_post.review_debug = f"ma:{row[1]}"
            wd.s.add(last_post)
        posting_groups.append(
                PostingGroup(last_post.id, author_name=row[3], subreddit_name=row[4].lower(), posts=posts))
    wd.s.commit()

    logger.debug(f"Total groups found: {len(posting_groups)}")
    tick = datetime.now(pytz.utc)

    # sort this list
    logger.debug(f"sorting list...")
    posting_groups.sort(key=lambda y: y.latest_post_id, reverse=True)
    logger.debug(f"done")

    # Go through posting group
    for i, pg in enumerate(posting_groups):
        logger.debug(
            f"========================{i + 1}/{len(posting_groups)}=================================")

        # Break if taking too long
        tock = datetime.now(pytz.utc) - tick
        if tock > timedelta(minutes=10):
            logger.warning("Aborting, taking more than 10 min")
            wd.s.commit()
            break

        # Load subreddit settings
        # tr_sub = wd.sub_dict[pg.subreddit_name]
        tr_sub = get_subreddit_by_name(wd, pg.subreddit_name, update_if_due=False)
        if not tr_sub:
            logger.debug(f"Unable to find subreddit in database{pg.subreddit_name} ")
            continue
        max_count = tr_sub.max_count_per_interval
        if tr_sub.active_status_enum not in (SubStatus.ACTIVE, SubStatus.NO_BAN_ACCESS):
            logger.warning(f"Subreddit is not active {tr_sub.subreddit_name} {tr_sub.active_status_enum}")
            continue

        # Check if they're on the soft blacklist
        subreddit_author: SubAuthor = wd.s.query(SubAuthor).get((pg.subreddit_name, pg.author_name))

        # Remove any posts that are prior to eligibility
        posts_to_verify = []
        logger.debug(f"/r/{pg.subreddit_name}---max_count: {max_count}, interval: {tr_sub.min_post_interval_txt} "
              f"grace_period: {tr_sub.grace_period}")
        for j, post in enumerate(pg.posts):
            try:
                assert (isinstance(post, SubmittedPost))  #Assertion error
            except(AssertionError) as e:
                print(f"{e}")
                print(f"{post}")
                break

            logger.debug(
                f"{i}-{j}Checking: "
                f"{pg.author_name} {post.time_utc} url:{post.get_url()} reviewed:{post.reviewed}  "
                f"counted:{post.counted_status_enum} "
                f"posted:{post.posted_status}  title:{post.title[0:30]}")

            if post.counted_status_enum in (CountedStatus.NEED_REMOVE,
                                            CountedStatus.REMOVED,
                                            CountedStatus.BLKLIST_NEED_REMOVE,
                                            ):  # May not need this later
                logger.debug(
                    f"{i}-{j}\t\tAlready handled")
                continue

            # Check for post exemptions
            if not post.reviewed:

                counted_status, result = check_for_post_exemptions(tr_sub, post, wd=wd)
                post.counted_status_enum = counted_status
                #post.update_status(counted_status=counted_status)
                wd.s.add(post)
                logger.debug(f"\t\tpost status: {counted_status} {result}")
                if counted_status in ( CountedStatus.COUNTS , CountedStatus.NEED_REMOVE):
                    posts_to_verify.append(post)
                if i % 25 == 0:
                    wd.s.commit()

            else:
                logger.debug(f"{i}-{j}\t\tpost status: "
                            f"already reviewed {post.counted_status_enum} "
                            f"{'---MHB removed' if post.flagged_duplicate else ''}")

        """
        # Skip if we don't need to go through each post
        if len(left_over_posts) < max_count:
            logger.info("Did not collect enough counted posts")
            wd.s.commit()
            continue
        """

        wd.s.commit()



        # Collect all relevant posts
        logger.debug("finding back posts")
        back_posts = wd.s.query(SubmittedPost) \
            .filter(
            # SubmittedPost.flagged_duplicate.is_(False), # redundant with new flag
            SubmittedPost.subreddit_name.ilike(tr_sub.subreddit_name),
            SubmittedPost.time_utc > pg.posts[0].time_utc - tr_sub.min_post_interval + tr_sub.grace_period,
            SubmittedPost.time_utc < pg.posts[-1].time_utc,  # posts not after last post in question
            SubmittedPost.author == pg.author_name,
            SubmittedPost.counted_status_enum.in_((CountedStatus.NEEDS_UPDATE, CountedStatus.NOT_CHKD, CountedStatus.COUNTS))) \
            .order_by(SubmittedPost.time_utc) \
            .all()

        possible_pre_posts = []
        logger.debug(f"Found {len(back_posts)} backposts")
        if len(back_posts) == 0:
            # if pg.posts[-1].counted_status <2:   # This doesn't make sense?? what was this supposed to do
            #     pg.posts[-1].counted_status==2   # not an assignment!
            pg.posts[-1].reviewed = True
            wd.s.add(pg.posts[-1])

            logger.debug("Nothing to do, moving on.")
            continue

        # Check backposts
        logger.debug("reviwing back posts")
        for j, post in enumerate(back_posts):
            logger.debug(f"{i}-{j} Backpost: {post.time_utc} url:{post.get_url()}  title:{post.title[0:30]}"
                        f"\t counted_status: {post.counted_status_enum} posted_status: {post.posted_status} ")
            if post.counted_status_enum == CountedStatus.NOT_CHKD \
                    or post.counted_status_enum == CountedStatus.PREV_EXEMPT:
                counted_status, result = check_for_post_exemptions(tr_sub, post, wd=wd)
                post.counted_status_enum = counted_status
                #post.update_status(counted_status=counted_status)
                wd.s.add(post)
                logger.debug(
                    f"\tpost_counted_status updated: {post.counted_status_enum} {CountedStatus(post.counted_status_enum)}")
            if post.counted_status_enum == CountedStatus.COUNTS:
                logger.debug(f"\t....Including")
                possible_pre_posts.append(post)
            else:
                logger.debug(f"\t..exempting ")

        # Go through left over posts
        grace_count = 0
        for j, post in enumerate(posts_to_verify):
            logger.debug(f"{i}-{j} Reviewing: r/{pg.subreddit_name}  {pg.author_name}  {post.time_utc}  "
                        f"url:{post.get_url()}  title:{post.title[0:30]}"
                        f"\t counted_status: {post.counted_status_enum} posted_status: {post.posted_status}")

            # Go through possible preposts for left over post
            associated_reposts = []
            for x in possible_pre_posts:
                logger.debug(f"\tpost time:{post.time_utc} prev:{x.time_utc} "
                      f"furthestback: {post.time_utc - tr_sub.min_post_interval + tr_sub.grace_period}")
                if x.time_utc < post.time_utc - tr_sub.min_post_interval + tr_sub.grace_period:
                    if post.time_utc - x.time_utc > tr_sub.min_post_interval:
                        logger.debug("\t\t Post too far back")
                    else:
                        logger.debug("\t\t Post too far back - only grace peroid")
                        # post.update(counted_status=CountedStatus.GRACE_PERIOD_EXEMPT)
                        # s.add(post)
                    continue
                if x.id == post.id or x.time_utc > post.time_utc:
                    logger.debug("\t\t Same or future post - breaking loop")
                    break
                status = wd.ri.get_posted_status(x, get_removed_info=True)
                logger.debug(f"\t\tpost status: {status} gp:{tr_sub.grace_period} diff: {post.time_utc - x.time_utc}")
                if status == PostedStatus.SELF_DEL and post.time_utc - x.time_utc < tr_sub.grace_period:
                    logger.debug("\t\t Grace period exempt")
                    grace_count += 1
                    if grace_count < 3:
                        logger.debug("\t\t Grace period exempt")

                        post.counted_status_enum = CountedStatus.GRACE_PERIOD_EXEMPT
                        wd.s.add(post)
                        continue
                    else:
                        logger.debug("\t\t Too many grace exemptions")
                associated_reposts.append(x)

            # not enough posts
            if len(associated_reposts) < tr_sub.max_count_per_interval:
                logger.debug(f"\tNot enough previous posts: {len(associated_reposts)}/{max_count}: "
                            f"{','.join([x.id for x in associated_reposts])}")
                post.reviewed=True

            # Hall pass eligible
            elif subreddit_author and subreddit_author.hall_pass > 0:
                subreddit_author.hall_pass -= 1
                notification_text = f"Hall pass was used by {subreddit_author.author_name}: http://redd.it/{post.id}"
                # REDDIT_CLIENT.redditor(BOT_OWNER).message(pg.subreddit_name, notification_text)

                wd.ri.send_modmail(subreddit=tr_sub, subject="[Notification]  Hall pass was used",
                                   body=notification_text)
                # tr_sub.send_modmail(subject="[Notification]  Hall pass was used", body=notification_text)
                post.counted_status_enum = CountedStatus.HALLPASS
                wd.s.add(subreddit_author)
            # Must take action on post
            else:
                logger.info("post needs action - identifyinig action")
                do_requested_action_for_valid_reposts(tr_sub, post, associated_reposts, wd=wd)
                # post.update_status(reviewed=True, flagged_duplicate=True)
                wd.s.add(post)
                # Keep preduplicate posts to keep track of later
                for predupe_post in associated_reposts:
                    predupe_post.pre_duplicate = True
                    wd.s.add(predupe_post)
                wd.s.commit()  # just did a lot of work, need to save
                check_for_actionable_violations(tr_sub, post, associated_reposts, wd=wd)
            wd.s.add(post)
        wd.s.commit()

    wd.s.commit()





def do_requested_action_for_valid_reposts(tr_sub: TrackedSubreddit, recent_post: SubmittedPost,
                                          most_recent_reposts: List[SubmittedPost], wd=None):
    bot_name = wd.ri.bot_name
    possible_repost = most_recent_reposts[-1]
    if tr_sub.comment:
        recent_post.reply_comment = recent_post.reply_comment = make_comment(tr_sub, recent_post, most_recent_reposts,
                     tr_sub.comment, distinguish=tr_sub.distinguish, approve=tr_sub.approve,
                     lock_thread=tr_sub.lock_thread, stickied=tr_sub.comment_stickied, wd=wd, do_actual_comment=False)
    if tr_sub.modmail:
        message = tr_sub.modmail
        if message is True:
            message = "Repost that violates rules: [{title}]({url}) by [{author}](/u/{author})"
        # send_modmail_populate_tags(tr_sub, message, recent_post=recent_post, prev_post=possible_repost, )
        logger.debug("sending modmail notification)")
        wd.ri.send_modmail(subreddit=tr_sub,
                           body=tr_sub.populate_tags(message, recent_post=recent_post, prev_post=possible_repost),
                           subject="[Notification] Post that violates rule frequency restriction", use_same_thread=True)
    if tr_sub.action == "remove":
        recent_post.counted_status_enum = CountedStatus.NEED_REMOVE
        logger.debug(f"Post marked for removal {recent_post.subreddit_name} {recent_post.id} {recent_post.author}")
        if len(most_recent_reposts) >3:
            soft_blacklist(tr_sub, recent_post, recent_post.next_eligible, wd=wd)


    if tr_sub.action == "report":
        logger.debug("reporting post")
        if tr_sub.report_reason:
            rp_reason = tr_sub.populate_tags(tr_sub.report_reason, recent_post=recent_post, prev_post=possible_repost)
            wd.ri.get_submission_api_handle(recent_post).report(f"{bot_name}: {rp_reason}"[0:99])
        else:
            wd.ri.get_submission_api_handle(recent_post).report(f"{bot_name}: repeatedly exceeding posting threshold")
    if tr_sub.message and recent_post.author and wd.ri.get_submission_api_handle(recent_post).author:
        try:
            wd.ri.get_submission_api_handle(
                recent_post).author.message("Regarding your post", tr_sub.populate_tags(tr_sub.message,
                                                                                        recent_post=recent_post,
                                                                                        post_list=most_recent_reposts))
        except (praw.exceptions.APIException, prawcore.exceptions.Forbidden):
            logger.debug("\tcould not send message")



def check_for_actionable_violations(tr_sub: TrackedSubreddit, recent_post: SubmittedPost,
                                    most_recent_reposts: List[SubmittedPost], wd=None):
    possible_repost = most_recent_reposts[-1]
    tick = datetime.now(pytz.utc)
    other_spam_by_author = wd.s.query(SubmittedPost).filter(
        # SubmittedPost.flagged_duplicate.is_(True),
        SubmittedPost.counted_status_enum == CountedStatus.FLAGGED,
        SubmittedPost.author == recent_post.author,
        SubmittedPost.subreddit_name.ilike(tr_sub.subreddit_name),
        SubmittedPost.time_utc < recent_post.time_utc) \
        .all()

    logger.info("Author {0} had {1} rule violations. Banning if at least {2} - query time took: {3}"
                .format(recent_post.author, len(other_spam_by_author), tr_sub.ban_threshold_count,
                        datetime.now(pytz.utc) - tick))

    if tr_sub.ban_duration_days is None or isinstance(tr_sub.ban_duration_days, str):
        logger.info("No bans per wiki. ban_duration_days is {}".format(tr_sub.ban_duration_days))
        if tr_sub.ban_ability != 0:
            tr_sub.ban_ability = 0
            wd.s.add(tr_sub)
            wd.s.commit()
        # if len(most_recent_reposts) > 2:  this doesn't work - doesn't count bans
        #    logger.info("Adding to soft blacklist based on next eligibility - for tracking only")
        #    next_eligibility = most_recent_reposts[0].time_utc + subreddit.min_post_interval
        #    soft_blacklist(tr_sub, recent_post, next_eligibility)
        return

    if len(other_spam_by_author) == tr_sub.ban_threshold_count - 1 and tr_sub.ban_threshold_count > 1:
        try:
            # tr_sub.ignore_AutoModerator_removed

            wd.ri.reddit_client.redditor(recent_post.author).message(
                subject=f"Beep! Boop! Please note that you are close approaching "
                        f"your posting limit for {recent_post.subreddit_name}",
                message=
                f"This subreddit (/r/{recent_post.subreddit_name}) only allows {tr_sub.max_count_per_interval} post(s) "
                f"per {humanize.precisedelta(tr_sub.min_post_interval)}. "
                f"This {'does NOT' if tr_sub.ignore_moderator_removed else 'DOES'} include mod-removed posts. "
                f"While this post was within the post limiting rule and not removed by this bot, "
                f"please do not make any new posts before "
                f"{most_recent_reposts[0].time_utc + tr_sub.min_post_interval} UTC, as it "
                f"may result in a ban. If you made a title mistake you have "
                f"STRICTLY {humanize.precisedelta(tr_sub.grace_period)} to delete it and repost it. "
                f"This is an automated message. "
            )
        except praw.exceptions.APIException:
            pass

    if len(other_spam_by_author) >= tr_sub.ban_threshold_count:
        num_days = tr_sub.ban_duration_days

        if 0 < num_days < 1:
            num_days = 1
        if num_days > 998:
            num_days = 999
        if num_days == 0:
            num_days = 999

        str_prev_posts = ",".join(
            [" [{0}]({1})".format(a.id, "http://redd.it/{}".format(a.id)) for a in other_spam_by_author])

        ban_message = f"This subreddit (/r/{recent_post.subreddit_name}) only allows {tr_sub.max_count_per_interval} " \
                      f"post(s) per {humanize.precisedelta(tr_sub.min_post_interval)}, and it only allows for " \
                      f"{tr_sub.ban_threshold_count} violation(s) of this rule. This is a rolling limit and " \
                      f"includes self-deletions. Per our records, there were {len(other_spam_by_author)} post(s) " \
                      f"from you that went beyond the limit: {str_prev_posts} If you think you may have been hacked, " \
                      f"please change your passwords NOW. "
        time_next_eligible = datetime.now(pytz.utc) + timedelta(days=num_days)

        # If banning is specified but not enabled, just go to blacklist. Don't bother trying to ban without access.
        if tr_sub.ban_ability == -2:
            if tr_sub.ban_duration_days > 998:
                # Only do a 2 week ban if specified permanent ban
                time_next_eligible = datetime.now(pytz.utc) + timedelta(days=999)
            elif tr_sub.ban_duration_days == 0:
                # Only do a 2 week ban if specified permanent ban
                time_next_eligible = datetime.now(pytz.utc) + timedelta(days=14)

            soft_blacklist(tr_sub, recent_post, time_next_eligible, wd=wd)
            return

        try:
            if num_days == 999:
                # Permanent ban
                wd.ri.reddit_client.subreddit(tr_sub.subreddit_name).banned.add(
                    recent_post.author, note="ModhelpfulBot: repeated spam", ban_reason="MHB: posting too much",
                    ban_message=ban_message[:999])
                logger.info(f"PERMANENT ban for {recent_post.author} succeeded ")
            else:
                # Not permanent ban
                ban_message += f"\n\nYour ban will last {num_days} day{'s' if num_days > 1 else ''} from this message. " \
                               f"**Repeat infractions result in a permanent ban!**"

                wd.ri.reddit_client.subreddit(tr_sub.subreddit_name).banned.add(
                    recent_post.author, note="ModhelpfulBot: repeated spam", ban_message=ban_message[:999],
                    ban_reason="MHB: posting too much",
                    duration=num_days)
                logger.info(f"Ban for {recent_post.author} succeeded for {num_days} days")
        except praw.exceptions.APIException:
            pass
        except prawcore.exceptions.Forbidden:

            logger.info("Ban failed - no access?")
            tr_sub.ban_ability = -2
            if tr_sub.notify_about_spammers:
                response_lines = [
                    "This person has multiple rule violations. "
                    "Please adjust my privileges and ban threshold "
                    "if you would like me to automatically ban them.\n\n".format(
                        recent_post.author, len(other_spam_by_author), tr_sub.ban_threshold_count)]

                for post in other_spam_by_author:
                    response_lines.append(f"* {post.time_utc}: "
                                          f"[{post.author}](/u/{post.author}) "
                                          f"[{post.title}]({post.get_comments_url()})\n")
                response_lines.append(f"* {recent_post.time_utc}: "
                                      f"[{recent_post.author}](/u/{recent_post.author}) "
                                      f"[{recent_post.title}]({recent_post.get_comments_url()})\n")

                # send_modmail_populate_tags(tr_sub, "\n\n".join(response_lines), recent_post=recent_post, prev_post=possible_repost)
                wd.ri.send_modmail(subreddit=tr_sub, subject="[Notification] Multiple post frequency violations",
                                   body=tr_sub.populate_tags2("\n\n".join(response_lines),
                                                              recent_post=recent_post, prev_post=possible_repost))
            if tr_sub.ban_duration_days > 998:
                # Only do a 2-week ban if specified permanent ban
                time_next_eligible = datetime.now(pytz.utc) + timedelta(days=999)
            elif tr_sub.ban_duration_days == 0:
                # Only do a 2-week ban if specified permanent ban
                time_next_eligible = datetime.now(pytz.utc) + timedelta(days=14)

            soft_blacklist(tr_sub, recent_post, time_next_eligible, wd=wd)


def make_comment(subreddit: TrackedSubreddit, recent_post: SubmittedPost, most_recent_reposts, comment_template: String,
                 distinguish=False, approve=False, lock_thread=True, stickied=False, next_eligibility: datetime = None,
                 blacklist=False, wd=None, do_actual_comment=True):
    prev_submission = most_recent_reposts[-1] if most_recent_reposts else None
    if not next_eligibility:
        next_eligibility = most_recent_reposts[0].time_utc + subreddit.min_post_interval
        recent_post.next_eligible = next_eligibility
    # print(most_recent_reposts)
    reposts_str = ",".join(
        [f" [{a.id}]({a.get_comments_url()})" for a in most_recent_reposts]) \
        if most_recent_reposts and most_recent_reposts[0] else "BL"
    if blacklist:
        reposts_str = " Temporary lock out per " + reposts_str
    else:
        reposts_str = " Previous post(s):" + reposts_str
    ids = f"{reposts_str} | limit: {{maxcount}} per {{interval}} | " \
          f"next eligibility: {next_eligibility.strftime('%Y-%m-%d %H:%M UTC')}"

    ids = ids.replace(" ", " ^^")
    comment = None
    response = subreddit.populate_tags2(f"{comment_template}{RESPONSE_TAIL}{ids}",
                                        recent_post=recent_post, prev_post=prev_submission, wd=wd)


    if not do_actual_comment:
        return response
    try:
        comment: praw.models.Comment | None = \
            wd.ri.reply(recent_post, response, distinguish=distinguish, approve=approve, lock_thread=lock_thread)

        # assert comment

        if stickied and comment:
            comment.mod.distinguish(how='yes', sticky=True)
            try:
                recent_post.bot_comment_id = comment.id
            except AttributeError:
                print(comment, type(comment))
                logger.warning(f'tried to sticky a comment but failed: Attribute Error')

    except (praw.exceptions.APIException, prawcore.exceptions.Forbidden) as e:
        logger.warning(f'something went wrong in creating comment {str(e)}')
    return comment


def soft_blacklist(tr_sub: TrackedSubreddit, recent_post: SubmittedPost, time_next_eligible: datetime, wd=None):
    # time_next_eligible = datetime.now(pytz.utc) + timedelta(days=num_days)
    logger.info("Author added to blacklisted 2/2 no permission to ban. Ban duration is {}"
                .format(tr_sub.ban_duration_days, ))
    # Add to the watch list
    subreddit_author: SubAuthor = wd.s.query(SubAuthor).get((tr_sub.subreddit_name, recent_post.author))
    if not subreddit_author:
        subreddit_author = SubAuthor(tr_sub.subreddit_name, recent_post.author)
    subreddit_author.last_valid_post = recent_post.id
    subreddit_author.next_eligible = time_next_eligible
    wd.s.add(subreddit_author)
    wd.s.add(tr_sub)
    wd.s.commit()


from workingdata import WorkingData


def get_subreddit_by_name(wd: WorkingData, subreddit_name: str, create_if_not_exist=True, update_if_due=False):
    # check if tr_sub already loaded in memory
    tr_sub: TrackedSubreddit = wd.sub_dict.get(subreddit_name)
    if tr_sub:  # we have the sub on record
        return tr_sub

    if not tr_sub: # know about the sub, but not being loaded (permissions issues, etc.)
        tr_sub: TrackedSubreddit = wd.s.query(TrackedSubreddit).get(subreddit_name)

    # Give up as requested if not in db
    if not tr_sub and not create_if_not_exist:
        print(f"GSBN: doesn't exist and not supposed to create  {subreddit_name}")
        return None

    # If need to create this, do so now
    if not tr_sub:
        print("GSBN: creating sub...")
        sub_info = wd.ri.get_subreddit_info(subreddit_name=subreddit_name)  # get subreddit info for sub from api
        if subreddit_name == MAIN_BOT_NAME or \
                (sub_info and sub_info.active_status_enum not in (SubStatus.SUB_FORBIDDEN, SubStatus.SUB_GONE)):   # make sure sub is accessible
            tr_sub = TrackedSubreddit(subreddit_name=subreddit_name, sub_info=sub_info)  # add sub to sb
            wd.s.add(tr_sub)
            wd.s.commit()
            wd.sub_dict[subreddit_name] = tr_sub  # add sub to current working list

        else:
            print(f"GSBN: subreddit doesn't exist  {sub_info}")
            return None

    # Update from scratch if it has been a while
    if not tr_sub.config_last_checked or tr_sub.config_last_checked < datetime.now() - timedelta(hours=SUBWIKI_CHECK_INTERVAL_HRS):
        print(f"GSBN: needs update {tr_sub.subreddit_name}")
        sub_info = wd.ri.get_subreddit_info(subreddit_name=tr_sub.subreddit_name)

        worked, status = tr_sub.update_from_subinfo(sub_info)
        tr_sub.config_last_checked = datetime.now()  #this should be UTC... need to fix
        wd.s.add(tr_sub)

    else:  # or just load from database
        worked, status = tr_sub.reload_yaml_settings()
        if not worked:

            print(f"GSBN: couldn't load from stored info {tr_sub.subreddit_name} because {status} ACTIVE STATUS {tr_sub.active_status_enum}")
            # sub_info = wd.ri.get_subreddit_info(subreddit_name=tr_sub.subreddit_name)

            # if hasattr(sub_info, 'yaml_settings_text'):
            #     print(f"GSBN: settings: {sub_info.yaml_settings_text}")
            # else:z
            #     print(f"GSBN: no luck in getting yaml text  {tr_sub.subreddit_name}")
            # worked, status = tr_sub.update_from_subinfo(sub_info)
            # worked, status = tr_sub.reload_yaml_settings()

    if not worked:
        print(f"GSBN: didn't exist? {worked} {status}")
        return None

    wd.s.add(tr_sub)
    wd.s.commit()
    wd.sub_dict[subreddit_name] = tr_sub
    return tr_sub


"""  
        need to figure out check_actioned
        elif hasattr(tr_sub, "settings_revision_date"):
            if not check_actioned(wd, f"wu-{subreddit_name}-{tr_sub.settings_revision_date}"):
                wd.ri.send_modmail(subject="[Notification] wiki settings loading error"
                                           f"There was an error loading your {BOT_NAME} configuration: {status} "
                                           f"\n\n https://www.reddit.com/r/{subreddit_name}"
                                           f"/wiki/edit/{BOT_NAME}. \n\n"
                                           f"Please see https://www.reddit.com/r/{BOT_NAME}/wiki/index for examples",
                                   subreddit_name=subreddit_name)
                record_actioned(wd, f"wu-{subreddit_name}-{tr_sub.settings_revision_date}")
"""
