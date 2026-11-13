class VoiceManagerError(Exception):
    pass


class VoiceManagerQueueFullError(VoiceManagerError):
    pass


class VoiceChannelDisconnected(VoiceManagerError):
    pass


class VoiceClientError(VoiceManagerError):
    pass
