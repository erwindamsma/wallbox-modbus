"""What an energy source is, and the registry that finds one by name.

A source is anything that can keep a MeterModel fed with the power measured at
the grid connection point. Home Assistant is the only one shipped, but nothing
above this line knows that: `source.type` in the config selects a class from the
registry below, and that class brings its own configuration options with it.

Writing a new one means three things -- a config dataclass, a `run()` that
loops forever, and a `register()` call. See homeassistant.py as the worked
example and CONTRIBUTING.md for what a source has to get right.
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

    Subclasses must:

    * call ``model.update(power_w=..., voltage=...)`` with the power measured
      at the grid connection point, **positive when importing**. Getting that
      sign backwards makes the charger speed up exactly when it should back
      off, so a source is responsible for normalising it;
    * keep ``self.connected`` truthful, because the status line reports it;
    * let ``run()`` raise ``asyncio.CancelledError`` through on shutdown.

    A source does *not* have to handle its own failure. Simply ceasing to call
    ``model.update()`` trips the failsafe after ``failsafe.max_data_age_s``,
    which is the safe outcome. Reconnecting on your own is a kindness, not a
    requirement.
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
        """Raise ValueError if this config cannot work.

        ``complete`` is False when the config is not expected to be finished
        yet -- ``--list-entities`` runs precisely to find out what to put in
        it -- so only check what is needed to make contact.
        """

    def describe(self) -> str:
        """One line naming where the data comes from, for logs and --check-source."""
        return self.cfg.type

    async def run(self) -> None:
        """Feed the model until cancelled. Must not return on its own."""
        raise NotImplementedError

    @classmethod
    async def discover(cls, cfg: SourceConfig) -> list[dict] | None:
        """Candidate readings this source can see, for ``--list-entities``.

        Return a list of ``{"id", "value", "unit", "name", "likely"}`` dicts,
        likeliest grid sensors flagged with ``likely``. Return None (the
        default) if the source has nothing to enumerate -- a fixed serial
        protocol, say -- and the CLI will say so rather than print an empty
        table.
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
