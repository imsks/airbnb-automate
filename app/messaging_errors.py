"""Failures that must not be treated as successful message delivery."""


class SessionExpired(RuntimeError):
    """Airbnb requires the account owner to sign in again."""


class ComposerUnavailable(RuntimeError):
    """No usable message composer or send control was found."""


class DeliveryUnconfirmed(RuntimeError):
    """Submission may have happened; inspect the thread before retrying."""


class MessageRejected(RuntimeError):
    """Airbnb refused the text before sending it. Nothing reached the host.

    Distinct from :class:`DeliveryUnconfirmed`: Airbnb states the message was
    not sent, so rewriting and trying again is safe.
    """

    def __init__(self, message: str, terms=None) -> None:
        super().__init__(message)
        self.terms = list(terms or [])