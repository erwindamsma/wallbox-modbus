"""Energy sources, and the registry that finds one by name.

A source keeps the MeterModel fed with power measured at the grid connection.
`source.type` picks a class out of the registry below and that class brings its
own config options, so nothing outside this package knows Home Assistant exists.

Writing one: a config dataclass, a run() that loops forever, a register() call.
homeassistant.py is the worked example.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SourceConfig:
    """Options every source understands. Subclass it to add your own."""

    type: str = "homeassistant"
    # push = the source tells us when the value changes; poll = we ask.
    # A source that can only do one of the two should say so in validate().
    mode: str = "push"
    poll_interval_s: float = 1.0
    # Override unit detection when the source cannot report its own unit.
    power_unit: str | None = None


class EnergySource:
    """Base class for energy sources.

    Subclasses must call model.update(power_w=..., voltage=...) with power at
    the grid connection, positive when importing. Flip the sign in the source
    if yours reports the other way round: get it wrong and the charger speeds
    up exactly when it should back off. Keep self.connected honest, the status
    line prints it, and let run() pass CancelledError through on shutdown.

    You don't have to handle your own failures. Just stop calling update() and
    the failsafe trips after failsafe.max_data_age_s, which is the safe
    outcome. Reconnecting yourself is nice, not required.
    """

    #: The ``source.type`` value that selects this class.
    type: str = ""
    #: The dataclass holding this source's options; a SourceConfig subclass.
    config_class: type[SourceConfig] = SourceConfig

    def __init__(self, cfg: SourceConfig, model):
        self.cfg = cfg
        self.model = model
        self.connected = False

    @classmethod
    def validate(cls, cfg: SourceConfig, complete: bool = True) -> None:
        """Raise ValueError if this config can't work.

        complete=False means the config isn't finished yet (--list-entities
        runs to find out what to put in it), so only check what you need to
        make contact.
        """

    def describe(self) -> str:
        """One line naming where the data comes from, for logs and --check-source."""
        return self.cfg.type

    async def run(self) -> None:
        """Feed the model until cancelled. Must not return on its own."""
        raise NotImplementedError

    @classmethod
    async def discover(cls, cfg: SourceConfig) -> list[dict] | None:
        """Readings this source can see, for --list-entities.

        Return {"id", "value", "unit", "name", "likely"} dicts with the
        probable grid sensors flagged. None (the default) means there's
        nothing to enumerate, and the CLI says so instead of printing an
        empty table.
        """
        return None


_REGISTRY: dict[str, type[EnergySource]] = {}


def register(cls: type[EnergySource]) -> type[EnergySource]:
    """Class decorator: make `cls` selectable as `source.type`."""
    if not cls.type:
        raise ValueError(f"{cls.__name__} must set a non-empty `type`")
    _REGISTRY[cls.type] = cls
    return cls


def available() -> list[str]:
    return sorted(_REGISTRY)


def get(type_name: str) -> type[EnergySource]:
    try:
        return _REGISTRY[type_name]
    except KeyError:
        raise ValueError(
            f"unsupported source.type {type_name!r}; available: {', '.join(available())}"
        ) from None


def create(cfg: SourceConfig, model) -> EnergySource:
    return get(cfg.type)(cfg, model)
