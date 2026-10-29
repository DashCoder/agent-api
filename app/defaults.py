"""Default site configuration. Admins override these in the UI (stored in the settings table)."""

DEFAULT_SITE = {
    "brand": {
        "name": "Agent UI",
        "adminTitle": "builder.example.com",
        "landingUrl": "/",
    },
    "assistant": {
        "name": "Builder",
        "greeting": "Hi! I'm Builder 👋 How can I help you today?\n\n"
                    "By the way — tap \"🔐 Save my work\" below if you'd like your saved tiles and "
                    "documents linked to your email, so they're there next time, even on a different device.",
        "learnByDefault": False,
    },
    "avatar": {
        # Leave lists empty to show the poster image only.
        # Idle clips play while listening; talk clips while the assistant is replying.
        "posterUrl": "/avatar/poster.svg",
        "idleVideos": [],
        "talkVideos": [],
        "speakReplies": False,
    },
    "quickReplies": [
        "What services do you offer?",
        "How do I make a booking?",
        "What are your rates?",
        "What areas do you cover?",
    ],
    # Used by the built-in mock agent and as context you can pass to Hermes.
    "business": {
        "services": "We set up AI assistants that answer calls, chat on your website and book appointments.",
        "booking": "Pick a time in the calendar, or tell me a day that suits you and I'll find a slot.",
        "rates": "Plans start at £49 per month, with a free 14-day trial.",
        "areas": "We work with businesses across the UK, fully remotely.",
    },
    "phone": {"number": "", "script": ""},
    "calendar": {"provider": "", "connected": False},
}
