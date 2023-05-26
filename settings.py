import os

scopes = {'edit', 'flair', 'save', 'read', 'vote', 'history', 'submit', 'modconfig', 'identity', 'modcontributors',
          'modflair', 'modlog', 'modothers', 'modposts', 'modself', 'modwiki', 'mysubreddits', 'privatemessages', 'report', 'subscribe', 'wikiedit', 'wikiread'}
user_agent = "ModeratelyHelpfulBot v1.1 by /u/antidense"

CLIENT_ID = os.environ['CLIENT_ID']
CLIENT_SECRET = os.environ['CLIENT_SECRET']
BOT_PW = os.environ['BOT_PW']
BOT_NAME = os.environ['BOT_NAME']
BOT_OWNER = os.environ['BOT_OWNER']
DB_ENGINE = os.environ['DB_ENGINE']  #'mysql+pymysql://username:password@host:3306/yourdatabase?charset=utf8mb4'

