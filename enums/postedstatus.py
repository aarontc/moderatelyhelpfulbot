from enum import Enum

class PostedStatus(Enum):
    SELF_DEL = "self-deleted"
    UP = "up"
    MOD_RM = "mod-removed"
    AUTOMOD_RM = "AutoMod-removed"
    MHB_RM = "MHB-removed"
    BOT_RM = "Bot-removed"
    SPAM_FLT = "Spam-filtered"
    UNKNOWN = "Unknown status"
    FH_RM = "Flair_Helper removed"
    UNAVAILABLE = "Unavailable"
