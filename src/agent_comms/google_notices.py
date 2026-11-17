"""Fixed person-facing Google Chat lines; no provider text or identity is copied."""

NO_SESSION = ("This agent is not running. Your message is kept for up to 24 hours "
              "and will be retried if it starts.")
TEMPORARILY_UNAVAILABLE = ("This agent cannot take your message right now. "
                           "Your message is kept for up to 24 hours.")
HELD = "This agent is not taking live messages. Your message is saved for later review."
NOT_ACCEPTING = "This agent is not accepting messages."
NOT_LISTED = "You are not on this agent's list."
FILE_TOO_LARGE = "The file is too large (limit 50 MiB). No text was delivered."
DRIVE_BACKED = ("This kind of Chat attachment is not supported yet. "
                "Attach the file itself or send a Drive link.")
NOT_DELIVERED = "Your message was not delivered. Please send it again."


def for_refusal(reason: str) -> str:
    if reason.partition(" (Google account ")[0] == "person is not listed":
        return NOT_LISTED
    if reason == "Drive-backed attachment is not supported":
        return DRIVE_BACKED
    if reason == "file too large":
        return FILE_TOO_LARGE
    if reason == "agent delivery is none":
        return NOT_ACCEPTING
    return NOT_DELIVERED
