from pathlib import Path


class Songs(dict[str, Path]):
    """
    key -> sound name
    value -> sound file dir
    """

    def names(self) -> set[str]:
        return set(self.keys())
